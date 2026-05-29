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
    # expect_terms=False parses an envelope whose TERMS section should be
    # stripped off the body but not parsed into new_terms.
    raw = _envelope(
        "Improved", "Corrected body.",
        '[{"zh": "丁", "en": "D", "category": "place"}]',
    )
    result = _parse_deepseek_response(raw, expect_terms=False)
    assert result.translated_text == "Corrected body."
    assert result.new_terms == []
