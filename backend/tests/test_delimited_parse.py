"""Unit tests for the shared delimited-envelope parser in translators/base.py.

Focus: the missing-TERMS-delimiter case. A response that drops the
=====TERMS===== line but still appends the terms JSON array must NOT parse
"successfully" with the JSON left inside the chapter body; raising lets the
caller's existing one-retry path fire. A response with no terms material at
all stays tolerated (absent block means zero new terms).
"""

from __future__ import annotations

import pytest

from backend.services.translators.base import parse_delimited_response


def test_missing_terms_delimiter_with_trailing_terms_json_raises():
    raw = (
        "TITLE_EN: The Mountain Gate\n"
        "=====BODY=====\n"
        "Lin Feng stepped through the gate.\n\n"
        "The wind howled.\n\n"
        '[{"zh": "林风", "en": "Lin Feng", "category": "character"}]'
    )
    with pytest.raises(ValueError):
        parse_delimited_response(raw)


def test_missing_terms_delimiter_with_fenced_terms_json_raises():
    raw = (
        "TITLE_EN: The Mountain Gate\n"
        "=====BODY=====\n"
        "Lin Feng stepped through the gate.\n\n"
        '```json\n[{"zh": "林风", "en": "Lin Feng", "category": "character"}]\n```'
    )
    with pytest.raises(ValueError):
        parse_delimited_response(raw)


def test_missing_terms_delimiter_plain_body_still_tolerated():
    raw = (
        "TITLE_EN: A Title\n"
        "=====BODY=====\n"
        "Some body text.\n\nA second paragraph."
    )
    result = parse_delimited_response(raw)
    assert result.title_en == "A Title"
    assert result.translated_text == "Some body text.\n\nA second paragraph."
    assert result.new_terms == []


def test_body_paragraph_with_brackets_is_not_mistaken_for_terms():
    # Prose that merely ends with square brackets (a system pane line, a
    # bracketed aside) must not trip the trailing-terms guard.
    raw = (
        "TITLE_EN: A Title\n"
        "=====BODY=====\n"
        "He read the panel aloud.\n\n"
        "[Quest Complete: Slay the Boar]"
    )
    result = parse_delimited_response(raw)
    assert result.translated_text.endswith("[Quest Complete: Slay the Boar]")
    assert result.new_terms == []


def test_well_formed_envelope_unchanged():
    raw = (
        "TITLE_EN: A Title\n"
        "=====BODY=====\n"
        "Body prose.\n"
        "=====TERMS=====\n"
        '[{"zh": "甲", "en": "A", "category": "character"}]'
    )
    result = parse_delimited_response(raw)
    assert result.translated_text == "Body prose."
    assert [t.en for t in result.new_terms] == ["A"]
