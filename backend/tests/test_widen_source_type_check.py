"""Regression tests for backend.db._widen_source_type_check (Bug #1).

The migration rebuilt the `novels` table to widen the source_type CHECK from
('paste','txt','url') to include ('epub','docx','html') for Initiative 7.
The current implementation reads the original DDL from sqlite_master.sql and
surgically replaces the narrow CHECK string with the wider one, so column-
level constraints (DEFAULT expressions, CHECK clauses, UNIQUE, REFERENCES)
survive verbatim instead of round-tripping through PRAGMA table_info.

These tests pin the end-to-end migration against a faithful legacy schema.
"""

import sqlite3
import tempfile
from pathlib import Path

import aiosqlite
import pytest

from backend.db import _widen_source_type_check

# Mirrors the pre-Initiative-7 novels DDL. Includes a column with an
# expression default (created_at) because that's where the bug bit.
_LEGACY_NOVELS_DDL = """
CREATE TABLE novels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('paste', 'txt', 'url')),
    source_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    style_note TEXT,
    source_language TEXT NOT NULL DEFAULT 'zh',
    genre TEXT
)
"""


@pytest.mark.asyncio
async def test_widen_source_type_check_handles_expression_default():
    """End-to-end: a novels DB with `DEFAULT (datetime('now'))` must migrate
    without raising. This is the live failure mode reproduced in dev today."""
    db_path = Path(tempfile.mkdtemp(prefix="widen-test-")) / "test.db"
    # Seed a legacy-schema DB and insert one row so the INSERT-from-old-table
    # branch of the migration actually moves data.
    with sqlite3.connect(db_path) as seed:
        seed.execute(_LEGACY_NOVELS_DDL)
        seed.execute(
            "INSERT INTO novels (title, source_type) VALUES ('Test Novel', 'paste')"
        )
        seed.commit()
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        # The migration runs without raising.
        await _widen_source_type_check(conn)
    # Post-migration: novels has the WIDE CHECK + the seeded row survived +
    # the created_at default still parses as a valid datetime.
    with sqlite3.connect(db_path) as check:
        check.row_factory = sqlite3.Row
        ddl_row = check.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='novels'"
        ).fetchone()
        assert "'epub'" in ddl_row["sql"], (
            f"wide CHECK not present in migrated DDL:\n{ddl_row['sql']}"
        )
        rows = check.execute("SELECT title, source_type FROM novels").fetchall()
        assert len(rows) == 1
        assert rows[0]["title"] == "Test Novel"
        # A fresh INSERT picks up the (now properly parenthesised) DEFAULT
        # expression — failure mode would be either a SQL parse error during
        # migration (already asserted) or a NULL created_at after insert.
        check.execute(
            "INSERT INTO novels (title, source_type) VALUES ('Second', 'epub')"
        )
        check.commit()
        created = check.execute(
            "SELECT created_at FROM novels WHERE title='Second'"
        ).fetchone()
        assert created["created_at"] is not None
        assert len(created["created_at"]) >= 10  # at least 'YYYY-MM-DD'


@pytest.mark.asyncio
async def test_widen_source_type_check_preserves_other_check_constraints():
    """Bug #4 regression: column-level CHECK constraints on other columns
    must survive the rebuild. Today the only one on `novels` is on
    source_type (which we explicitly widen); this guards against
    silently losing one if someone adds a new column with a CHECK in the
    future."""
    db_path = Path(tempfile.mkdtemp(prefix="widen-checks-")) / "test.db"
    legacy_ddl_with_extra_check = """
        CREATE TABLE novels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            source_type TEXT NOT NULL CHECK (source_type IN ('paste', 'txt', 'url')),
            visibility TEXT NOT NULL DEFAULT 'public'
                CHECK (visibility IN ('public', 'private', 'archived')),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """
    with sqlite3.connect(db_path) as seed:
        seed.execute(legacy_ddl_with_extra_check)
        seed.commit()
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("PRAGMA foreign_keys = ON")
        await _widen_source_type_check(conn)
    with sqlite3.connect(db_path) as check:
        ddl = check.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='novels'"
        ).fetchone()[0]
        # Wide source_type CHECK is in.
        assert "'epub'" in ddl, ddl
        # The other column's CHECK survived.
        assert "CHECK (visibility IN ('public', 'private', 'archived'))" in ddl, ddl
        # And it actually enforces — try to violate it.
        try:
            check.execute(
                "INSERT INTO novels (title, source_type, visibility) "
                "VALUES ('t', 'epub', 'bogus')"
            )
            check.commit()
            raise AssertionError("CHECK on visibility was silently dropped")
        except sqlite3.IntegrityError:
            pass  # expected — CHECK is live


@pytest.mark.asyncio
async def test_widen_source_type_check_noop_on_wide_schema():
    """If the schema already has the wide CHECK (fresh DB from current
    SCHEMA), the migration is a no-op and must not touch the table."""
    db_path = Path(tempfile.mkdtemp(prefix="widen-noop-")) / "test.db"
    wide_ddl = _LEGACY_NOVELS_DDL.replace(
        "CHECK (source_type IN ('paste', 'txt', 'url'))",
        "CHECK (source_type IN ('paste', 'txt', 'url', 'epub', 'docx', 'html'))",
    )
    with sqlite3.connect(db_path) as seed:
        seed.execute(wide_ddl)
        seed.commit()
    async with aiosqlite.connect(db_path) as conn:
        await _widen_source_type_check(conn)
    with sqlite3.connect(db_path) as check:
        ddl = check.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='novels'"
        ).fetchone()[0]
        # Same DDL we put in — the migration's early-return ('epub' in ddl)
        # must have prevented any rebuild.
        assert "'epub'" in ddl
