"""Tests for the queue's deterministic fixup ordering (`_apply_text_fixups`).

C3: `enforce_brackets` can leave a stray ``**`` when a bold-wrapped bracket
span has whitespace before its closing marker (the fixed-offset lookahead and
`_is_inline_span` both misread the ``  **``). Balance must therefore run AFTER
brackets so the half-pair is cleaned.

M4 hardening: the LLM body is normalized to LF before the LF-assuming fixup
chain, mirroring the source-side normalization in `parser.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.services.queue import _apply_text_fixups


def _result(body: str, title: str = "Title"):
    return SimpleNamespace(translated_text=body, title_en=title)


def test_bracket_strip_leaves_no_stray_emphasis():
    # `**【x】  **` (whitespace before the closing bold marker) must not leave a
    # half-pair `**` that renders as a literal asterisk in the reader.
    r = _result("He paused. **【test】  ** The wind rose.")
    _title, cleaned, _counts = _apply_text_fixups(r, [], 1)
    assert "**" not in cleaned
    # The inner word survives (case may shift if it lands at a sentence head).
    assert "test" in cleaned.lower()


def test_clean_bold_bracket_still_stripped():
    # Regression: a well-formed `**【x】**` inline emphasis span still strips
    # cleanly to bare text (no behavior change from the reorder).
    r = _result("He drew **【Longclaw】** and struck.")
    _title, cleaned, _counts = _apply_text_fixups(r, [], 1)
    assert "**" not in cleaned
    assert "Longclaw" in cleaned


def test_body_normalized_to_lf():
    r = _result("First line.\r\n\r\nSecond line.")
    _title, cleaned, _counts = _apply_text_fixups(r, [], 1)
    assert "\r" not in cleaned
    assert "First line." in cleaned and "Second line." in cleaned


def test_fixup_counts_record_what_changed():
    # A bold-bracket span fires brackets (strip) and may fire emphasis; a clean
    # body fires nothing. The audit dict records nonzero rules + a total.
    r = _result("He drew **【Longclaw】** and struck.")
    _title, _cleaned, counts = _apply_text_fixups(r, [], 1)
    assert counts["total"] >= 1
    assert "brackets" in counts["rules"]
    assert all(v > 0 for v in counts["rules"].values())  # zero-count rules omitted

    clean = _result("He drew the sword and struck the foe.")
    _t, _c, clean_counts = _apply_text_fixups(clean, [], 1)
    assert clean_counts == {"rules": {}, "total": 0}
