"""Unit tests for the DeepSeek delimited-envelope parser.

`_parse_deepseek_response` turns DeepSeek's free-form delimited output (used
instead of JSON mode — see translators/deepseek.py) into a TranslationResult.
These tests are pure: no API calls, no DB.
"""

import pytest

from backend.services.translators.deepseek import (
    _BODY_DELIM,
    _TERMS_DELIM,
    _parse_deepseek_response,
)
from backend.services.translators.deepseek_revise import (
    _build_revise_prompt,
    _is_no_issues,
)


def _envelope(title: str, body: str, terms: str | None) -> str:
    text = f"TITLE_EN: {title}\n{_BODY_DELIM}\n{body}"
    if terms is not None:
        text += f"\n{_TERMS_DELIM}\n{terms}"
    return text


def test_well_formed_response() -> None:
    raw = _envelope(
        "The Mountain Gate",
        "Lin Feng stepped through the gate.\n\nThe wind howled.",
        '[{"zh": "林风", "en": "Lin Feng", "category": "character"}]',
    )
    result = _parse_deepseek_response(raw)
    assert result.title_en == "The Mountain Gate"
    assert result.translated_text == (
        "Lin Feng stepped through the gate.\n\nThe wind howled."
    )
    assert len(result.new_terms) == 1
    assert result.new_terms[0].zh == "林风"
    assert result.new_terms[0].en == "Lin Feng"
    assert result.new_terms[0].category == "character"


def test_missing_terms_section_yields_empty_terms() -> None:
    raw = _envelope("A Title", "Some body text.", terms=None)
    result = _parse_deepseek_response(raw)
    assert result.title_en == "A Title"
    assert result.translated_text == "Some body text."
    assert result.new_terms == []


def test_empty_terms_array_yields_empty_terms() -> None:
    raw = _envelope("A Title", "Body.", "[]")
    result = _parse_deepseek_response(raw)
    assert result.new_terms == []


def test_missing_body_delimiter_raises() -> None:
    raw = "TITLE_EN: No Body\nLin Feng walked on."
    with pytest.raises(ValueError):
        _parse_deepseek_response(raw)


def test_empty_body_raises() -> None:
    raw = _envelope("Title", "   ", "[]")
    with pytest.raises(ValueError):
        _parse_deepseek_response(raw)


def test_malformed_terms_json_is_tolerated() -> None:
    raw = _envelope("Title", "Body stays intact.", "[not valid json")
    result = _parse_deepseek_response(raw)
    # Body must still parse; only new_terms is dropped.
    assert result.translated_text == "Body stays intact."
    assert result.new_terms == []


def test_terms_with_bad_category_drops_only_that_term() -> None:
    raw = _envelope(
        "Title",
        "Body.",
        '[{"zh": "甲", "en": "A", "category": "character"}, '
        '{"zh": "乙", "en": "B", "category": "not-a-category"}]',
    )
    result = _parse_deepseek_response(raw)
    assert [t.en for t in result.new_terms] == ["A"]


def test_code_fence_wrapped_whole_response() -> None:
    inner = _envelope("Fenced", "The body survives.", "[]")
    raw = f"```\n{inner}\n```"
    result = _parse_deepseek_response(raw)
    assert result.title_en == "Fenced"
    assert result.translated_text == "The body survives."


def test_fenced_terms_block_does_not_eat_title_and_body() -> None:
    # A model that fences only the trailing TERMS JSON must not lose the
    # title/body — _unwrap_outer_fence only peels a whole-response wrapper.
    raw = (
        f"TITLE_EN: Kept\n{_BODY_DELIM}\nReal body content.\n{_TERMS_DELIM}\n"
        '```json\n[{"zh": "丙", "en": "C", "category": "item"}]\n```'
    )
    result = _parse_deepseek_response(raw)
    assert result.title_en == "Kept"
    assert result.translated_text == "Real body content."
    assert [t.en for t in result.new_terms] == ["C"]


def test_missing_title_falls_back_to_untitled() -> None:
    raw = f"{_BODY_DELIM}\nBody only, no title line.\n{_TERMS_DELIM}\n[]"
    result = _parse_deepseek_response(raw)
    assert result.title_en == "(untitled)"
    assert result.translated_text == "Body only, no title line."


def test_expect_terms_false_ignores_terms_section() -> None:
    # The improve pass passes expect_terms=False; a stray TERMS section must
    # be stripped off the body but not parsed into new_terms.
    raw = _envelope(
        "Improved", "Corrected body.",
        '[{"zh": "丁", "en": "D", "category": "place"}]',
    )
    result = _parse_deepseek_response(raw, expect_terms=False)
    assert result.translated_text == "Corrected body."
    assert result.new_terms == []


# -- _build_revise_prompt: single-pass combined critique+rewrite -------------


def test_build_revise_prompt_contains_required_sections() -> None:
    prompt = _build_revise_prompt(
        chapter_zh="第一章 测试内容",
        draft_en="Chapter One. A draft sentence.",
        glossary_block="[character]\n  林风 → Lin Feng",
    )
    # The Chinese source, the English draft, and the glossary must all be
    # embedded so the model has everything it needs in one call.
    assert "第一章 测试内容" in prompt
    assert "Chapter One. A draft sentence." in prompt
    assert "林风 → Lin Feng" in prompt


def test_build_revise_prompt_requests_termless_envelope() -> None:
    # The revise call carries new_terms over from the draft, so its envelope
    # omits the TERMS section — its output must round-trip through
    # _parse_deepseek_response(expect_terms=False).
    prompt = _build_revise_prompt("源", "draft", "(none)")
    assert _BODY_DELIM in prompt
    assert _TERMS_DELIM not in prompt


def test_revise_prompt_is_holistic_literary_edit() -> None:
    # The revise prompt must ask for a full re-render, not conservative
    # patch-mode, and must carry the worked style examples.
    prompt = _build_revise_prompt("源文", "draft", "(none)")
    assert "publishable English" in prompt
    assert "Worked examples" in prompt
    assert "Leave everything that has no genuine problem unchanged" not in prompt


def test_revise_prompt_carries_novel_wide_checks() -> None:
    # The single-pass revise prompt must police cross-chapter consistency:
    # recurring term / name / epithet drift, title-order drift, and a realm
    # stacked straight onto a group noun. Phase 2: the prompt is now
    # genre-agnostic, so "genre register" check is phrased against the
    # SYSTEM_INSTRUCTION's genre rather than naming xianxia specifically.
    # The xianxia-specific examples (LitRPG ban, "early Foundation
    # Establishment clan") moved to backend/prompts/genres/xianxia.md;
    # passing genre="xianxia" will surface them via worked examples.
    prompt = _build_revise_prompt("源文", "draft", "(none)").lower()
    assert "epithet" in prompt
    assert "title-first" in prompt
    # Genre-register check is now phrased against the system instruction's
    # genre, not hardcoded against xianxia.
    assert "genre register" in prompt
    # Xianxia-specific worked example lands when genre="xianxia" is passed.
    prompt_xianxia = _build_revise_prompt(
        "源文", "draft", "(none)", genre="xianxia",
    ).lower()
    assert "early foundation establishment clan" in prompt_xianxia


# -- _is_no_issues: reflect-pass "nothing to fix" sentinel --------------------


@pytest.mark.parametrize(
    "text",
    [
        "NO ISSUES",
        "no issues",
        "  NO ISSUES.  ",
        "**NO ISSUES**",
        "NO ISSUES FOUND",
        "No issues found.",
        '"NO ISSUES"',
    ],
)
def test_is_no_issues_true(text: str) -> None:
    assert _is_no_issues(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1. Fidelity: the phrase 'X' is mistranslated; should be 'Y'.",
        "No issues with the dialogue, but the narration is clunky.",
        "Issues: none with fidelity. 1. Fix the grammar in paragraph 2.",
        "The draft has no issues worth flagging except one minor tense slip.",
    ],
)
def test_is_no_issues_false(text: str) -> None:
    # A real review — even one that contains the words "no issues" — must not
    # be mistaken for a clean pass.
    assert _is_no_issues(text) is False
