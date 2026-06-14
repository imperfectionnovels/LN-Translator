"""Tests for the house-lexicon seed loader (translation-review fix 2026-06-14).

`backend.scripts.load_house_lexicon` seeds a few culturally-loaded loan-concepts
into the GLOBAL glossary so the translator stops flattening them across novels.
The load must be idempotent and must never clobber a user edit.
"""

from __future__ import annotations

import sqlite3

from backend.scripts.load_house_lexicon import HOUSE_LEXICON, load

# The committed DDL for global_glossary_entries (backend/db.py). Created inline
# so the test owns its schema and does not depend on full DB init.
_GLOBAL_GLOSSARY_DDL = """
CREATE TABLE IF NOT EXISTS global_glossary_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_zh TEXT NOT NULL UNIQUE,
    term_en TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    notes TEXT,
    usage_note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_VALID_CATEGORIES = {"character", "technique", "item", "place", "other", "idiom"}


def _make_db(tmp_path) -> str:
    db = tmp_path / "house_lexicon_test.db"
    conn = sqlite3.connect(db)
    conn.executescript(_GLOBAL_GLOSSARY_DDL)
    conn.commit()
    conn.close()
    return str(db)


class TestHouseLexiconConstant:
    def test_mianzi_renders_face(self):
        by_zh = {zh: en for zh, en, _cat, _notes in HOUSE_LEXICON}
        assert by_zh["面子"] == "face"

    def test_all_categories_valid(self):
        for _zh, _en, cat, _notes in HOUSE_LEXICON:
            assert cat in _VALID_CATEGORIES

    def test_no_duplicate_term_zh(self):
        zhs = [zh for zh, *_ in HOUSE_LEXICON]
        assert len(zhs) == len(set(zhs))

    def test_all_fields_non_empty(self):
        for row in HOUSE_LEXICON:
            assert len(row) == 4
            for field in row:
                assert isinstance(field, str) and field.strip()


class TestHouseLexiconLoad:
    def test_load_inserts_all_rows(self, tmp_path):
        db = _make_db(tmp_path)
        inserted, skipped = load(db_path=db)
        assert inserted == len(HOUSE_LEXICON)
        assert skipped == 0
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT term_en, category FROM global_glossary_entries "
            "WHERE term_zh = ?",
            ("面子",),
        ).fetchone()
        conn.close()
        assert row == ("face", "idiom")

    def test_second_load_is_idempotent(self, tmp_path):
        db = _make_db(tmp_path)
        load(db_path=db)
        inserted, skipped = load(db_path=db)
        assert inserted == 0
        assert skipped == len(HOUSE_LEXICON)

    def test_reload_does_not_clobber_user_edit(self, tmp_path):
        db = _make_db(tmp_path)
        load(db_path=db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE global_glossary_entries SET term_en = ? WHERE term_zh = ?",
            ("prestige (user override)", "面子"),
        )
        conn.commit()
        conn.close()
        # Re-running the loader must NOT overwrite the user's edit.
        load(db_path=db)
        conn = sqlite3.connect(db)
        term_en = conn.execute(
            "SELECT term_en FROM global_glossary_entries WHERE term_zh = ?",
            ("面子",),
        ).fetchone()[0]
        conn.close()
        assert term_en == "prestige (user override)"
