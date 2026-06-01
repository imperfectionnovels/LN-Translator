"""Per-chapter token persistence (the catalog-redesign-trimmed remainder).

Verifies:
- TranslationResult carries TokenUsage when a backend emits one.
- Queue worker writes input_tokens / output_tokens / cached_input_tokens
  to the chapters row on success.
- The legacy `_drop_dead_columns` rebuild carries the token columns (and
  the vestigial cost_usd column) through without blanking live data.

Per-chapter cost tracking was removed in the 2026-05-26 catalog redesign:
the chapters.cost_usd column is retained for migration safety but is never
read or written, so there are no cost assertions here.
"""

from __future__ import annotations

import pytest

from backend.db import init_db, open_conn
from backend.models import TokenUsage, TranslationResult
from backend.services import providers as providers_svc
from backend.services import queue as queue_svc


async def _reset_db() -> None:
    async with open_conn() as conn:
        for table in ("chapters", "novels", "providers"):
            try:
                await conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        await conn.commit()


@pytest.fixture(autouse=True)
async def fresh_db():
    await init_db()
    await _reset_db()
    yield
    await _reset_db()


# ---- queue worker end-to-end ------------------------------------------------

async def _seed_provider() -> int:
    p = await providers_svc.create_provider(
        name="gem",
        provider_type="gemini",
        model_id="gemini-3-pro",
        secret_ref="DEFINITELY_NOT_SET_FOR_TESTS",
        is_default=True,
    )
    return p.id


async def _make_novel_with_chapter() -> tuple[int, int]:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, ?)",
            ("test novel", "paste"),
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "status, translate_queued) VALUES (?, 1, '原文', 'pending', 1)",
            (novel_id,),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return novel_id, chapter_id


def _stub_translate_with_usage(monkeypatch, usage: TokenUsage | None) -> None:
    async def _fake(*args, **kwargs):
        return TranslationResult(
            title_en="Chapter 1: T",
            translated_text="A normal-length body " * 30,
            new_terms=[],
            usage=usage,
        )
    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake)


async def test_queue_writes_token_columns(monkeypatch):
    """Fresh translate run with usage attached: token counts land on the
    chapter row."""
    await _seed_provider()
    novel_id, chapter_id = await _make_novel_with_chapter()
    _stub_translate_with_usage(
        monkeypatch,
        TokenUsage(input_tokens=10_000, output_tokens=5_000, cached_input_tokens=2_000),
    )

    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT status, input_tokens, output_tokens, cached_input_tokens "
            "FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["status"] == "done"
    assert row["input_tokens"] == 10_000
    assert row["output_tokens"] == 5_000
    assert row["cached_input_tokens"] == 2_000


async def test_queue_skips_token_columns_when_usage_missing(monkeypatch):
    """No usage in the TranslationResult (cache hit or claude_cli on an
    older version) leaves columns NULL."""
    await _seed_provider()
    novel_id, chapter_id = await _make_novel_with_chapter()
    _stub_translate_with_usage(monkeypatch, usage=None)

    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT input_tokens, output_tokens "
            "FROM chapters WHERE id = ?", (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None


async def test_queue_preserves_existing_columns_on_cache_hit_retranslate(monkeypatch):
    """A retranslate that hits the cache (no fresh usage) must NOT
    overwrite the chapter's existing token columns from the first
    successful translation."""
    await _seed_provider()
    novel_id, chapter_id = await _make_novel_with_chapter()

    _stub_translate_with_usage(
        monkeypatch,
        TokenUsage(input_tokens=8000, output_tokens=4000),
    )
    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)
        await conn.execute(
            "UPDATE chapters SET status='pending', translate_queued=1, "
            "force_retranslate=1 WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()

    _stub_translate_with_usage(monkeypatch, usage=None)
    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)
        cur = await conn.execute(
            "SELECT input_tokens, output_tokens "
            "FROM chapters WHERE id = ?", (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["input_tokens"] == 8000
    assert row["output_tokens"] == 4000


async def test_drop_dead_columns_rebuild_preserves_token_data():
    """Block 1.2 regression: `_drop_dead_columns` rebuilds the chapters
    table to drop legacy `humanized_text`. The rebuild was patched to
    carry the token columns through; without this test, a future refactor
    of the CREATE TABLE / INSERT statements could silently blank live token
    data on the one DB where the rebuild fires (a humanizer-era install).

    cost_usd is still in the schema (legacy data carries forward) but is
    no longer read or written post-redesign; the rebuild keeps the column
    intact, which this test still pins."""
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
        await conn.execute(
            "CREATE TABLE novels (id INTEGER PRIMARY KEY, title TEXT, "
            "source_type TEXT, source_url TEXT, created_at TEXT, "
            "style_note TEXT, humanizer_tone TEXT, "
            "source_language TEXT NOT NULL DEFAULT 'zh', "
            "genre TEXT, custom_style_brief TEXT, "
            "translator_provider_id INTEGER, refinement_provider_id INTEGER)"
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
            "refinement_status TEXT NOT NULL DEFAULT 'none', "
            "refined_text TEXT, refinement_error TEXT, refined_at TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, "
            "cached_input_tokens INTEGER, cost_usd REAL, "
            "UNIQUE (novel_id, chapter_num))"
        )
        await conn.execute(
            "INSERT INTO novels (id, title, source_type, source_url, created_at, "
            "humanizer_tone) VALUES (1, 'precious-cost', 'paste', NULL, "
            "'2026-01-01', 'scholarly')"
        )
        await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status, humanized_text, input_tokens, "
            "output_tokens, cached_input_tokens, cost_usd) VALUES "
            "(1, 1, 'src', 'eng draft', 'done', 'humanized eng', "
            "12345, 6789, 1024, 0.42)"
        )
        await conn.commit()
        await conn.execute("PRAGMA foreign_keys = ON")

    await init_db()

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT input_tokens, output_tokens, cached_input_tokens, cost_usd, "
            "translated_text FROM chapters WHERE novel_id = 1 AND chapter_num = 1"
        )
        row = await cur.fetchone()
    assert row is not None, "chapter row was dropped during rebuild"
    assert row["input_tokens"] == 12345
    assert row["output_tokens"] == 6789
    assert row["cached_input_tokens"] == 1024
    assert row["cost_usd"] == pytest.approx(0.42, abs=1e-6)
    assert row["translated_text"] == "humanized eng"
