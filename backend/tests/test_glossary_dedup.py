"""Tests for simplified/traditional glossary de-duplication.

The glossary keys uniqueness on the literal Chinese `term_zh`, so the same
name written in two Han scripts (索喚 traditional / 索唤 simplified) would land
as two rows. `canonical_zh` folds the scripts; `merge_new_terms` and
`create_or_overwrite_entry` use it to refuse the duplicate, and
`filter_glossary_for_chapter` uses it so a script-mismatched entry still
reaches the prompt.
"""

import os
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from backend.db import SCHEMA
from backend.models import GlossaryEntry, NewTerm
from backend.services import glossary as g

DB_PATH = Path(os.environ["DB_PATH"])

# 索喚 (traditional 喚) and 索唤 (simplified 唤) — the same character name.
TRAD = "索喚"
SIMP = "索唤"


def _reset_db() -> None:
    """Fresh DB with the schema and one novel (id=1)."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO novels (id, title, source_type) VALUES (1, 'Test', 'paste')"
    )
    conn.commit()
    conn.close()


def _seed_entry(term_zh: str, term_en: str, *, locked: int, auto: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO glossary_entries "
        "(novel_id, term_zh, term_en, category, auto_detected, locked) "
        "VALUES (1, ?, ?, 'character', ?, ?)",
        (term_zh, term_en, auto, locked),
    )
    conn.commit()
    conn.close()


def _glossary_rows() -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                "SELECT term_zh, term_en FROM glossary_entries WHERE novel_id = 1"
            ).fetchall()
        )
    finally:
        conn.close()


# --- canonical_zh ----------------------------------------------------------

def test_canonical_zh_folds_traditional_to_simplified() -> None:
    assert g.canonical_zh(TRAD) == g.canonical_zh(SIMP)
    assert g.canonical_zh("老龍君") == g.canonical_zh("老龙君")
    assert g.canonical_zh("知見障") == g.canonical_zh("知见障")


def test_canonical_zh_strips_invisible_chars() -> None:
    assert g.canonical_zh("﻿" + SIMP + "​") == g.canonical_zh(SIMP)


def test_canonical_zh_distinguishes_genuinely_different_terms() -> None:
    # 妖兽 / 魔兽 are different words that happen to share an English gloss —
    # they must NOT fold together.
    assert g.canonical_zh("妖兽") != g.canonical_zh("魔兽")


# --- merge_new_terms (auto-extraction) -------------------------------------

async def test_merge_skips_traditional_simplified_variant() -> None:
    _reset_db()
    _seed_entry(TRAD, "Suo Huan", locked=1, auto=0)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await g.merge_new_terms(
            conn, 1, [NewTerm(zh=SIMP, en="Suo Huan", category="character")]
        )
    rows = _glossary_rows()
    assert len(rows) == 1, "script variant must not create a second row"
    assert rows[0]["term_zh"] == TRAD, "the locked entry stays untouched"


async def test_merge_still_inserts_genuinely_new_term() -> None:
    _reset_db()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await g.merge_new_terms(
            conn, 1, [NewTerm(zh="新词", en="New Word", category="other")]
        )
    assert len(_glossary_rows()) == 1


async def test_merge_dedups_two_variants_within_one_batch() -> None:
    _reset_db()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await g.merge_new_terms(
            conn,
            1,
            [
                NewTerm(zh=SIMP, en="Suo Huan", category="character"),
                NewTerm(zh=TRAD, en="Suo Huan", category="character"),
            ],
        )
    assert len(_glossary_rows()) == 1


# --- create_or_overwrite_entry (manual add) --------------------------------

async def test_manual_add_of_variant_conflicts_with_locked_entry() -> None:
    _reset_db()
    _seed_entry(TRAD, "Suo Huan", locked=1, auto=0)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        with pytest.raises(g.LockedEntryConflict):
            await g.create_or_overwrite_entry(
                conn, 1, SIMP, "Suo Huan", "character", None
            )
    assert len(_glossary_rows()) == 1


async def test_manual_add_of_variant_overwrites_unlocked_entry() -> None:
    _reset_db()
    _seed_entry(TRAD, "Suo Huan", locked=0, auto=1)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        entry = await g.create_or_overwrite_entry(
            conn, 1, SIMP, "Suo Huan (fixed)", "character", None
        )
    rows = _glossary_rows()
    assert len(rows) == 1, "variant overwrites the unlocked row, no new row"
    assert rows[0]["term_en"] == "Suo Huan (fixed)"
    assert entry.locked is True


# --- filter_glossary_for_chapter -------------------------------------------

def test_filter_glossary_matches_across_han_scripts() -> None:
    entry = GlossaryEntry(
        id=1,
        novel_id=1,
        term_zh=TRAD,  # locked entry is traditional
        term_en="Suo Huan",
        category="character",
        notes=None,
        auto_detected=False,
        locked=True,
    )
    chapter_simplified = f"这一日，{SIMP}走进了大殿之中。"
    kept = g.filter_glossary_for_chapter([entry], chapter_simplified)
    assert kept == [entry], "traditional entry must match a simplified chapter"
