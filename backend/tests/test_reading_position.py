"""Tests for the durable per-novel reading position (2026-05-28).

Reading position used to live only in browser localStorage, which WebView2
discards whenever app_entry's private_mode fallback fires (and which is also
subject to cache eviction). These tests pin the SQLite-backed replacement so
reopening the app resumes on the last-read chapter instead of chapter 1:

  * novels.last_read_chapter_num  (INTEGER, nullable)
  * novels.last_read_at           (TEXT, nullable — drives the library
                                    "Continue reading" sort)
  * PUT /api/novels/{id}/reading-position  (the write path)
  * GET /api/novels and /api/novels/{id} surface both columns

Covers: fresh-init schema, additive migration onto a legacy DB, the
_drop_dead_columns humanizer-era rebuild carry-forward, the endpoint
round-trip + validation, and PATCH isolation (a rename must not clobber the
position).
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import _ADDITIVE_MIGRATIONS, init_db, open_conn


_EXPECTED_COLUMNS: dict[str, dict[str, object]] = {
    "last_read_chapter_num": {"type": "INTEGER", "notnull": 0, "default": None},
    "last_read_at": {"type": "TEXT", "notnull": 0, "default": None},
}


def _novel_column_map(db_path: Path) -> dict[str, dict[str, object]]:
    """Return {col_name: {type, notnull, default}} for the novels table."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("PRAGMA table_info(novels)")
        rows = cur.fetchall()
    return {
        row[1]: {"type": row[2], "notnull": row[3], "default": row[4]}
        for row in rows
    }


# ----- fixtures -----

@pytest.fixture
def app_with_stubs(monkeypatch):
    """The FastAPI app with the startup probe + queue drain stubbed, so
    TestClient startup doesn't reach for a real translator backend."""
    async def _no_probe(default_provider):
        return None

    async def _no_drain():
        return None

    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)

    from backend.main import app
    return app


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point the app + db helpers at a fresh temp DB with a clean schema.

    The shared conftest DB accumulates schema drift across modules — e.g.
    test_providers' rebuild test (which runs earlier alphabetically) recreates
    novels without created_at's NOT NULL DEFAULT, so a paste afterward inserts
    a NULL created_at. Isolating here makes these endpoint tests deterministic
    regardless of run order. init_db is awaited in the test body."""
    db_path = tmp_path / "reading-pos.db"
    from backend import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path, raising=True)
    return db_path


# ----- schema -----

@pytest.mark.asyncio
async def test_fresh_init_db_has_reading_position_columns(tmp_path, monkeypatch):
    """init_db on a fresh DB creates novels with both reading-position columns,
    nullable and without a default."""
    db_path = tmp_path / "fresh.db"
    # backend.db binds DB_PATH at import; patch the bound name on the module.
    from backend import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path, raising=True)

    await init_db()

    columns = _novel_column_map(db_path)
    for col, expected in _EXPECTED_COLUMNS.items():
        assert col in columns, f"novels.{col} missing after init_db"
        actual = columns[col]
        assert actual["type"] == expected["type"], (
            f"novels.{col} type {actual['type']!r} != {expected['type']!r}"
        )
        assert actual["notnull"] == expected["notnull"], (
            f"novels.{col} notnull {actual['notnull']} != {expected['notnull']}"
        )
        assert actual["default"] is None, (
            f"novels.{col} default {actual['default']!r} != NULL"
        )


def test_additive_migrations_extend_legacy_novels_table():
    """A legacy novels table without the new columns gains them via the
    additive migration list."""
    db_path = Path(tempfile.mkdtemp(prefix="reading-pos-mig-")) / "legacy.db"
    with sqlite3.connect(db_path) as seed:
        seed.execute(
            """
            CREATE TABLE novels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source_type TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        seed.commit()
        # Replay only the reading-position migrations — grep the full list so
        # this test isn't coupled to the exact migration index.
        for stmt in _ADDITIVE_MIGRATIONS:
            if "last_read" in stmt:
                seed.execute(stmt)
        seed.commit()

    columns = _novel_column_map(db_path)
    for col in _EXPECTED_COLUMNS:
        assert col in columns, f"novels.{col} missing after running migrations"


@pytest.mark.asyncio
async def test_drop_dead_columns_preserves_reading_position(tmp_path, monkeypatch):
    """The one-time humanizer-era novels rebuild (_drop_dead_columns) must
    carry the reading position forward, not reset it to chapter 1.

    Runs against an isolated temp DB (patched DB_PATH): the full rebuild path
    drops and recreates the FTS5 index, and doing that inside the shared
    conftest DB leaves the shadow tables in the 'database disk image is
    malformed' state the db.py comments warn about — which would corrupt every
    subsequent test in the session. Isolation keeps the hazard contained.
    """
    db_path = tmp_path / "humanizer.db"
    from backend import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path, raising=True)

    async with open_conn() as conn:
        await conn.execute("PRAGMA foreign_keys = OFF")
        await conn.execute("DROP TABLE IF EXISTS chapter_fts")
        for shadow in (
            "chapter_fts_data", "chapter_fts_idx",
            "chapter_fts_docsize", "chapter_fts_config",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {shadow}")
        await conn.execute("DROP TABLE IF EXISTS chapters")
        await conn.execute("DROP TABLE IF EXISTS novels")
        # Humanizer-era novels table that ALSO already carries the
        # reading-position columns with a value — the rebuild must preserve it.
        await conn.execute(
            "CREATE TABLE novels (id INTEGER PRIMARY KEY, title TEXT, "
            "source_type TEXT, source_url TEXT, created_at TEXT, "
            "style_note TEXT, humanizer_tone TEXT, "
            "source_language TEXT NOT NULL DEFAULT 'zh', "
            "genre TEXT, custom_style_brief TEXT, "
            "translator_provider_id INTEGER, refinement_provider_id INTEGER, "
            "last_read_chapter_num INTEGER, last_read_at TEXT)"
        )
        await conn.execute(
            "CREATE TABLE chapters (id INTEGER PRIMARY KEY, novel_id INTEGER, "
            "chapter_num INTEGER, title_zh TEXT, title_en TEXT, "
            "original_text TEXT NOT NULL DEFAULT '', translated_text TEXT, "
            "status TEXT NOT NULL DEFAULT 'pending', error_msg TEXT, "
            "translate_queued INTEGER NOT NULL DEFAULT 0, "
            "force_retranslate INTEGER NOT NULL DEFAULT 0, "
            "translation_degraded INTEGER NOT NULL DEFAULT 0, "
            "glossary_merge_error TEXT, humanized_text TEXT, "
            "UNIQUE (novel_id, chapter_num))"
        )
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, source_url, created_at, "
            "humanizer_tone, last_read_chapter_num, last_read_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?)",
            ("resume-survivor", "paste", None, "2026-01-01",
             "scholarly", 300, "2026-05-28 10:00:00"),
        )
        survivor_id = cur.lastrowid
        # A chapter with humanized_text set is the sentinel that makes
        # _drop_dead_columns take the FULL rebuild path (chapters + novels),
        # not the cheap drop-column-and-return early-out. That's the path the
        # carry-forward of last_read_* has to survive.
        await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status, humanized_text) VALUES (?, ?, ?, ?, ?, ?)",
            (survivor_id, 300, "原文", "draft body", "done", "humanized body"),
        )
        await conn.commit()
        await conn.execute("PRAGMA foreign_keys = ON")

    # Trigger the cleanup pass; the humanized_text row forces the novels
    # rebuild, which must carry last_read_chapter_num / last_read_at forward.
    await init_db()

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT last_read_chapter_num, last_read_at FROM novels "
            "WHERE title = 'resume-survivor'"
        )
        row = await cur.fetchone()
    assert row is not None, "row was dropped during rebuild"
    assert row["last_read_chapter_num"] == 300, "reading position lost in rebuild"
    assert row["last_read_at"] == "2026-05-28 10:00:00"


# ----- endpoint -----

@pytest.mark.asyncio
async def test_reading_position_round_trip(app_with_stubs, isolated_db):
    """PUT records the position; GET (single + list) surfaces it with a
    server-stamped last_read_at."""
    await init_db()

    with TestClient(app_with_stubs) as client:
        novel_id = client.post(
            "/api/translate/paste",
            json={"title": "Resume Me", "text": "Chapter 1\n\nFoo."},
        ).json()["novel_id"]

        # Brand-new novel: no position yet.
        assert client.get(f"/api/novels/{novel_id}").json()["last_read_chapter_num"] is None

        resp = client.put(
            f"/api/novels/{novel_id}/reading-position",
            json={"chapter_num": 300},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["last_read_chapter_num"] == 300

        single = client.get(f"/api/novels/{novel_id}").json()
        assert single["last_read_chapter_num"] == 300
        assert single["last_read_at"] is not None

        listed = client.get("/api/novels").json()
        match = next(n for n in listed if n["id"] == novel_id)
        assert match["last_read_chapter_num"] == 300
        assert match["last_read_at"] is not None


@pytest.mark.asyncio
async def test_reading_position_validation(app_with_stubs, isolated_db):
    """chapter_num must be >= 1 (422); a missing novel is a 404."""
    await init_db()

    with TestClient(app_with_stubs) as client:
        novel_id = client.post(
            "/api/translate/paste",
            json={"title": "Validate Me", "text": "Chapter 1\n\nFoo."},
        ).json()["novel_id"]

        assert client.put(
            f"/api/novels/{novel_id}/reading-position",
            json={"chapter_num": 0},
        ).status_code == 422

        assert client.put(
            "/api/novels/99999/reading-position",
            json={"chapter_num": 5},
        ).status_code == 404


@pytest.mark.asyncio
async def test_patch_does_not_clobber_reading_position(app_with_stubs, isolated_db):
    """A general novel PATCH (e.g. rename) must leave the reading position
    untouched — the two write paths are independent."""
    await init_db()

    with TestClient(app_with_stubs) as client:
        novel_id = client.post(
            "/api/translate/paste",
            json={"title": "Before", "text": "Chapter 1\n\nFoo."},
        ).json()["novel_id"]

        client.put(
            f"/api/novels/{novel_id}/reading-position",
            json={"chapter_num": 42},
        )
        resp = client.patch(f"/api/novels/{novel_id}", json={"title": "After"})
        assert resp.status_code == 200, resp.text

        single = client.get(f"/api/novels/{novel_id}").json()
        assert single["title"] == "After"
        assert single["last_read_chapter_num"] == 42
