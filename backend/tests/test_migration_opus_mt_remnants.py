"""Coverage for backend.main._migrate_opus_mt_remnants.

The migration handles three things on the first boot after upgrading from
the OPUS-MT free-tier to the Google Translate free-tier:
  1. Rewrites providers.provider_type='opus_mt' rows to 'google_translate_free'.
  2. Deletes USER_DATA_ROOT/opus_mt/ if it exists.
  3. Resets chapters with free_draft_status != 'none' so the new engine
     regenerates fresh drafts (the old OPUS-MT-era text was the reason for
     the swap).

A config_kv sentinel (free_draft_engine_migrated_to_google = '1') gates the
whole thing so subsequent boots are no-ops — that's critical because the
chapter reset is destructive and we don't want it to keep clearing freshly
generated Google drafts.
"""

from __future__ import annotations

import aiosqlite
import pytest

from backend.db import init_db, open_conn
from backend.main import _OPUS_MT_MIGRATION_SENTINEL, _migrate_opus_mt_remnants

pytestmark = pytest.mark.asyncio


async def _reset_db() -> None:
    async with open_conn() as conn:
        for table in ("chapters", "novels", "providers", "config_kv"):
            try:
                await conn.execute(f"DELETE FROM {table}")
            except aiosqlite.OperationalError:
                pass
        await conn.commit()


@pytest.fixture(autouse=True)
async def fresh_db():
    await init_db()
    await _reset_db()
    yield
    await _reset_db()


async def _seed_chapter(free_draft_status: str, free_draft_text: str | None) -> int:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, source_language) "
            "VALUES (?, 'paste', 'zh')",
            ("Test Novel",),
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, original_text, status, "
            "free_draft_text, free_draft_status) "
            "VALUES (?, ?, ?, 'done', ?, ?)",
            (novel_id, 1, "第一章。", free_draft_text, free_draft_status),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return chapter_id


async def _read_chapter(chapter_id: int) -> dict:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT free_draft_status, free_draft_text, free_draft_error, "
            "free_draft_completed_at "
            "FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        return await cur.fetchone()


async def _seed_opus_mt_provider() -> int:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO providers (name, provider_type, model_id, params_json) "
            "VALUES ('Free Tier', 'opus_mt', 'zh-en', '{}')",
        )
        await conn.commit()
        return cur.lastrowid


async def _read_provider(provider_id: int) -> dict:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT provider_type, model_id FROM providers WHERE id = ?",
            (provider_id,),
        )
        return await cur.fetchone()


# ---------------------------------------------------------------------------
# Provider migration
# ---------------------------------------------------------------------------

async def test_migration_rewrites_opus_mt_provider_rows():
    provider_id = await _seed_opus_mt_provider()
    await _migrate_opus_mt_remnants()
    row = await _read_provider(provider_id)
    assert row["provider_type"] == "google_translate_free"
    assert row["model_id"] == "google-web"


# ---------------------------------------------------------------------------
# Chapter free-draft state reset
# ---------------------------------------------------------------------------

async def test_migration_clears_opus_mt_era_free_draft_text():
    """Chapters with old OPUS-MT free-draft text get their state reset to
    'none' so the new Google Translate worker regenerates them fresh."""
    chapter_id = await _seed_chapter(
        free_draft_status="done",
        free_draft_text="OPUS-MT garbage ZX0660 here",
    )
    await _migrate_opus_mt_remnants()
    row = await _read_chapter(chapter_id)
    assert row["free_draft_status"] == "none"
    assert row["free_draft_text"] is None
    assert row["free_draft_error"] is None
    assert row["free_draft_completed_at"] is None


async def test_migration_clears_errored_chapter_state():
    """Chapters where the OPUS-MT worker had errored out also get reset
    so the next reader open re-queues them under Google Translate."""
    chapter_id = await _seed_chapter(
        free_draft_status="error",
        free_draft_text=None,
    )
    await _migrate_opus_mt_remnants()
    row = await _read_chapter(chapter_id)
    assert row["free_draft_status"] == "none"


async def test_migration_leaves_chapters_already_at_none_alone():
    """Brand-new chapters that never had a draft attempt shouldn't be
    touched — they're already in the right state for the worker to pick up."""
    chapter_id = await _seed_chapter(
        free_draft_status="none",
        free_draft_text=None,
    )
    await _migrate_opus_mt_remnants()
    row = await _read_chapter(chapter_id)
    assert row["free_draft_status"] == "none"


# ---------------------------------------------------------------------------
# Sentinel-gated idempotence
# ---------------------------------------------------------------------------

async def test_migration_writes_sentinel_after_first_run():
    await _migrate_opus_mt_remnants()
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT value FROM config_kv WHERE key = ?",
            (_OPUS_MT_MIGRATION_SENTINEL,),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row["value"] == "1"


async def test_migration_is_no_op_on_second_call():
    """Once the sentinel is set, a second migration run must NOT touch any
    chapter state — including a freshly-generated Google Translate draft
    that arrived after the first migration."""
    await _migrate_opus_mt_remnants()
    # Simulate a fresh Google Translate draft landing post-migration.
    chapter_id = await _seed_chapter(
        free_draft_status="done",
        free_draft_text="fresh Google Translate output",
    )
    await _migrate_opus_mt_remnants()
    row = await _read_chapter(chapter_id)
    assert row["free_draft_status"] == "done"
    assert row["free_draft_text"] == "fresh Google Translate output"
