"""Initiative 3 — global glossary tests.

Covers the three claims the plan made the most noise about:

1. Cache invalidation via prompt-body change: editing a global glossary
   entry produces a different `translation_key` than the same chapter
   would produce with a different rendering. The glossary text lives in
   the prompt body that feeds `translation_key`, so the cache key must
   actually shift when a global term changes.

2. Precedence: per-novel entries shadow global entries with the same
   term_zh. The composed list passed to the translator carries only the
   per-novel row when both exist.

3. Scope labels: format_glossary stamps [global] / [novel-locked] /
   [novel-auto] on each line so the translator sees precedence directly.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.config import DB_PATH
from backend.db import SCHEMA
from backend.models import GlossaryEntry
from backend.services import global_glossary, llm_cache
from backend.services.translators.base import (
    build_prompt,
    format_glossary,
)


@pytest.fixture(autouse=True)
def _reset_db():
    """Fresh DB per test. The conftest already redirects DB_PATH into a
    tempdir — we just clear and re-apply SCHEMA so global_glossary_entries
    is empty at the start of every test."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    yield


# ---- Scope-label rendering ----------------------------------------------


def test_format_glossary_labels_scope_for_each_kind():
    glossary = [
        GlossaryEntry(
            id=1, novel_id=1, term_zh="昂霄", term_en="Soaring Firmament",
            category="character", notes=None, auto_detected=False,
            locked=True, scope="novel",
        ),
        GlossaryEntry(
            id=2, novel_id=1, term_zh="鸿运道人", term_en="Hongyun Daoist",
            category="character", notes=None, auto_detected=True,
            locked=False, scope="novel",
        ),
        GlossaryEntry(
            id=3, novel_id=None, term_zh="筑基", term_en="Foundation Establishment",
            category="technique", notes=None, auto_detected=False,
            locked=True, scope="global",
        ),
    ]
    out = format_glossary(glossary)
    # Novel-locked vs novel-auto is conveyed by the MASTER vs THIS CHAPTER block
    # headers, so those per-line tags are omitted to save tokens; only a
    # cross-novel [global] term carries a tag (it can sit in either block).
    assert "[novel-locked]" not in out
    assert "[novel-auto]" not in out
    assert "[global]" in out
    assert "Soaring Firmament" in out
    assert "Hongyun Daoist" in out
    assert "Foundation Establishment  [global]" in out


def test_format_glossary_annotates_subterm_of_compound():
    # 法力 is a sub-token of 法力道主; the compound and the sub-token land in
    # different categories, so within-category ordering can't put the compound
    # first. The sub-token line must carry a containment pointer so the model
    # does not decompose the compound. (Reinforces base.md's longest-match rule.)
    glossary = [
        GlossaryEntry(
            id=1, novel_id=1, term_zh="法力道主", term_en="Spiritual Power Dao Lord",
            category="character", notes=None, auto_detected=False,
            locked=True, scope="novel",
        ),
        GlossaryEntry(
            id=2, novel_id=1, term_zh="法力", term_en="spiritual power",
            category="other", notes=None, auto_detected=False,
            locked=True, scope="novel",
        ),
    ]
    out = format_glossary(glossary)
    # The sub-token (法力) is flagged as part of the compound.
    sub_line = next(line for line in out.splitlines() if line.strip().startswith("法力 "))
    assert "part of 法力道主" in sub_line
    # The compound itself is not annotated as a sub-term of anything.
    compound_line = next(
        line for line in out.splitlines() if line.strip().startswith("法力道主 ")
    )
    assert "part of" not in compound_line


# ---- Precedence in the composed list ------------------------------------


@pytest.mark.asyncio
async def test_per_novel_entry_shadows_global_on_same_term():
    """Both a per-novel and a global entry have term_zh="筑基". The
    composed list returned to the prompt-build pipeline should contain
    only the per-novel one — the global is dropped because the per-novel
    side wins."""
    from backend.db import open_conn
    async with open_conn() as conn:
        # Set up a novel + a per-novel glossary row.
        await conn.execute(
            "INSERT INTO novels (id, title, source_type, source_url) "
            "VALUES (?, ?, ?, NULL)",
            (1, "TestNovel", "paste"),
        )
        await conn.execute(
            "INSERT INTO glossary_entries "
            "(novel_id, term_zh, term_en, category, locked, auto_detected) "
            "VALUES (?, ?, ?, ?, 1, 0)",
            (1, "筑基", "Foundation Forging", "technique"),
        )
        # Global entry with the SAME term_zh but a different rendering.
        await conn.execute(
            "INSERT INTO global_glossary_entries (term_zh, term_en, category) "
            "VALUES (?, ?, ?)",
            ("筑基", "Foundation Establishment", "technique"),
        )
        await conn.commit()

        composed = await global_glossary.list_for_novel_with_globals(conn, 1)

    # Only the per-novel rendering survives. The global is shadowed and the
    # remaining entry carries scope="novel".
    matches = [g for g in composed if g.term_zh == "筑基"]
    assert len(matches) == 1, f"expected 1, got {len(matches)}: {matches}"
    assert matches[0].term_en == "Foundation Forging"
    assert matches[0].scope == "novel"


@pytest.mark.asyncio
async def test_global_entry_appears_when_no_per_novel_shadow():
    """A global entry for a term the per-novel glossary doesn't cover comes
    through with scope='global'."""
    from backend.db import open_conn
    async with open_conn() as conn:
        await conn.execute(
            "INSERT INTO novels (id, title, source_type, source_url) "
            "VALUES (?, ?, ?, NULL)",
            (1, "TestNovel", "paste"),
        )
        await conn.execute(
            "INSERT INTO global_glossary_entries (term_zh, term_en, category) "
            "VALUES (?, ?, ?)",
            ("天劫", "Heavenly Tribulation", "other"),
        )
        await conn.commit()
        composed = await global_glossary.list_for_novel_with_globals(conn, 1)

    matches = [g for g in composed if g.term_zh == "天劫"]
    assert len(matches) == 1
    assert matches[0].scope == "global"
    assert matches[0].locked is True  # globals are inherently locked
    assert matches[0].novel_id is None


# ---- Cache invalidation via prompt body ---------------------------------


def _key_with_glossary(glossary):
    """Helper: compute a translation_key for a fixed chapter + fixed system
    instruction, varying only the glossary list. Mirrors what
    BaseTranslator.translate_chapter does just before the cache lookup."""
    prompt = build_prompt(
        chapter_zh="天劫降临，主角颤抖。",
        title_zh="第一章",
        glossary=glossary,
    )
    return llm_cache.translation_key(
        backend_id="test-backend:test-model:v1",
        system_instruction="(fixed system instruction)",
        prompt=prompt,
    )


def test_changing_global_glossary_term_changes_cache_key():
    """Plan viability claim #3: glossary content is in the user prompt body;
    the LLM cache key folds in the prompt body; therefore editing a global
    glossary term invalidates the cache for any novel that uses it."""
    before = [
        GlossaryEntry(
            id=10, novel_id=None, term_zh="天劫", term_en="Heavenly Tribulation",
            category="other", notes=None, auto_detected=False,
            locked=True, scope="global",
        ),
    ]
    after = [
        GlossaryEntry(
            id=10, novel_id=None, term_zh="天劫", term_en="Heaven's Trial",
            category="other", notes=None, auto_detected=False,
            locked=True, scope="global",
        ),
    ]
    key_before = _key_with_glossary(before)
    key_after = _key_with_glossary(after)
    assert key_before != key_after, (
        "Editing a global glossary term must change translation_key — "
        "otherwise the cached translation would override the new rendering."
    )


def test_adding_global_glossary_entry_changes_cache_key():
    """Likewise: adding a new global entry that's relevant to the chapter
    must invalidate. Without this, a freshly-promoted global would still
    serve stale cached translations of chapters that reference its term."""
    chapter_term = ("天劫", "Heavenly Tribulation")
    before = []  # no glossary at all
    after = [
        GlossaryEntry(
            id=11, novel_id=None, term_zh=chapter_term[0],
            term_en=chapter_term[1],
            category="other", notes=None, auto_detected=False,
            locked=True, scope="global",
        ),
    ]
    assert _key_with_glossary(before) != _key_with_glossary(after)
