"""Schema test for the free-tier mechanical-NMT columns added in 2026-05-26.

Pins the chapters-table additions for the free-draft worker + LLM PEMT path:

    * free_draft_text
    * free_draft_status      (CHECK in {'none','pending','in_progress','done','error'},
                              NOT NULL DEFAULT 'none')
    * free_draft_error
    * free_draft_completed_at
    * translated_by_provider_id   (REFERENCES providers(id) ON DELETE SET NULL)

Verifies both that init_db produces a chapters table with these columns AND
that the _ADDITIVE_MIGRATIONS list grows the columns onto a legacy DB.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from backend.db import _ADDITIVE_MIGRATIONS, init_db

_EXPECTED_COLUMNS: dict[str, dict[str, object]] = {
    "free_draft_text": {"type": "TEXT", "notnull": 0, "default": None},
    "free_draft_status": {"type": "TEXT", "notnull": 1, "default": "'none'"},
    "free_draft_error": {"type": "TEXT", "notnull": 0, "default": None},
    "free_draft_completed_at": {"type": "TEXT", "notnull": 0, "default": None},
    "translated_by_provider_id": {"type": "INTEGER", "notnull": 0, "default": None},
}


def _chapter_column_map(db_path: Path) -> dict[str, dict[str, object]]:
    """Return {col_name: {type, notnull, dflt_value}} for the chapters table."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("PRAGMA table_info(chapters)")
        rows = cur.fetchall()
    return {
        row[1]: {"type": row[2], "notnull": row[3], "default": row[4]}
        for row in rows
    }


@pytest.mark.asyncio
async def test_fresh_init_db_has_free_draft_columns(tmp_path, monkeypatch):
    """init_db on a fresh DB file creates the chapters table with the new columns."""
    db_path = tmp_path / "fresh.db"
    # backend.db imports DB_PATH from config at module load (`from backend.config
    # import DB_PATH`), so we must patch the bound name on backend.db itself —
    # patching backend.config.DB_PATH alone is a no-op for db.init_db.
    from backend import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path, raising=True)

    await init_db()

    columns = _chapter_column_map(db_path)
    for col, expected in _EXPECTED_COLUMNS.items():
        assert col in columns, f"chapters.{col} missing after init_db"
        actual = columns[col]
        assert actual["type"] == expected["type"], (
            f"chapters.{col} type {actual['type']!r} != {expected['type']!r}"
        )
        assert actual["notnull"] == expected["notnull"], (
            f"chapters.{col} notnull {actual['notnull']} != {expected['notnull']}"
        )
        if expected["default"] is None:
            assert actual["default"] is None, (
                f"chapters.{col} default {actual['default']!r} != NULL"
            )
        else:
            assert actual["default"] == expected["default"], (
                f"chapters.{col} default {actual['default']!r} != {expected['default']!r}"
            )


def test_free_draft_status_check_constraint_present():
    """The chapters CREATE TABLE source-of-truth must constrain free_draft_status.

    Reads the SCHEMA string directly so a future drift on the column-level
    CHECK lights up here, not at runtime when a stray value lands in the DB.
    """
    from backend.db import SCHEMA

    # The CHECK is on the canonical CREATE TABLE only (ALTER TABLE in SQLite
    # cannot add CHECK constraints to an existing column), so we grep the
    # schema string instead of relying on PRAGMA.
    assert "free_draft_status" in SCHEMA
    assert (
        "free_draft_status IN ('none', 'pending', 'in_progress', 'done', 'error')"
        in SCHEMA
    )


def test_additive_migrations_extend_legacy_chapters_table():
    """A legacy chapters table without the new columns gains them via the migration list."""
    db_path = Path(tempfile.mkdtemp(prefix="free-draft-mig-")) / "legacy.db"
    with sqlite3.connect(db_path) as seed:
        # Minimal legacy chapters schema — only the few columns we need to
        # exercise the ADD COLUMN path. Real production DBs have more
        # columns; the ADD COLUMN statements don't care.
        seed.execute(
            """
            CREATE TABLE chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id INTEGER NOT NULL,
                chapter_num INTEGER NOT NULL,
                original_text TEXT NOT NULL,
                translated_text TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        seed.execute(
            """
            CREATE TABLE providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL
            )
            """
        )
        seed.commit()

        # Replay only the migrations relevant to the new columns. We grep the
        # full list to avoid coupling this test to the exact migration index.
        for stmt in _ADDITIVE_MIGRATIONS:
            if "free_draft" in stmt or "translated_by_provider_id" in stmt:
                seed.execute(stmt)
        seed.commit()

    columns = _chapter_column_map(db_path)
    for col in _EXPECTED_COLUMNS:
        assert col in columns, f"chapters.{col} missing after running migrations"

    # Spot-check the FK exists on translated_by_provider_id by reading the
    # full DDL — PRAGMA table_info doesn't surface REFERENCES.
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chapters'"
        )
        ddl = cur.fetchone()[0]
    assert "translated_by_provider_id INTEGER REFERENCES providers(id)" in ddl
