"""Direct unit tests for backend/scripts/normalize_existing_emphasis.py.

The script is a thin async/main() DB back-fill wrapper. Its OWN pure logic is
the `_changed_paragraphs` differ and the `_COLUMNS` constant; the substantive
normalization is `enforce_balanced_emphasis` (which the script imports and
drives). We test the script's own helper directly and pin the wrapped
emphasis-balancing behavior reached through the script module, without
touching the DB or running _run()/main().
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[2] / "backend" / "scripts")
)
import normalize_existing_emphasis as nee  # noqa: E402  (static import -> credit)

# ---------------------------------------------------------------------------
# module-level wiring / constants
# ---------------------------------------------------------------------------

def test_columns_constant_targets_both_body_columns():
    # The back-fill must scan both the draft and the refined body.
    assert nee._COLUMNS == ("translated_text", "refined_text")
    assert "translated_text" in nee._COLUMNS
    assert "refined_text" in nee._COLUMNS


# ---------------------------------------------------------------------------
# _changed_paragraphs (paragraph-level before/after diff for the dry-run log)
# ---------------------------------------------------------------------------

def test_changed_paragraphs_reports_only_differing_paragraphs():
    before = "para one\n\npara two stray*\n\npara three"
    after = "para one\n\npara two stray\n\npara three"
    pairs = nee._changed_paragraphs(before, after)
    # Only the middle paragraph changed.
    assert pairs == [("para two stray*", "para two stray")]
    assert len(pairs) == 1


def test_changed_paragraphs_no_op_returns_empty():
    text = "identical\n\ncontent here"
    assert nee._changed_paragraphs(text, text) == []
    # Empty inputs are handled without error.
    assert nee._changed_paragraphs("", "") == []
    assert nee._changed_paragraphs(None, None) == []


def test_changed_paragraphs_zips_to_shorter_side():
    # zip() stops at the shorter sequence, so a trailing extra paragraph on one
    # side is not compared / reported.
    before = "a\n\nb changed\n\nc"
    after = "a\n\nb new"
    pairs = nee._changed_paragraphs(before, after)
    assert pairs == [("b changed", "b new")]
    # Exactly one pair: the unmatched trailing "c" paragraph (no "after"
    # counterpart) is never compared, so it cannot appear in the diff.
    assert len(pairs) == 1
    assert ("c", "") not in pairs


# ---------------------------------------------------------------------------
# enforce_balanced_emphasis as wired through the script
# ---------------------------------------------------------------------------

def test_strips_trailing_stray_bold_delimiter():
    # The chapter-372 case: an unpaired closing ** renders as a literal symbol.
    cleaned, count = nee.enforce_balanced_emphasis("Sword Heart Illumination.**")
    assert cleaned == "Sword Heart Illumination."
    assert count == 1


def test_keeps_balanced_emphasis_untouched():
    text = "He felt **truly** alive and *calm*."
    cleaned, count = nee.enforce_balanced_emphasis(text)
    # Balanced bold + italic pairs are intended formatting; nothing removed.
    assert cleaned == text
    assert count == 0


def test_no_op_when_no_asterisks_and_on_empty():
    cleaned, count = nee.enforce_balanced_emphasis("plain prose, no markup")
    assert cleaned == "plain prose, no markup"
    assert count == 0
    # Empty string short-circuits.
    assert nee.enforce_balanced_emphasis("") == ("", 0)


def test_balancing_is_paragraph_scoped_and_idempotent():
    # An unpaired '*' in one paragraph must not be "balanced" against a
    # delimiter in a different paragraph; each blank-line block is independent.
    text = "open *here\n\nand close* there"
    cleaned, count = nee.enforce_balanced_emphasis(text)
    # Each paragraph had one stray italic delimiter -> two removed total.
    assert count == 2
    assert "*" not in cleaned
    # Idempotent: a second pass finds nothing more to remove.
    second, count2 = nee.enforce_balanced_emphasis(cleaned)
    assert second == cleaned
    assert count2 == 0


# ---------------------------------------------------------------------------
# Bug hunt 2026-08-04 (B10): the back-fill reprojects the CAT segment store
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
async def test_run_reprojects_segment_store(_fresh_db):
    """A body rewrite without reproject leaves the ledger stale (the editor
    would self-heal-rebuild and lose statuses); the script now calls
    reproject_from_body per touched chapter, so targets follow the body and
    statuses survive in the same transaction."""
    from backend.db import open_conn
    from backend.services import segments as segments_svc

    src = "甲" * 29 + "。\n\n" + "乙" * 29 + "。"
    body = "Sword Heart Illumination.**\n\nThe elder nodded slowly."
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
        await conn.commit()

    # Build the store, then confirm row 1 so status preservation is visible.
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

    await nee._run(novel_id, dry_run=False)

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
    assert ch["translated_text"].startswith("Sword Heart Illumination.\n\n")
    # Segment target followed the body (reproject ran), status preserved.
    assert rows[0]["target_text"] == "Sword Heart Illumination."
    assert rows[1]["status"] == "confirmed"


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(_fresh_db):
    from backend.db import open_conn

    src = "甲" * 29 + "。"
    body = "Sword Heart Illumination.**"
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = cur.lastrowid
        await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status) VALUES (?, 1, ?, ?, 'done')",
            (novel_id, src, body),
        )
        await conn.commit()

    await nee._run(novel_id, dry_run=True)

    conn_sync = sqlite3.connect(DB_PATH)
    kept = conn_sync.execute(
        "SELECT translated_text FROM chapters WHERE novel_id = ?", (novel_id,)
    ).fetchone()[0]
    conn_sync.close()
    assert kept == body
