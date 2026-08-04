"""Unit tests for the casing re-apply tool's pure diff helpers."""

from __future__ import annotations

from backend.scripts.reapply_term_casing import _word_pair, diff_runs


def test_diff_runs_finds_casing_changes():
    old = "Dao of dual cultivation"
    new = "Dao of Dual Cultivation"
    runs = diff_runs(old, new)
    # Two single-char flips: 'd'->'D' and 'c'->'C'.
    assert runs == [(7, 8), (12, 13)]


def test_diff_runs_empty_when_identical():
    assert diff_runs("same text", "same text") == []


def test_diff_runs_guards_length_mismatch():
    assert diff_runs("abc", "abcd") == []


def test_word_pair_expands_to_whole_word():
    old = "Dao of dual cultivation"
    new = "Dao of Dual Cultivation"
    # The diff at index 7 ('d'->'D') should expand to the whole word.
    assert _word_pair(old, new, 7, 8) == ("dual", "Dual")
    # The diff at index 12 ('c'->'C') -> "cultivation"/"Cultivation".
    assert _word_pair(old, new, 12, 13) == ("cultivation", "Cultivation")


# ---------------------------------------------------------------------------
# Bug hunt 2026-08-04 (B10): --apply reprojects the CAT segment store
# ---------------------------------------------------------------------------

import os  # noqa: E402
import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

DB_PATH = Path(os.environ["DB_PATH"])


def _unlink_db_trio() -> None:
    # Remove -wal/-shm alongside the main file: a stale WAL next to a
    # recreated DB reads as "database disk image is malformed" in whichever
    # module opens it next.
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()


@pytest.fixture
def _fresh_db():
    from backend.db import SCHEMA
    _unlink_db_trio()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    yield
    _unlink_db_trio()


@pytest.mark.asyncio
async def test_apply_reprojects_segment_store(_fresh_db, monkeypatch, tmp_path):
    """The casing rewrite is a text-authoritative mutation: without the
    reproject hook the ledger keeps the old casing and the editor's
    self-heal would rebuild (dropping statuses). The script now re-syncs
    per touched chapter, status-preserving."""
    from backend.db import open_conn
    from backend.scripts import reapply_term_casing as rtc
    from backend.services import segments as segments_svc

    # Keep the before-image backup out of the real data dir.
    monkeypatch.setattr(rtc, "USER_DATA_ROOT", tmp_path)

    src = "甲" * 29 + "。\n\n" + "乙" * 29 + "。"
    body = "His golden core trembled.\n\nThe elder nodded slowly."
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status) VALUES (?, 1, ?, ?, 'done')",
            (novel_id, src, body),
        )
        chapter_id = cur.lastrowid
        await conn.execute(
            "INSERT INTO glossary_entries (novel_id, term_zh, term_en, "
            "category, auto_detected, locked) "
            "VALUES (?, '金丹', 'Golden Core', 'other', 0, 1)",
            (novel_id,),
        )
        await conn.commit()

    # Build the store and confirm row 1.
    async with open_conn() as conn:
        payload = await segments_svc.get_segments(conn, novel_id, 1)
        await conn.commit()
    seg1 = next(s for s in payload["segments"] if s["index"] == 1)
    async with open_conn() as conn:
        await segments_svc.update_segment(
            conn, novel_id, 1, 1, action="confirm", after_text=None,
            client_rev=payload["chapter_rev"],
            before_target_hash=seg1["target_hash"],
        )
        await conn.commit()

    await rtc._run(novel_id, apply=True, assume_yes=True)

    conn_sync = sqlite3.connect(DB_PATH)
    conn_sync.row_factory = sqlite3.Row
    ch = conn_sync.execute(
        "SELECT translated_text FROM chapters WHERE id = ?", (chapter_id,)
    ).fetchone()
    rows = conn_sync.execute(
        "SELECT target_text, status FROM chapter_segments "
        "WHERE chapter_id = ? ORDER BY seg_index",
        (chapter_id,),
    ).fetchall()
    conn_sync.close()
    assert ch["translated_text"].startswith("His Golden Core trembled.")
    # The ledger followed the recased body; the confirmed row survived.
    assert rows[0]["target_text"] == "His Golden Core trembled."
    assert rows[1]["status"] == "confirmed"
