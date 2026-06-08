"""Unit tests for `enforce_balanced_emphasis` in text_fixups.

The translator emits Markdown the reader renders with `marked`: `**bold**` for
`**【Field: Value】**` system lines and `*italics*` for inner thought. When the
model drops one half of a pair, the leftover delimiter has no partner in its
paragraph; `marked` cannot match it and renders a literal asterisk (a "stray
symbol"). This fixup removes such unpaired delimiters per paragraph while
leaving balanced ones — the intended bold / italic — untouched.

Pure transform returning `(text, count)`.
"""

from __future__ import annotations

from backend.services.text_fixups import enforce_balanced_emphasis


# ---------------------------------------------------------------------------
# Stray (unpaired) delimiters are removed
# ---------------------------------------------------------------------------


def test_trailing_stray_bold_removed():
    # The chapter-372 defect: a standalone technique-name paragraph with a
    # trailing `**` and no opener.
    out, n = enforce_balanced_emphasis("Sword Heart Illumination.**")
    assert out == "Sword Heart Illumination."
    assert n == 1


def test_leading_stray_bold_removed():
    # The chapter-2 defect: a paragraph that opens with `**` and never closes.
    out, n = enforce_balanced_emphasis("**Four: Forgo all gains.")
    assert out == "Four: Forgo all gains."
    assert n == 1


def test_trailing_stray_after_punctuation_removed():
    out, n = enforce_balanced_emphasis("Illuminating Clarity!**")
    assert out == "Illuminating Clarity!"
    assert n == 1


def test_stray_single_italic_removed():
    out, n = enforce_balanced_emphasis("He whispered *something to himself.")
    assert out == "He whispered something to himself."
    assert n == 1


# ---------------------------------------------------------------------------
# Balanced delimiters are preserved
# ---------------------------------------------------------------------------


def test_balanced_bold_pane_kept():
    src = "**【Got Some Skills: handle it with ease for two moves.】**"
    out, n = enforce_balanced_emphasis(src)
    assert out == src
    assert n == 0


def test_balanced_italic_thought_kept():
    src = "*What in the world is going on?*"
    out, n = enforce_balanced_emphasis(src)
    assert out == src
    assert n == 0


def test_bold_italic_triple_run_kept():
    src = "***fully emphasized***"
    out, n = enforce_balanced_emphasis(src)
    assert out == src
    assert n == 0


def test_inline_balanced_bold_kept():
    src = "The **Foundation Establishment** realm came next."
    out, n = enforce_balanced_emphasis(src)
    assert out == src
    assert n == 0


# ---------------------------------------------------------------------------
# Paragraph scope: delimiters never pair across a blank line
# ---------------------------------------------------------------------------


def test_paragraphs_balanced_independently():
    # Two separate one-line paragraphs, each with a single trailing `**`. A
    # whole-text count would be even (2) and miss both; per-paragraph each is a
    # stray. Both are stripped and the balanced pane between them is kept.
    src = (
        "Illuminating Clarity!**\n\n"
        "**【Got Some Skills: ready.】**\n\n"
        "Sword Heart Illumination.**"
    )
    expected = (
        "Illuminating Clarity!\n\n"
        "**【Got Some Skills: ready.】**\n\n"
        "Sword Heart Illumination."
    )
    out, n = enforce_balanced_emphasis(src)
    assert out == expected
    assert n == 2


def test_separators_preserved_verbatim():
    # A run of 3+ newlines between paragraphs must survive untouched.
    src = "First.**\n\n\nSecond paragraph stays."
    out, n = enforce_balanced_emphasis(src)
    assert out == "First.\n\n\nSecond paragraph stays."
    assert n == 1


def test_only_stray_paragraph_changes():
    src = "*A clean thought.*\n\nA broken header.**\n\n*Another clean thought.*"
    out, n = enforce_balanced_emphasis(src)
    assert out == "*A clean thought.*\n\nA broken header.\n\n*Another clean thought.*"
    assert n == 1


# ---------------------------------------------------------------------------
# No-ops and idempotency
# ---------------------------------------------------------------------------


def test_no_asterisks_is_noop():
    src = "Plain prose with no markdown at all."
    out, n = enforce_balanced_emphasis(src)
    assert out == src
    assert n == 0


def test_empty_string_is_noop():
    out, n = enforce_balanced_emphasis("")
    assert out == ""
    assert n == 0


def test_idempotent():
    src = (
        "Illuminating Clarity!**\n\n"
        "*A real thought.*\n\n"
        "**Four: Forgo all gains."
    )
    once, n1 = enforce_balanced_emphasis(src)
    twice, n2 = enforce_balanced_emphasis(once)
    assert twice == once
    assert n2 == 0
    assert n1 == 2
