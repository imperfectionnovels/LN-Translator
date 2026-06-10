"""Unit tests for `enforce_mid_sentence_comma_break` in text_fixups.

The translator occasionally splits one sentence across a `\\n\\n` paragraph
break at a comma (e.g. ch339: "Yet to Lü Yang's surprise, ⏎⏎ Soaring Firmament
showed no alarm…"). A paragraph never legitimately ends in a comma/semicolon, so
this deterministic backstop joins the two halves back into one line. It is
narrow by design: colon intros and bare-lowercase label lines are left alone.

Pure transform returning `(text, count)`.
"""

from __future__ import annotations

from backend.services.text_fixups import enforce_mid_sentence_comma_break


# ---------------------------------------------------------------------------
# Comma / semicolon breaks ARE joined
# ---------------------------------------------------------------------------


def test_joins_comma_break_with_trailing_space():
    # The exact ch339 signature: comma + trailing space + blank line.
    out, n = enforce_mid_sentence_comma_break(
        "Yet to Lü Yang's surprise, \n\nSoaring Firmament showed no alarm."
    )
    assert out == "Yet to Lü Yang's surprise, Soaring Firmament showed no alarm."
    assert n == 1


def test_joins_comma_break_without_trailing_space():
    out, n = enforce_mid_sentence_comma_break("He paused,\n\nand then continued.")
    assert out == "He paused, and then continued."
    assert n == 1


def test_joins_fullwidth_comma_break():
    out, n = enforce_mid_sentence_comma_break("话音未落，\n\n昂霄毫无惊慌。")
    assert out == "话音未落， 昂霄毫无惊慌。"
    assert n == 1


def test_joins_semicolon_break():
    out, n = enforce_mid_sentence_comma_break("The first part held;\n\nthe rest did not.")
    assert out == "The first part held; the rest did not."
    assert n == 1


def test_joins_chained_comma_run_into_one_line():
    # A vertical comma-list collapses to a single comma series.
    out, n = enforce_mid_sentence_comma_break("apples,\n\noranges,\n\nand pears.")
    assert out == "apples, oranges, and pears."
    assert n == 2


# ---------------------------------------------------------------------------
# Legitimate breaks are NOT touched
# ---------------------------------------------------------------------------


def test_leaves_clean_terminal_break():
    text = "He stood at the gate.\n\nThe wind howled past."
    out, n = enforce_mid_sentence_comma_break(text)
    assert out == text
    assert n == 0


def test_leaves_colon_intro_break():
    # `He said:` before content is a legitimate dialogue/list intro.
    text = "He gave a single instruction:\n\nLeave at once."
    out, n = enforce_mid_sentence_comma_break(text)
    assert out == text
    assert n == 0


def test_leaves_standalone_label_line():
    # A bare-lowercase-ending label on its own line (an ability name) must not
    # be glued onto the following sentence.
    text = "Wave of Passing Tribulations\n\nEven so, he ran the mysteries again."
    out, n = enforce_mid_sentence_comma_break(text)
    assert out == text
    assert n == 0


def test_suppressed_when_next_opens_with_double_quote():
    text = 'He said,\n\n"Leave at once."'
    out, n = enforce_mid_sentence_comma_break(text)
    assert out == text
    assert n == 0


def test_suppressed_when_next_opens_with_italic():
    text = "He thought,\n\n*this cannot be happening.*"
    out, n = enforce_mid_sentence_comma_break(text)
    assert out == text
    assert n == 0


def test_suppressed_when_next_opens_with_em_dash():
    text = "He hesitated,\n\n—then said nothing."
    out, n = enforce_mid_sentence_comma_break(text)
    assert out == text
    assert n == 0


def test_suppressed_when_next_opens_with_cjk_book_title():
    # A standalone 《Title》 line (scripture / manual / book name) must not be
    # glued onto a comma-ended paragraph: "…he recalled, 《Manual》" is wrong.
    text = "The name surfaced as he recalled,\n\n《Nine Heavens Manual》"
    out, n = enforce_mid_sentence_comma_break(text)
    assert out == text
    assert n == 0


def test_suppressed_when_next_opens_with_single_angle_bracket():
    text = "He read the title aloud,\n\n〈Preface〉"
    out, n = enforce_mid_sentence_comma_break(text)
    assert out == text
    assert n == 0


# ---------------------------------------------------------------------------
# Edge cases / invariants
# ---------------------------------------------------------------------------


def test_noop_on_single_paragraph():
    text = "A single, comma-laden, paragraph with no break."
    out, n = enforce_mid_sentence_comma_break(text)
    assert out == text
    assert n == 0


def test_noop_on_empty_string():
    out, n = enforce_mid_sentence_comma_break("")
    assert out == ""
    assert n == 0


def test_comma_break_join_idempotent():
    text = "He paused,\n\nand then continued.\n\nThe hall was dark."
    once, n1 = enforce_mid_sentence_comma_break(text)
    twice, n2 = enforce_mid_sentence_comma_break(once)
    assert once == twice
    assert n1 == 1
    assert n2 == 0
