"""Tests for the PEMT prompt section in build_prompt.

When ``free_draft`` is non-empty, build_prompt must insert a REFERENCE
TRANSLATION section with the explicit "combine the best parts" instruction.
When ``free_draft`` is None or whitespace, the section must be omitted
entirely (graceful degrade to the current LLM-only behavior).

Also pins PROMPT_TEMPLATE_VERSION bump so existing cached LLM translations
re-run through PEMT mode.
"""

from __future__ import annotations

import pytest

from backend.models import GlossaryEntry
from backend.services.translators import base as base_module
from backend.services.translators.base import (
    PROMPT_TEMPLATE_VERSION,
    build_prompt,
)


# Marker strings the test scans for. Kept here so a future copy edit to the
# prompt body lights up only one test instead of dozens.
_REFERENCE_HEADER = "REFERENCE TRANSLATION"
_COMBINE_INSTRUCTION = "combines the best parts of each"
_DO_NOT_COPY = "DO NOT TRANSLATE OR COPY VERBATIM"


def test_pemt_section_absent_without_free_draft():
    """No free_draft → no REFERENCE TRANSLATION section. The LLM prompt is
    identical to the pre-PEMT behavior; existing callers see no diff."""
    out = build_prompt(
        chapter_zh="第一章。",
        title_zh="标题",
        glossary=[],
    )
    assert _REFERENCE_HEADER not in out
    assert _COMBINE_INSTRUCTION not in out
    assert _DO_NOT_COPY not in out


def test_pemt_section_absent_with_whitespace_free_draft():
    """A whitespace-only free_draft is treated as None — no section."""
    out = build_prompt(
        chapter_zh="第一章。",
        title_zh=None,
        glossary=[],
        free_draft="   \n\n   ",
    )
    assert _REFERENCE_HEADER not in out


def test_pemt_section_present_with_free_draft():
    """A non-empty free_draft injects the full PEMT block."""
    out = build_prompt(
        chapter_zh="第一章内容。",
        title_zh=None,
        glossary=[],
        free_draft="Chapter 1.\n\nFirst sentence of the draft.",
    )
    assert _REFERENCE_HEADER in out
    assert _COMBINE_INSTRUCTION in out
    assert _DO_NOT_COPY in out
    # The draft body itself appears verbatim inside the section.
    assert "First sentence of the draft." in out


def test_pemt_section_precedes_chinese_source_block():
    """The reference block sits between the style preferences and the
    Chinese source — i.e. AFTER the glossary/style context but BEFORE the
    CHAPTER (Chinese) header, so the LLM reads the reference *before* it
    reads what it has to translate."""
    out = build_prompt(
        chapter_zh="一行汉字。",
        title_zh=None,
        glossary=[],
        free_draft="Reference body line.",
    )
    ref_idx = out.index(_REFERENCE_HEADER)
    chapter_idx = out.index("CHAPTER (Chinese)")
    assert ref_idx < chapter_idx


def test_pemt_section_does_not_disturb_glossary_block():
    """Adding free_draft must not change the glossary block formatting —
    it inserts a new section, not edits an existing one."""
    glossary = [
        GlossaryEntry(
            id=1, novel_id=1,
            term_zh="测试", term_en="Test",
            category="other", notes=None, auto_detected=False, locked=True,
        ),
    ]
    out_with = build_prompt(
        chapter_zh="测试内容。", title_zh=None, glossary=glossary,
        free_draft="A reference.",
    )
    out_without = build_prompt(
        chapter_zh="测试内容。", title_zh=None, glossary=glossary,
    )
    # Master glossary line is byte-identical between the two outputs.
    assert "测试 → Test" in out_with
    assert "测试 → Test" in out_without


def test_prompt_template_version_bumped_for_pemt():
    """The version constant moved past phase2-genres for the PEMT release.
    Existing caches keyed on the old version stop matching, so a re-run
    picks up the new prompt shape."""
    assert PROMPT_TEMPLATE_VERSION != "phase2-genres"
    assert "pemt" in PROMPT_TEMPLATE_VERSION


def test_pemt_reference_truncated_when_over_cap(monkeypatch):
    """A free_draft longer than FREE_DRAFT_REF_MAX_CHARS is truncated with a
    marker, so a pathologically long draft cannot balloon the prompt."""
    monkeypatch.setattr(base_module, "FREE_DRAFT_REF_MAX_CHARS", 50)
    out = build_prompt(
        chapter_zh="第一章内容。",
        title_zh=None,
        glossary=[],
        free_draft="X" * 200,
    )
    assert _REFERENCE_HEADER in out
    assert "[reference truncated]" in out
    assert "X" * 200 not in out


def test_pemt_reference_untouched_under_cap(monkeypatch):
    """A normal-length draft passes through whole, with no truncation marker."""
    monkeypatch.setattr(base_module, "FREE_DRAFT_REF_MAX_CHARS", 10000)
    out = build_prompt(
        chapter_zh="第一章内容。",
        title_zh=None,
        glossary=[],
        free_draft="Reference body that is short.",
    )
    assert "Reference body that is short." in out
    assert "[reference truncated]" not in out


def test_pemt_cap_disabled_when_non_positive(monkeypatch):
    """A cap of 0 disables truncation entirely."""
    monkeypatch.setattr(base_module, "FREE_DRAFT_REF_MAX_CHARS", 0)
    out = build_prompt(
        chapter_zh="第一章内容。",
        title_zh=None,
        glossary=[],
        free_draft="Y" * 500,
    )
    assert "Y" * 500 in out
    assert "[reference truncated]" not in out


@pytest.mark.asyncio
async def test_basetranslator_threads_free_draft_into_build_prompt(monkeypatch):
    """BaseTranslator.translate_chapter must accept the new kwargs and
    pass free_draft through to build_prompt."""
    from backend.models import TranslationResult
    from backend.services.translators.base import (
        BaseTranslator,
        _DELIMITED_BODY_DELIMITER,
        _DELIMITED_TERMS_DELIMITER,
    )

    captured_prompt: list[str] = []

    class _StubTranslator(BaseTranslator):
        name = "stub"
        model_id = "stub-1"

        async def _complete(self, prompt: str) -> str:
            captured_prompt.append(prompt)
            # Return a minimal valid delimited envelope.
            return (
                "TITLE_EN: t\n"
                f"{_DELIMITED_BODY_DELIMITER}\n"
                "body.\n"
                f"{_DELIMITED_TERMS_DELIMITER}\n[]"
            )

        async def _complete_plain(self, prompt: str) -> str:
            return "body."

    t = _StubTranslator()
    await t.translate_chapter(
        chapter_zh="第一章。",
        title_zh=None,
        glossary=[],
        free_draft="Hand-rolled reference.",
        source_language="zh",
        use_cache=False,
    )
    assert captured_prompt, "stub _complete was never called"
    assert "Hand-rolled reference." in captured_prompt[0]
    assert _REFERENCE_HEADER in captured_prompt[0]
