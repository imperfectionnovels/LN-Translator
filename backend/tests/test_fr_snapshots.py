"""Tests for find/replace commit snapshots + restore.

Invariants exercised:
- record_snapshot writes a row with the JSON payload
- oversized payloads skip recording without raising
- list_for_novel returns newest first
- restore_snapshot replays the payload onto chapters
- restore on an already-restored snapshot is 409
- purge_old_snapshots removes only rows past retention
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from backend.db import SCHEMA, _ADDITIVE_MIGRATIONS

DB_PATH = Path(os.environ["DB_PATH"])


def _setup_db() -> None:
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


def _insert_novel() -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO novels (title, source_type) VALUES ('Novel A', 'paste')",
    )
    conn.commit()
    novel_id = cur.lastrowid
    conn.close()
    return novel_id


def _insert_chapter(novel_id: int, num: int, translated: str | None) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO chapters (novel_id, chapter_num, original_text, "
        "translated_text, status) VALUES (?, ?, '...', ?, 'done')",
        (novel_id, num, translated),
    )
    conn.commit()
    chapter_id = cur.lastrowid
    conn.close()
    return chapter_id


@pytest.mark.asyncio
async def test_record_then_list_round_trip():
    from backend.db import open_conn
    from backend.services.fr_snapshots import list_for_novel, record_snapshot

    _setup_db()
    novel_id = _insert_novel()

    async with open_conn() as conn:
        snap_id = await record_snapshot(
            conn,
            novel_id=novel_id,
            commit_token="tok-1",
            find_pattern="Sword",
            replace_pattern="Blade",
            target="translated_text",
            scope="novel",
            chapters_changed=2,
            payload={"1": {"translated_before": "Sword"}},
        )
        await conn.commit()
    assert snap_id is not None

    async with open_conn() as conn:
        snapshots = await list_for_novel(conn, novel_id)
    assert len(snapshots) == 1
    assert snapshots[0].commit_token == "tok-1"
    assert snapshots[0].chapters_changed == 2
    assert snapshots[0].restored_at is None


@pytest.mark.asyncio
async def test_oversized_payload_skips_recording():
    from backend.db import open_conn
    from backend.services.fr_snapshots import (
        SNAPSHOT_PAYLOAD_BYTE_CAP,
        list_for_novel,
        record_snapshot,
    )

    _setup_db()
    novel_id = _insert_novel()

    # Build a payload that exceeds the cap.
    big_body = "A" * (SNAPSHOT_PAYLOAD_BYTE_CAP + 1000)
    payload = {"1": {"translated_before": big_body}}

    async with open_conn() as conn:
        snap_id = await record_snapshot(
            conn,
            novel_id=novel_id,
            commit_token="tok-big",
            find_pattern="x",
            replace_pattern="y",
            target="translated_text",
            scope="novel",
            chapters_changed=1,
            payload=payload,
        )
        await conn.commit()
    assert snap_id is None

    async with open_conn() as conn:
        snapshots = await list_for_novel(conn, novel_id)
    assert snapshots == []


@pytest.mark.asyncio
async def test_restore_replays_payload_onto_chapters():
    from backend.db import open_conn
    from backend.services.fr_snapshots import record_snapshot, restore_snapshot

    _setup_db()
    novel_id = _insert_novel()
    ch1 = _insert_chapter(novel_id, 1, "Blade arrived.")
    ch2 = _insert_chapter(novel_id, 2, "More Blade.")

    payload = {
        str(ch1): {"translated_before": "Sword arrived."},
        str(ch2): {"translated_before": "More Sword."},
    }

    async with open_conn() as conn:
        snap_id = await record_snapshot(
            conn,
            novel_id=novel_id,
            commit_token="tok-1",
            find_pattern="Sword",
            replace_pattern="Blade",
            target="translated_text",
            scope="novel",
            chapters_changed=2,
            payload=payload,
        )
        await conn.commit()

    async with open_conn() as conn:
        result = await restore_snapshot(conn, snap_id)
    assert result["chapters_restored"] == 2

    conn_sync = sqlite3.connect(DB_PATH)
    rows = conn_sync.execute(
        "SELECT chapter_num, translated_text FROM chapters "
        "WHERE novel_id = ? ORDER BY chapter_num", (novel_id,),
    ).fetchall()
    conn_sync.close()

    assert rows[0][1] == "Sword arrived."
    assert rows[1][1] == "More Sword."


@pytest.mark.asyncio
async def test_double_restore_returns_409():
    from fastapi import HTTPException

    from backend.db import open_conn
    from backend.services.fr_snapshots import record_snapshot, restore_snapshot

    _setup_db()
    novel_id = _insert_novel()
    ch1 = _insert_chapter(novel_id, 1, "Blade.")

    async with open_conn() as conn:
        snap_id = await record_snapshot(
            conn,
            novel_id=novel_id,
            commit_token="tok-1",
            find_pattern="Sword",
            replace_pattern="Blade",
            target="translated_text",
            scope="novel",
            chapters_changed=1,
            payload={str(ch1): {"translated_before": "Sword."}},
        )
        await conn.commit()

    async with open_conn() as conn:
        await restore_snapshot(conn, snap_id)

    async with open_conn() as conn:
        with pytest.raises(HTTPException) as exc_info:
            await restore_snapshot(conn, snap_id)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_restore_not_found_returns_404():
    from fastapi import HTTPException

    from backend.db import open_conn
    from backend.services.fr_snapshots import restore_snapshot

    _setup_db()
    async with open_conn() as conn:
        with pytest.raises(HTTPException) as exc_info:
            await restore_snapshot(conn, 9999)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_purge_old_snapshots_removes_past_retention():
    """Insert a snapshot with an artificially old committed_at; verify
    the retention sweep removes it but leaves a fresh one alone."""
    from backend.db import open_conn
    from backend.services.fr_snapshots import (
        RETENTION_DAYS,
        purge_old_snapshots,
        record_snapshot,
    )

    _setup_db()
    novel_id = _insert_novel()

    # Fresh snapshot.
    async with open_conn() as conn:
        fresh_id = await record_snapshot(
            conn,
            novel_id=novel_id,
            commit_token="fresh",
            find_pattern="x",
            replace_pattern="y",
            target="translated_text",
            scope="novel",
            chapters_changed=0,
            payload={},
        )
        await conn.commit()

    # Insert one manually with an old committed_at.
    conn_sync = sqlite3.connect(DB_PATH)
    cur = conn_sync.execute(
        f"""
        INSERT INTO find_replace_snapshots
            (novel_id, commit_token, find_pattern, replace_pattern,
             target, scope, chapters_changed, payload_json, committed_at)
        VALUES (?, 'old', 'x', 'y', 'translated_text', 'novel', 0, '{{}}',
                datetime('now', '-{RETENTION_DAYS + 5} days'))
        """,
        (novel_id,),
    )
    old_id = cur.lastrowid
    conn_sync.commit()
    conn_sync.close()

    async with open_conn() as conn:
        purged = await purge_old_snapshots(conn)
    assert purged == 1

    conn_sync = sqlite3.connect(DB_PATH)
    remaining = {
        r[0] for r in conn_sync.execute(
            "SELECT id FROM find_replace_snapshots",
        ).fetchall()
    }
    conn_sync.close()
    assert fresh_id in remaining
    assert old_id not in remaining
