"""Unit tests for the enforcement-layer completion transforms in text_fixups.

These three deterministic fixups close the gaps the 301-316 audit surfaced:

- `enforce_lowercase_locked_terms`: the missing down-casing direction. Locked
  glossary rows whose notes say `lowercase` are inert in the up-caser; this
  transform forces their occurrences down to the lowercase canonical so
  generics (avatar, divine sense, sea of consciousness) stop rendering Title
  Case no matter what the model wrote.
- `strip_chapter_end_marker`: strips a leaked trailing `(本章完)` / `(End of
  Chapter)` CMS sentinel (it leaked in 11 of 16 sampled chapters).
- `enforce_sentence_initial_capitalization`: re-capitalizes sentence starts so
  an inserted lowercase proper noun ("the Heaven of Non-Being...") at a
  sentence head reads correctly.

All three are pure and return `(text, count)`.
"""

from __future__ import annotations

from backend.models import GlossaryEntry
from backend.services.text_fixups import (
    enforce_lowercase_locked_terms,
    enforce_sentence_initial_capitalization,
    strip_chapter_end_marker,
)


def _lc_entry(
    term_en: str, *, term_zh: str = "X", notes: str | None = "lowercase",
    locked: bool = True, category: str = "other",
) -> GlossaryEntry:
    return GlossaryEntry(
        id=1, novel_id=1, term_zh=term_zh, term_en=term_en, category=category,
        notes=notes, auto_detected=False, locked=locked,
    )


# ---------------------------------------------------------------------------
# enforce_lowercase_locked_terms
# ---------------------------------------------------------------------------


def test_lowercase_downcases_mid_sentence() -> None:
    g = [_lc_entry("avatar")]
    out, n = enforce_lowercase_locked_terms("He hid his Avatar from view.", g)
    assert out == "He hid his avatar from view."
    assert n == 1


def test_lowercase_skips_mixed_case_term_en() -> None:
    # Safety: only rows whose term_en is already all-lowercase are down-cased.
    # A named-realm row like 虛瞑之地 -> "the Void" must never be lowercased even
    # if its notes mention lowercase; fix-glossary lowercases a row first to
    # opt it in.
    g = [_lc_entry("Mortal", term_zh="凡人", category="character")]
    out, n = enforce_lowercase_locked_terms("the Mortal world below", g)
    assert out == "the Mortal world below"
    assert n == 0


def test_lowercase_multiword_term() -> None:
    # The glossary row has been opted in (term_en lowercased by fix-glossary).
    g = [_lc_entry("sea of consciousness", term_zh="识海")]
    out, n = enforce_lowercase_locked_terms(
        "his spiritual platform and Sea of Consciousness", g
    )
    assert out == "his spiritual platform and sea of consciousness"
    assert n == 1


def test_lowercase_skips_sentence_initial() -> None:
    # A generic at a sentence head is correctly capitalized; don't down-case it.
    g = [_lc_entry("avatar")]
    out, n = enforce_lowercase_locked_terms("Avatar bodies are rare.", g)
    assert out == "Avatar bodies are rare."
    assert n == 0


def test_lowercase_protects_forward_proper_noun_compound() -> None:
    # "Ghost Mountain" is a place; "Ghost" is followed by a capitalized word, so
    # the down-caser must not touch it.
    g = [_lc_entry("ghost", term_zh="鬼", category="character")]
    out, n = enforce_lowercase_locked_terms("He climbed Ghost Mountain at dusk.", g)
    assert out == "He climbed Ghost Mountain at dusk."
    assert n == 0


def test_lowercase_downcases_when_followed_by_lowercase() -> None:
    g = [_lc_entry("ghost", term_zh="鬼", category="character")]
    out, n = enforce_lowercase_locked_terms("He could roam like a Ghost back then.", g)
    assert out == "He could roam like a ghost back then."
    assert n == 1


def test_lowercase_skips_proper_caveat_entries() -> None:
    # 虚空 -> "the void" lowercase as a concept, but "capitalize when proper
    # place" — the caveat means it is context-dependent; never auto-down-case.
    g = [_lc_entry(
        "void", term_zh="虚空", category="place",
        notes="lowercase as concept; capitalize when proper place",
    )]
    out, n = enforce_lowercase_locked_terms("He vanished into the Void.", g)
    assert out == "He vanished into the Void."
    assert n == 0


def test_lowercase_backward_proper_noun_compound_protected() -> None:
    # "divine ability" is a substring of the named slot "Innate Divine
    # Ability"; the down-caser must not lowercase the embedded words there,
    # while a bare "his Divine Ability" still down-cases.
    g = [_lc_entry("divine ability", term_zh="神通", category="technique")]
    out, n = enforce_lowercase_locked_terms(
        "his Innate Divine Ability flared as his Divine Ability surged", g
    )
    assert "Innate Divine Ability" in out
    assert "his divine ability surged" in out
    assert n == 1


def test_lowercase_allows_capitalized_function_word_before() -> None:
    # A capitalized function word ("His") is not a proper-noun compound, so the
    # generic after it still down-cases.
    g = [_lc_entry("avatar")]
    out, n = enforce_lowercase_locked_terms("His Avatar drifted away.", g)
    assert out == "His avatar drifted away."
    assert n == 1


def test_lowercase_ignores_non_lowercase_noted_entries() -> None:
    # Entries without a `lowercase` note belong to the up-caser; the down-caser
    # leaves them entirely alone.
    g = [_lc_entry("Sea of Consciousness", notes=None)]
    out, n = enforce_lowercase_locked_terms("the Sea of Consciousness churned", g)
    assert out == "the Sea of Consciousness churned"
    assert n == 0


def test_lowercase_skips_unlocked_entries() -> None:
    g = [_lc_entry("avatar", locked=False)]
    out, n = enforce_lowercase_locked_terms("his Avatar moved", g)
    assert out == "his Avatar moved"
    assert n == 0


def test_lowercase_skips_code_fence() -> None:
    g = [_lc_entry("avatar")]
    text = "```\nAvatar = 1\n```\nhis Avatar moved"
    out, n = enforce_lowercase_locked_terms(text, g)
    assert "```\nAvatar = 1\n```" in out
    assert "his avatar moved" in out
    assert n == 1


def test_lowercase_idempotent() -> None:
    g = [_lc_entry("avatar")]
    once, n1 = enforce_lowercase_locked_terms("his Avatar and her Avatar", g)
    twice, n2 = enforce_lowercase_locked_terms(once, g)
    assert n1 == 2
    assert n2 == 0
    assert once == twice


def test_lowercase_empty() -> None:
    assert enforce_lowercase_locked_terms("", [_lc_entry("avatar")]) == ("", 0)
    assert enforce_lowercase_locked_terms("text", None) == ("text", 0)


# ---------------------------------------------------------------------------
# strip_chapter_end_marker
# ---------------------------------------------------------------------------


def test_strip_marker_english_titlecase() -> None:
    out, n = strip_chapter_end_marker("The gate opened.\n\n(End of Chapter)")
    assert out == "The gate opened."
    assert n == 1


def test_strip_marker_english_lowercase() -> None:
    out, n = strip_chapter_end_marker("The gate opened.\n\n(end of chapter)\n")
    assert out == "The gate opened."
    assert n == 1


def test_strip_marker_cjk_parens() -> None:
    out, n = strip_chapter_end_marker("结束了。\n\n(本章完)")
    assert out == "结束了。"
    assert n == 1


def test_strip_marker_fullwidth_parens() -> None:
    out, n = strip_chapter_end_marker("done\n\n（本章完）")
    assert out == "done"
    assert n == 1


def test_strip_marker_bare_cjk() -> None:
    out, n = strip_chapter_end_marker("done\n\n本章完")
    assert out == "done"
    assert n == 1


def test_strip_marker_only_trailing() -> None:
    # A marker that is not the last non-empty block is left alone (defensive).
    text = "本章完\n\nThis is still real body text that follows."
    out, n = strip_chapter_end_marker(text)
    assert out == text
    assert n == 0


def test_strip_marker_clean_text_unchanged() -> None:
    out, n = strip_chapter_end_marker("A normal ending sentence.")
    assert out == "A normal ending sentence."
    assert n == 0


def test_strip_marker_empty() -> None:
    assert strip_chapter_end_marker("") == ("", 0)


# ---------------------------------------------------------------------------
# enforce_sentence_initial_capitalization
# ---------------------------------------------------------------------------


def test_sentence_initial_text_start_and_after_period() -> None:
    out, n = enforce_sentence_initial_capitalization(
        "the Heaven of Non-Being contained five scenes. it was vast."
    )
    assert out == "The Heaven of Non-Being contained five scenes. It was vast."
    assert n == 2


def test_sentence_initial_ellipsis_not_a_boundary() -> None:
    out, n = enforce_sentence_initial_capitalization("No... calm down. it ended.")
    assert out == "No... calm down. It ended."
    assert n == 1


def test_sentence_initial_paragraph_start() -> None:
    out, n = enforce_sentence_initial_capitalization("He left.\n\nthe end came.")
    assert out == "He left.\n\nThe end came."
    assert n == 1


def test_sentence_initial_skips_leading_quote() -> None:
    out, n = enforce_sentence_initial_capitalization('"come here," she said.')
    assert out == '"Come here," she said.'
    assert n == 1


def test_sentence_initial_intervening_quote_not_recapped() -> None:
    # `."` then a lowercase tag is left alone (conservative: avoids breaking a
    # dialogue tag whose period should have been a comma).
    text = 'He said "go." then silence fell.'
    out, n = enforce_sentence_initial_capitalization(text)
    assert out == text
    assert n == 0


def test_sentence_initial_skips_code_fence() -> None:
    # Content inside the fence stays lowercase; a real prose sentence that
    # follows a terminator-ending paragraph is capitalized.
    text = "```\nthe code stays\n```\n\nHe left.\n\nthe town slept."
    out, n = enforce_sentence_initial_capitalization(text)
    assert "```\nthe code stays\n```" in out
    assert "The town slept." in out
    assert n == 1


def test_sentence_initial_skips_mid_sentence_paragraph_break() -> None:
    # A paragraph that continues a sentence (prev line ends in a comma) must
    # NOT have its first word capitalized — that would mask the logged defect.
    text = "He paused,\n\nand then continued toward the hall."
    out, n = enforce_sentence_initial_capitalization(text)
    assert out == text
    assert n == 0


def test_sentence_initial_idempotent() -> None:
    text = "the dawn broke. the city stirred."
    once, n1 = enforce_sentence_initial_capitalization(text)
    twice, n2 = enforce_sentence_initial_capitalization(once)
    assert n1 == 2
    assert n2 == 0
    assert once == twice


def test_sentence_initial_empty() -> None:
    assert enforce_sentence_initial_capitalization("") == ("", 0)
