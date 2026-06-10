"""Tests for the PEMT (free-draft reference) section in build_prompt.

When ``free_draft`` is non-empty, build_prompt must insert a reference block
carrying the draft text. When ``free_draft`` is None or whitespace, the prompt
must be byte-identical to the no-draft prompt (graceful degrade to LLM-only).

Assertions are structural (prompt equality, draft-text containment, ordering
against test-owned inputs), never pinned to the prompt's own phrasing — the
scaffolding wording is edited freely and a copy edit must not break the suite.
"""

from __future__ import annotations

import pytest

from backend.models import GlossaryEntry
from backend.services.translators import base as base_module
from backend.services.translators.base import build_prompt, format_glossary


def test_pemt_section_absent_without_free_draft():
    """No free_draft, empty free_draft, and whitespace-only free_draft all
    produce the identical prompt — the reference section is omitted entirely,
    not emitted empty."""
    plain = build_prompt(chapter_zh="第一章。", title_zh="标题", glossary=[])
    assert build_prompt(
        chapter_zh="第一章。", title_zh="标题", glossary=[], free_draft=None,
    ) == plain
    assert build_prompt(
        chapter_zh="第一章。", title_zh="标题", glossary=[], free_draft="   \n\n   ",
    ) == plain


def test_pemt_section_present_with_free_draft():
    """A non-empty free_draft changes the prompt and carries the draft body
    verbatim."""
    plain = build_prompt(chapter_zh="第一章内容。", title_zh=None, glossary=[])
    out = build_prompt(
        chapter_zh="第一章内容。",
        title_zh=None,
        glossary=[],
        free_draft="Chapter 1.\n\nFirst sentence of the draft.",
    )
    assert out != plain
    # The draft body itself appears verbatim inside the section.
    assert "First sentence of the draft." in out


def test_pemt_section_precedes_chinese_source_block():
    """The reference block sits BEFORE the Chinese source text, so the LLM
    reads the reference before it reads what it has to translate."""
    out = build_prompt(
        chapter_zh="一行汉字。",
        title_zh=None,
        glossary=[],
        free_draft="Reference body line.",
    )
    assert out.index("Reference body line.") < out.index("一行汉字。")


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
    # The formatted glossary block appears identically in both outputs.
    block = format_glossary(glossary)
    assert block in out_with
    assert block in out_without


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
    from backend.services.translators.base import (
        _DELIMITED_BODY_DELIMITER,
        _DELIMITED_TERMS_DELIMITER,
        BaseTranslator,
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
