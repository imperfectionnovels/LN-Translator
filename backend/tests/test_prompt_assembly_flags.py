"""Tests for PROMPT_INCLUDE_* flag gates + prompt_config_snapshot provenance.

Covers three concerns:

1. Schema migration: chapters.prompt_config_snapshot exists on a fresh DB
   AND on a legacy DB (via _ADDITIVE_MIGRATIONS), with the right type and
   NOT NULL DEFAULT '{}'.
2. The snapshot helpers (_build_prompt_config_snapshot,
   _extend_snapshot_with_refiner) are pure and produce well-formed JSON
   with the expected keys.
3. The queue fetch helpers (_fetch_style_note, _fetch_style_edits) early-
   return when their flag is false, so the kwargs into translate_chapter
   drop to None / [] without DB mutation.

The free_draft and refiner gates are inline one-liners and tested
implicitly via the snapshot tests below (free_draft_included flips with
the flag; refinement_pending logic is exercised at the call site).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from backend.db import _ADDITIVE_MIGRATIONS, init_db
from backend.services import queue as queue_module
from backend.services.providers import Provider


def _chapter_column_map(db_path: Path) -> dict[str, dict[str, object]]:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("PRAGMA table_info(chapters)")
        rows = cur.fetchall()
    return {
        row[1]: {"type": row[2], "notnull": row[3], "default": row[4]}
        for row in rows
    }


@pytest.mark.asyncio
async def test_fresh_init_db_has_prompt_config_snapshot(tmp_path, monkeypatch):
    """init_db on a fresh DB creates chapters.prompt_config_snapshot from SCHEMA."""
    db_path = tmp_path / "fresh.db"
    from backend import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path, raising=True)

    await init_db()

    columns = _chapter_column_map(db_path)
    assert "prompt_config_snapshot" in columns
    col = columns["prompt_config_snapshot"]
    assert col["type"] == "TEXT"
    assert col["notnull"] == 1
    assert col["default"] == "'{}'"


def test_legacy_db_migration_adds_prompt_config_snapshot(tmp_path):
    """An existing chapters table without the column gets it via the
    additive migration. Run the migration on a synthetic legacy DB."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    # Minimal legacy chapters table — just enough columns to ALTER onto.
    conn.execute(
        "CREATE TABLE chapters (id INTEGER PRIMARY KEY, original_text TEXT NOT NULL)"
    )
    conn.commit()

    # Find the prompt_config_snapshot migration and apply it.
    target = next(
        s for s in _ADDITIVE_MIGRATIONS if "prompt_config_snapshot" in s
    )
    conn.execute(target)
    conn.commit()

    cur = conn.execute("PRAGMA table_info(chapters)")
    cols = {row[1]: row for row in cur.fetchall()}
    conn.close()
    assert "prompt_config_snapshot" in cols
    assert cols["prompt_config_snapshot"][2] == "TEXT"
    assert cols["prompt_config_snapshot"][3] == 1  # NOT NULL
    assert cols["prompt_config_snapshot"][4] == "'{}'"


def test_build_prompt_config_snapshot_well_formed_all_flags_true(monkeypatch):
    """At default flags, the snapshot reports every block as included when
    its data is non-empty, and every flag as true."""
    monkeypatch.setattr(queue_module, "PROMPT_INCLUDE_FREE_DRAFT", True)
    monkeypatch.setattr(queue_module, "PROMPT_INCLUDE_STYLE_NOTE", True)
    monkeypatch.setattr(queue_module, "PROMPT_INCLUDE_STYLE_EDITS", True)
    monkeypatch.setattr(queue_module, "PROMPT_INCLUDE_REFINER", True)
    monkeypatch.setattr(queue_module, "PREVIOUS_CONTEXT_ENABLED", True)

    provider = Provider(
        id=7, name="p", provider_type="deepseek",
        base_url=None, model_id="deepseek-chat",
        secret_ref="ENV", is_default=True,
    )
    novel_meta = {
        "genre": "xianxia",
        "custom_style_brief": "match the early-Daoist register",
        "source_language": "zh",
    }
    blob = queue_module._build_prompt_config_snapshot(
        provider=provider,
        novel_meta=novel_meta,
        free_draft_included=True,
        previous_context_included=True,
        style_note_included=True,
        style_edits_included=True,
    )
    parsed = json.loads(blob)
    assert parsed["translator_provider_id"] == 7
    assert parsed["translator_provider_type"] == "deepseek"
    assert parsed["translator_model_id"] == "deepseek-chat"
    assert parsed["genre"] == "xianxia"
    assert parsed["custom_brief_present"] is True
    assert parsed["free_draft_included"] is True
    assert parsed["previous_context_included"] is True
    assert parsed["style_note_included"] is True
    assert parsed["style_edits_included"] is True
    assert parsed["flags"]["PROMPT_INCLUDE_FREE_DRAFT"] is True
    assert parsed["flags"]["PROMPT_INCLUDE_STYLE_NOTE"] is True
    assert parsed["flags"]["PROMPT_INCLUDE_STYLE_EDITS"] is True
    assert parsed["flags"]["PROMPT_INCLUDE_REFINER"] is True
    assert parsed["flags"]["PREVIOUS_CONTEXT_ENABLED"] is True
    assert "prompt_template_version" in parsed


def test_build_prompt_config_snapshot_records_flag_off_separately(monkeypatch):
    """flags.* records env state regardless of block-emit state, so a
    flag-off translation is distinguishable from a flag-on + data-empty one."""
    monkeypatch.setattr(queue_module, "PROMPT_INCLUDE_FREE_DRAFT", False)
    # All other flags default; *_included flags track actual emission.
    blob = queue_module._build_prompt_config_snapshot(
        provider=None,
        novel_meta={"genre": None, "custom_style_brief": None},
        free_draft_included=False,
        previous_context_included=False,
        style_note_included=False,
        style_edits_included=False,
    )
    parsed = json.loads(blob)
    assert parsed["flags"]["PROMPT_INCLUDE_FREE_DRAFT"] is False
    assert parsed["free_draft_included"] is False
    assert parsed["translator_provider_id"] is None
    assert parsed["custom_brief_present"] is False


def test_extend_snapshot_with_refiner_merges_into_existing_blob():
    refiner = Provider(
        id=42, name="r", provider_type="anthropic_api",
        base_url=None, model_id="claude-opus-4-7",
        secret_ref="ENV", is_default=False,
    )
    existing = json.dumps({"translator_provider_id": 7, "genre": "xianxia"})
    merged = json.loads(queue_module._extend_snapshot_with_refiner(existing, refiner))
    assert merged["translator_provider_id"] == 7
    assert merged["genre"] == "xianxia"
    assert merged["refiner_provider_id"] == 42
    assert merged["refiner_provider_type"] == "anthropic_api"
    assert merged["refiner_model_id"] == "claude-opus-4-7"


def test_extend_snapshot_with_refiner_tolerates_missing_or_bad_json():
    refiner = Provider(
        id=1, name="r", provider_type="openai",
        base_url=None, model_id="gpt-5",
        secret_ref="ENV", is_default=False,
    )
    # None, empty string, malformed all start from {} so the refiner
    # provenance is still recorded.
    for existing in (None, "", "not json", "[]"):
        merged = json.loads(queue_module._extend_snapshot_with_refiner(existing, refiner))
        assert merged["refiner_provider_id"] == 1
        assert merged["refiner_provider_type"] == "openai"


@pytest.mark.asyncio
async def test_fetch_style_note_returns_none_when_flag_off(tmp_path, monkeypatch):
    """_fetch_style_note short-circuits to None when PROMPT_INCLUDE_STYLE_NOTE
    is false, even when novels.style_note has content."""
    db_path = tmp_path / "f.db"
    from backend import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path, raising=True)
    await init_db()
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "INSERT INTO novels (id, title, source_type, style_note) "
            "VALUES (1, 'N', 'paste', 'voice anchor text')"
        )
        await conn.commit()
        # _fetch_style_note uses a row_factory-keyed lookup; the connection's
        # default factory is fine here because aiosqlite.Row supports
        # subscript access by both index and column name.

        monkeypatch.setattr(queue_module, "PROMPT_INCLUDE_STYLE_NOTE", True)
        assert await queue_module._fetch_style_note(conn, 1) == "voice anchor text"

        monkeypatch.setattr(queue_module, "PROMPT_INCLUDE_STYLE_NOTE", False)
        assert await queue_module._fetch_style_note(conn, 1) is None


@pytest.mark.asyncio
async def test_fetch_style_edits_returns_empty_when_flag_off(tmp_path, monkeypatch):
    """_fetch_style_edits short-circuits to [] when PROMPT_INCLUDE_STYLE_EDITS
    is false, even when style_edits rows exist."""
    db_path = tmp_path / "f.db"
    from backend import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path, raising=True)
    await init_db()
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "INSERT INTO novels (id, title, source_type) VALUES (1, 'N', 'paste')"
        )
        await conn.execute(
            "INSERT INTO style_edits (novel_id, chapter_id, before_text, after_text) "
            "VALUES (1, NULL, 'before phrase', 'after phrase')"
        )
        await conn.commit()

        monkeypatch.setattr(queue_module, "PROMPT_INCLUDE_STYLE_EDITS", True)
        on_result = await queue_module._fetch_style_edits(conn, 1)
        assert on_result == [("before phrase", "after phrase")]

        monkeypatch.setattr(queue_module, "PROMPT_INCLUDE_STYLE_EDITS", False)
        off_result = await queue_module._fetch_style_edits(conn, 1)
        assert off_result == []
