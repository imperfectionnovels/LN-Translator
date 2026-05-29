"""Tests for the novel soft-delete + Archive + Purge service.

Invariants exercised:
- archive sets deleted_at; second archive is a no-op
- restore clears deleted_at; restore on non-archived is 409
- purge requires archive first; CASCADE fires on chapters
- delete_counts aggregates correctly across chapters / glossary / etc.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import _ADDITIVE_MIGRATIONS, SCHEMA
from backend.main import app

DB_PATH = Path(os.environ["DB_PATH"])


def _setup_db() -> None:
    """Re-init schema + apply migrations. Same approach as test_genres_novel.py."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    for stmt in _ADDITIVE_MIGRATIONS:
        try:
            conn.executescript(stmt)
        except sqlite3.OperationalError:
            pass
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    conn.close()


def _insert_novel(title: str = "Novel A") -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO novels (title, source_type) VALUES (?, 'paste')",
        (title,),
    )
    conn.commit()
    novel_id = cur.lastrowid
    conn.close()
    return novel_id


def _insert_chapter(novel_id: int, chapter_num: int, *, cost_usd: float = 0.0) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO chapters (novel_id, chapter_num, original_text, status, cost_usd) "
        "VALUES (?, ?, '...', 'done', ?)",
        (novel_id, chapter_num, cost_usd),
    )
    conn.commit()
    chapter_id = cur.lastrowid
    conn.close()
    return chapter_id


@pytest.fixture
def client():
    _setup_db()
    return TestClient(app)


@pytest.mark.asyncio
async def test_delete_counts_aggregates():
    """delete_counts returns chapters, cost, etc. across the affected tables."""
    from backend.db import open_conn
    from backend.services.soft_delete import delete_counts

    _setup_db()
    novel_id = _insert_novel()
    _insert_chapter(novel_id, 1, cost_usd=0.50)
    _insert_chapter(novel_id, 2, cost_usd=0.75)

    async with open_conn() as conn:
        counts = await delete_counts(conn, novel_id)

    assert counts.novel_id == novel_id
    assert counts.chapters == 2
    assert counts.total_cost_usd == pytest.approx(1.25)
    assert counts.glossary_entries == 0
    assert counts.bookmarks == 0


@pytest.mark.asyncio
async def test_archive_then_restore_round_trip():
    from backend.db import open_conn
    from backend.services.soft_delete import archive_novel, restore_novel

    _setup_db()
    novel_id = _insert_novel()

    async with open_conn() as conn:
        await archive_novel(conn, novel_id)

    # Verify deleted_at is set.
    conn_sync = sqlite3.connect(DB_PATH)
    row = conn_sync.execute(
        "SELECT deleted_at FROM novels WHERE id = ?", (novel_id,),
    ).fetchone()
    conn_sync.close()
    assert row[0] is not None

    async with open_conn() as conn:
        await restore_novel(conn, novel_id)

    conn_sync = sqlite3.connect(DB_PATH)
    row = conn_sync.execute(
        "SELECT deleted_at FROM novels WHERE id = ?", (novel_id,),
    ).fetchone()
    conn_sync.close()
    assert row[0] is None


@pytest.mark.asyncio
async def test_restore_unarchived_returns_409():
    from fastapi import HTTPException

    from backend.db import open_conn
    from backend.services.soft_delete import restore_novel

    _setup_db()
    novel_id = _insert_novel()

    async with open_conn() as conn:
        with pytest.raises(HTTPException) as exc_info:
            await restore_novel(conn, novel_id)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_purge_requires_archive_first():
    from fastapi import HTTPException

    from backend.db import open_conn
    from backend.services.soft_delete import purge_novel

    _setup_db()
    novel_id = _insert_novel()

    async with open_conn() as conn:
        with pytest.raises(HTTPException) as exc_info:
            await purge_novel(conn, novel_id)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_purge_after_archive_cascades_to_chapters():
    from backend.db import open_conn
    from backend.services.soft_delete import archive_novel, purge_novel

    _setup_db()
    novel_id = _insert_novel()
    _insert_chapter(novel_id, 1)
    _insert_chapter(novel_id, 2)

    async with open_conn() as conn:
        await archive_novel(conn, novel_id)
        counts = await purge_novel(conn, novel_id)
    assert counts.chapters == 2

    # Chapters should be gone (CASCADE).
    conn_sync = sqlite3.connect(DB_PATH)
    n = conn_sync.execute(
        "SELECT COUNT(*) FROM chapters WHERE novel_id = ?", (novel_id,),
    ).fetchone()[0]
    conn_sync.close()
    assert n == 0


@pytest.mark.asyncio
async def test_list_archived_returns_only_archived():
    from backend.db import open_conn
    from backend.services.soft_delete import archive_novel, list_archived

    _setup_db()
    active = _insert_novel("Active")
    to_archive = _insert_novel("Will Archive")

    async with open_conn() as conn:
        await archive_novel(conn, to_archive)
        archived = await list_archived(conn)

    ids = {n["id"] for n in archived}
    assert to_archive in ids
    assert active not in ids


@pytest.mark.asyncio
async def test_archive_is_idempotent():
    """Calling archive on an already-archived novel must not error or
    change the deleted_at timestamp."""
    from backend.db import open_conn
    from backend.services.soft_delete import archive_novel

    _setup_db()
    novel_id = _insert_novel()

    async with open_conn() as conn:
        await archive_novel(conn, novel_id)

    conn_sync = sqlite3.connect(DB_PATH)
    ts_first = conn_sync.execute(
        "SELECT deleted_at FROM novels WHERE id = ?", (novel_id,),
    ).fetchone()[0]
    conn_sync.close()

    async with open_conn() as conn:
        await archive_novel(conn, novel_id)

    conn_sync = sqlite3.connect(DB_PATH)
    ts_second = conn_sync.execute(
        "SELECT deleted_at FROM novels WHERE id = ?", (novel_id,),
    ).fetchone()[0]
    conn_sync.close()

    assert ts_first == ts_second
