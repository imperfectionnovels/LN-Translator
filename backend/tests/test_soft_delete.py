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


def _insert_chapter(novel_id: int, chapter_num: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO chapters (novel_id, chapter_num, original_text, status) "
        "VALUES (?, ?, '...', 'done')",
        (novel_id, chapter_num),
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
    """delete_counts returns chapters, glossary, etc. across the affected tables."""
    from backend.db import open_conn
    from backend.services.soft_delete import delete_counts

    _setup_db()
    novel_id = _insert_novel()
    _insert_chapter(novel_id, 1)
    _insert_chapter(novel_id, 2)

    async with open_conn() as conn:
        counts = await delete_counts(conn, novel_id)

    assert counts.novel_id == novel_id
    assert counts.chapters == 2
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


# ---- Bug hunt 2026-08-04 (B2): archive stops queued work -------------------


def _insert_chapter_row(novel_id: int, chapter_num: int, **cols) -> int:
    """Chapter insert with arbitrary column overrides (queue flags etc.)."""
    base = {
        "novel_id": novel_id, "chapter_num": chapter_num,
        "original_text": "...", "status": "done",
    }
    base.update(cols)
    keys = ", ".join(base)
    placeholders = ", ".join("?" for _ in base)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        f"INSERT INTO chapters ({keys}) VALUES ({placeholders})",
        tuple(base.values()),
    )
    conn.commit()
    chapter_id = cur.lastrowid
    conn.close()
    return chapter_id


def _chapter_state(chapter_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT translate_queued, queue_priority, refinement_status, status "
        "FROM chapters WHERE id = ?",
        (chapter_id,),
    ).fetchone()
    conn.close()
    return dict(row)


@pytest.mark.asyncio
async def test_archive_clears_queue_flags_and_demotes_pending_refinement():
    from backend.db import open_conn
    from backend.services.soft_delete import archive_novel

    _setup_db()
    novel_id = _insert_novel()
    queued = _insert_chapter_row(
        novel_id, 1, status="pending", translate_queued=1, queue_priority=7,
    )
    refining = _insert_chapter_row(
        novel_id, 2, status="done", refinement_status="pending",
    )
    in_flight = _insert_chapter_row(
        novel_id, 3, status="translating", translate_queued=1,
    )

    async with open_conn() as conn:
        await archive_novel(conn, novel_id)

    q = _chapter_state(queued)
    assert (q["translate_queued"], q["queue_priority"]) == (0, 0)
    assert _chapter_state(refining)["refinement_status"] == "none"
    # In-flight rows are left for the worker to finish (cancel semantics).
    assert _chapter_state(in_flight)["translate_queued"] == 1


@pytest.mark.asyncio
async def test_archive_leaves_other_novels_queue_untouched():
    from backend.db import open_conn
    from backend.services.soft_delete import archive_novel

    _setup_db()
    archived = _insert_novel("A")
    active = _insert_novel("B")
    _insert_chapter_row(archived, 1, status="pending", translate_queued=1)
    other = _insert_chapter_row(
        active, 1, status="pending", translate_queued=1, queue_priority=3,
    )

    async with open_conn() as conn:
        await archive_novel(conn, archived)

    o = _chapter_state(other)
    assert (o["translate_queued"], o["queue_priority"]) == (1, 3)


@pytest.mark.asyncio
async def test_restore_does_not_resurrect_queue_flags():
    """Restore brings the novel back but NOT its queue: the user re-queues
    explicitly (documented B2 behavior)."""
    from backend.db import open_conn
    from backend.services.soft_delete import archive_novel, restore_novel

    _setup_db()
    novel_id = _insert_novel()
    queued = _insert_chapter_row(
        novel_id, 1, status="pending", translate_queued=1, queue_priority=5,
    )
    refining = _insert_chapter_row(
        novel_id, 2, status="done", refinement_status="pending",
    )

    async with open_conn() as conn:
        await archive_novel(conn, novel_id)
        await restore_novel(conn, novel_id)

    q = _chapter_state(queued)
    assert (q["translate_queued"], q["queue_priority"]) == (0, 0)
    assert _chapter_state(refining)["refinement_status"] == "none"


# ---- Bug hunt 2026-08-04 (B4): CAT segment counts in delete_counts ---------


def _insert_segment(novel_id: int, chapter_id: int, seg_index: int,
                    status: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO chapter_segments (novel_id, chapter_id, seg_index, "
        "source_text, source_hash, target_text, machine_text, status, "
        "origin, aligned) VALUES (?, ?, ?, '原', 'h', 'T', 'T', ?, 'llm', 1)",
        (novel_id, chapter_id, seg_index, status),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_delete_counts_include_cat_segments_total_and_human():
    from backend.db import open_conn
    from backend.services.soft_delete import delete_counts

    _setup_db()
    novel_id = _insert_novel()
    ch = _insert_chapter(novel_id, 1)
    _insert_segment(novel_id, ch, 0, "machine")
    _insert_segment(novel_id, ch, 1, "edited")
    _insert_segment(novel_id, ch, 2, "confirmed")

    async with open_conn() as conn:
        counts = await delete_counts(conn, novel_id)
    assert counts.chapter_segments == 3
    assert counts.chapter_segments_human == 2


def test_delete_counts_route_exposes_segment_fields(client):
    novel_id = _insert_novel()
    ch = _insert_chapter(novel_id, 1)
    _insert_segment(novel_id, ch, 0, "confirmed")

    resp = client.get(f"/api/novels/{novel_id}/delete-counts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["chapter_segments"] == 1
    assert body["chapter_segments_human"] == 1
