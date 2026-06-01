"""Behavior tests for backend.services.prompt_inputs.

prompt_inputs is the read-only fetch surface the translator prompt is built
from: the queue worker (services/queue.py) and the A/B style-edit script both
subscript its return values directly. The flag-gated short-circuits for
fetch_style_note / fetch_style_edits already live in test_prompt_assembly_flags;
this file pins the remaining behavior that was previously uncovered:

- fetch_previous_chapter_tail: nearest-earlier-done lookup, the MAX_GAP window,
  the chapter_num <= 1 guard, the NULL / empty-body guards, and the
  PARAGRAPHS tail slice.
- resolve_translator_provider: per-novel id wins, falls back to the global
  default when the id is NULL or dangling, returns None on an empty table.
- fetch_novel_genre_brief: passes genre/brief through, defaults
  source_language to 'zh' for legacy NULL rows, tolerates a missing novel.
- fetch_style_edits: dedups repeated before/after pairs while preserving the
  newest-first order.

Harness: the conftest temp DB via init_db() + open_conn(), the same shape
test_providers.py uses, so providers created with create_provider land in the
same DB resolve_translator_provider reads from. No network / LLM is touched.
"""

from __future__ import annotations

import aiosqlite
import pytest

from backend.db import init_db, open_conn
from backend.services import prompt_inputs
from backend.services import providers as providers_svc

pytestmark = pytest.mark.asyncio


async def _reset_db() -> None:
    async with open_conn() as conn:
        for table in ("style_edits", "chapters", "novels", "providers"):
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


async def _new_novel(**cols) -> int:
    """Insert a novel with the given column overrides; return its id."""
    cols.setdefault("title", "N")
    cols.setdefault("source_type", "paste")
    keys = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    async with open_conn() as conn:
        cur = await conn.execute(
            f"INSERT INTO novels ({keys}) VALUES ({placeholders})",
            tuple(cols.values()),
        )
        await conn.commit()
        return cur.lastrowid


async def _add_chapter(
    novel_id: int, num: int, *, status: str, translated_text: str | None,
) -> None:
    async with open_conn() as conn:
        await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "status, translated_text) VALUES (?, ?, '...', ?, ?)",
            (novel_id, num, status, translated_text),
        )
        await conn.commit()


# ============================================================
# fetch_previous_chapter_tail
# ============================================================

async def test_prev_tail_none_on_first_chapter():
    """chapter_num <= 1 short-circuits to None before any DB read."""
    novel_id = await _new_novel()
    async with open_conn() as conn:
        assert await prompt_inputs.fetch_previous_chapter_tail(
            conn, novel_id, 1,
        ) is None


async def test_prev_tail_returns_nearest_done_chapter_tail():
    """The immediately-preceding done chapter supplies the tail; an even
    earlier done chapter is ignored once a nearer one exists."""
    novel_id = await _new_novel()
    await _add_chapter(
        novel_id, 1, status="done", translated_text="ch1 a\n\nch1 b",
    )
    await _add_chapter(
        novel_id, 2, status="done",
        translated_text="p1\n\np2\n\np3\n\np4\n\np5",
    )
    async with open_conn() as conn:
        tail = await prompt_inputs.fetch_previous_chapter_tail(
            conn, novel_id, 3,
        )
    # PREVIOUS_CONTEXT_PARAGRAPHS == 4, so the last 4 paragraphs of ch2.
    assert tail == "p2\n\np3\n\np4\n\np5"


async def test_prev_tail_skips_non_done_chapter():
    """A pending nearer chapter is skipped in favor of an earlier done one."""
    novel_id = await _new_novel()
    await _add_chapter(
        novel_id, 1, status="done", translated_text="done body",
    )
    await _add_chapter(
        novel_id, 2, status="pending", translated_text=None,
    )
    async with open_conn() as conn:
        tail = await prompt_inputs.fetch_previous_chapter_tail(
            conn, novel_id, 3,
        )
    assert tail == "done body"


async def test_prev_tail_respects_max_gap_window():
    """A done chapter further back than PREVIOUS_CONTEXT_MAX_GAP (10) is out
    of range and yields None."""
    novel_id = await _new_novel()
    # Chapter 1 done, requesting chapter 12 → floor = 12 - 10 = 2, so
    # chapter 1 (< floor) is excluded.
    await _add_chapter(
        novel_id, 1, status="done", translated_text="too far back",
    )
    async with open_conn() as conn:
        tail = await prompt_inputs.fetch_previous_chapter_tail(
            conn, novel_id, 12,
        )
    assert tail is None


async def test_prev_tail_none_when_body_empty():
    """A done previous chapter with an empty translated_text yields None,
    not an empty-string tail."""
    novel_id = await _new_novel()
    await _add_chapter(novel_id, 1, status="done", translated_text="")
    async with open_conn() as conn:
        tail = await prompt_inputs.fetch_previous_chapter_tail(
            conn, novel_id, 2,
        )
    assert tail is None


async def test_prev_tail_disabled_flag_returns_none(monkeypatch):
    """PREVIOUS_CONTEXT_ENABLED=False suppresses the block even with a valid
    done previous chapter present."""
    novel_id = await _new_novel()
    await _add_chapter(novel_id, 1, status="done", translated_text="body")
    monkeypatch.setattr(prompt_inputs, "PREVIOUS_CONTEXT_ENABLED", False)
    async with open_conn() as conn:
        tail = await prompt_inputs.fetch_previous_chapter_tail(
            conn, novel_id, 2,
        )
    assert tail is None


# ============================================================
# resolve_translator_provider
# ============================================================

async def test_resolve_provider_uses_per_novel_id():
    """When the novel pins translator_provider_id, that provider is returned
    even if it isn't the global default."""
    default_p = await providers_svc.create_provider(
        name="default", provider_type="gemini", model_id="d",
    )
    pinned_p = await providers_svc.create_provider(
        name="pinned", provider_type="gemini", model_id="p",
    )
    # First provider auto-promotes to default; pinned is not the default.
    assert default_p.is_default is True
    assert pinned_p.is_default is False
    novel_id = await _new_novel(translator_provider_id=pinned_p.id)
    async with open_conn() as conn:
        resolved = await prompt_inputs.resolve_translator_provider(
            conn, novel_id,
        )
    assert resolved is not None
    assert resolved.id == pinned_p.id


async def test_resolve_provider_falls_back_to_default_when_unset():
    """A novel with NULL translator_provider_id resolves to the global
    default provider."""
    default_p = await providers_svc.create_provider(
        name="default", provider_type="gemini", model_id="d",
    )
    novel_id = await _new_novel()  # translator_provider_id is NULL
    async with open_conn() as conn:
        resolved = await prompt_inputs.resolve_translator_provider(
            conn, novel_id,
        )
    assert resolved is not None
    assert resolved.id == default_p.id


async def test_resolve_provider_falls_back_when_id_is_dangling():
    """A novel whose translator_provider_id points at a provider row that no
    longer exists falls back to the default rather than returning None.

    The live FK is ON DELETE SET NULL, so a dangling pointer cannot normally
    persist; this exercises prompt_inputs' defensive 'row is gone' branch
    (the load_provider-returns-None path) by writing the stale id with FK
    enforcement off, the shape a legacy / FK-disabled DB could leave behind.
    """
    default_p = await providers_svc.create_provider(
        name="default", provider_type="gemini", model_id="d",
    )
    novel_id = await _new_novel()
    async with open_conn() as conn:
        await conn.execute("PRAGMA foreign_keys = OFF")
        await conn.execute(
            "UPDATE novels SET translator_provider_id = 999 WHERE id = ?",
            (novel_id,),
        )
        await conn.commit()
    async with open_conn() as conn:
        resolved = await prompt_inputs.resolve_translator_provider(
            conn, novel_id,
        )
    assert resolved is not None
    assert resolved.id == default_p.id


async def test_resolve_provider_none_when_no_providers():
    """An empty providers table yields None (the caller then falls through to
    the env-driven translator_factory)."""
    novel_id = await _new_novel()
    async with open_conn() as conn:
        resolved = await prompt_inputs.resolve_translator_provider(
            conn, novel_id,
        )
    assert resolved is None


# ============================================================
# fetch_novel_genre_brief
# ============================================================

async def test_genre_brief_passes_through_values():
    novel_id = await _new_novel(
        genre="wuxia",
        custom_style_brief="terse, wry",
        source_language="ja",
    )
    async with open_conn() as conn:
        brief = await prompt_inputs.fetch_novel_genre_brief(conn, novel_id)
    assert brief["genre"] == "wuxia"
    assert brief["custom_style_brief"] == "terse, wry"
    assert brief["source_language"] == "ja"


async def test_genre_brief_coalesces_blank_source_language():
    """A falsy (empty-string) source_language coalesces to 'zh' via the
    `row[...] or 'zh'` guard; NULL genre/brief pass through as None for
    build_system_instruction's fallback to handle.

    The live schema declares source_language NOT NULL DEFAULT 'zh', so a
    blanked-out empty string is the realistic falsy value to exercise (a
    legacy NULL would hit the same branch)."""
    novel_id = await _new_novel(source_language="")
    async with open_conn() as conn:
        brief = await prompt_inputs.fetch_novel_genre_brief(conn, novel_id)
    assert brief["genre"] is None
    assert brief["custom_style_brief"] is None
    assert brief["source_language"] == "zh"


async def test_genre_brief_missing_novel_returns_defaults():
    """A non-existent novel id returns the neutral default shape, not a
    crash."""
    async with open_conn() as conn:
        brief = await prompt_inputs.fetch_novel_genre_brief(conn, 424242)
    assert brief == {
        "genre": None, "custom_style_brief": None, "source_language": "zh",
    }


# ============================================================
# fetch_style_edits dedup
# ============================================================

async def test_style_edits_dedups_repeated_pairs(monkeypatch):
    """Identical before/after pairs collapse to one entry, preserving the
    newest-first order of the first occurrence."""
    monkeypatch.setattr(prompt_inputs, "PROMPT_INCLUDE_STYLE_EDITS", True)
    novel_id = await _new_novel()
    async with open_conn() as conn:
        # Insert in chronological order; the fetch returns newest id first.
        for before, after in (
            ("aaa", "AAA"),   # id 1 (oldest)
            ("bbb", "BBB"),   # id 2
            ("aaa", "AAA"),   # id 3 — duplicate of id 1
        ):
            await conn.execute(
                "INSERT INTO style_edits (novel_id, chapter_id, before_text, "
                "after_text) VALUES (?, NULL, ?, ?)",
                (novel_id, before, after),
            )
        await conn.commit()
        result = await prompt_inputs.fetch_style_edits(conn, novel_id)
    # Newest first: id3 (aaa/AAA) then id2 (bbb/BBB); id1 dedups against id3.
    assert result == [("aaa", "AAA"), ("bbb", "BBB")]
