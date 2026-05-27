"""Tests for the atomic-vs-soft locked-term classifier and the
parenthetical-stripping / slash-aware noise reduction in
`missing_translator_terms`.

The classifier is the single gate between "this locked term has its casing
mechanically enforced" and "this locked term is soft — translator discretion
within the prompt." Both the corpus inspection (data/novels.db) and a
representative synthetic table are tested here.
"""

from __future__ import annotations

from backend.models import GlossaryEntry
from backend.services.glossary import (
    _check_variants,
    is_atomic_case_locked_term,
    missing_translator_terms,
)


def _entry(
    term_en: str,
    *,
    category: str = "other",
    notes: str | None = None,
    locked: bool = True,
    term_zh: str = "X",
) -> GlossaryEntry:
    return GlossaryEntry(
        id=1, novel_id=1, term_zh=term_zh, term_en=term_en, category=category,
        notes=notes, auto_detected=False, locked=locked,
    )


# ---------------------------------------------------------------------------
# is_atomic_case_locked_term — the sanity table from the plan
# ---------------------------------------------------------------------------


def test_classifier_unlocked_returns_false() -> None:
    g = _entry("Chen-Earth", locked=False)
    assert is_atomic_case_locked_term(g) is False


def test_classifier_atomic_proper_categories_true() -> None:
    # character / place / technique / item are unambiguously atomic.
    for cat in ("character", "place", "technique", "item"):
        g = _entry("Eternal Radiance", category=cat)
        assert is_atomic_case_locked_term(g) is True, cat


def test_classifier_idiom_category_false() -> None:
    # Idioms render lowercase by policy — never force-cased.
    g = _entry("hitting the autumn wind", category="idiom")
    assert is_atomic_case_locked_term(g) is False


def test_classifier_other_with_hyphen_true() -> None:
    # Stem-Branch shape is atomic in category=other.
    assert is_atomic_case_locked_term(_entry("Chen-Earth")) is True
    assert is_atomic_case_locked_term(_entry("Si-Fire")) is True
    assert is_atomic_case_locked_term(_entry("Geng-Metal")) is True


def test_classifier_other_multiword_titlecase_true() -> None:
    # Multi-word with Title-Case non-function word is atomic in category=other.
    assert is_atomic_case_locked_term(_entry("Fruition Attainment")) is True
    assert is_atomic_case_locked_term(_entry("Karma Tribulation")) is True
    assert is_atomic_case_locked_term(_entry("Merit Bomb")) is True


def test_classifier_other_single_word_lowercase_false() -> None:
    # Single-word common nouns in `other` are not atomic.
    assert is_atomic_case_locked_term(_entry("restart")) is False
    assert is_atomic_case_locked_term(_entry("overpowered")) is False


def test_classifier_other_multiword_lowercase_false() -> None:
    # Multi-word with NO Title-Case non-function word is not atomic.
    # `newbie village` (lowercase) is a deliberate gaming-register term.
    assert is_atomic_case_locked_term(_entry("newbie village")) is False


def test_classifier_lowercase_note_overrides_structure() -> None:
    # Explicit user `lowercase` note forces soft, regardless of structure.
    for term, cat in [
        ("Spiritual Power", "other"),
        ("Divine Sense", "other"),
        ("Heavenly Secrets", "other"),
        ("Killing Karma", "other"),
    ]:
        g = _entry(term, category=cat, notes="lowercase")
        assert is_atomic_case_locked_term(g) is False, term


def test_classifier_lowercase_note_case_insensitive() -> None:
    # The notes check is case-insensitive.
    for note in ("LOWERCASE", "Lowercase term", "frequent | lowercase"):
        g = _entry("Spiritual Power", notes=note)
        assert is_atomic_case_locked_term(g) is False, note


def test_classifier_slash_alternative_false() -> None:
    # Slash-alternative term_en is soft.
    for sep in ("/", "／", "∕"):
        g = _entry(f"Karma {sep} Karmic Threads")
        assert is_atomic_case_locked_term(g) is False, sep


def test_classifier_parenthetical_metadata_false() -> None:
    g = _entry("Demonic Path (philosophy/affiliation)")
    assert is_atomic_case_locked_term(g) is False


def test_classifier_generic_rank_false() -> None:
    # _GENERIC_RANK_RE catches second-rank / late-stage / etc.
    for term in ("Second-Rank", "late-stage", "third-tier"):
        g = _entry(term)
        assert is_atomic_case_locked_term(g) is False, term


def test_classifier_empty_term_en_false() -> None:
    assert is_atomic_case_locked_term(_entry("")) is False


def test_classifier_apostrophe_name_atomic() -> None:
    # Yun family's Old Ancestor — locked-style character name with embedded 's
    # IS atomic (this is exactly the Ch297 regression case).
    g = _entry("Yun family's Old Ancestor", category="character")
    assert is_atomic_case_locked_term(g) is True

    g2 = _entry("True Person Sea's Roar", category="character")
    assert is_atomic_case_locked_term(g2) is True


# ---------------------------------------------------------------------------
# _check_variants — parenthetical / slash decomposition for checks
# ---------------------------------------------------------------------------


def test_check_variants_strips_trailing_parenthetical() -> None:
    assert _check_variants("Demonic Path (philosophy/affiliation)") == ["Demonic Path"]


def test_check_variants_splits_slash_alternatives() -> None:
    assert _check_variants("Karma / Karmic Threads") == ["Karma", "Karmic Threads"]


def test_check_variants_handles_slash_plus_parenthetical() -> None:
    out = _check_variants(
        "Demonic Path (general) / Demonic Sect (organizational)"
    )
    assert out == ["Demonic Path", "Demonic Sect"]


def test_check_variants_plain_term_unchanged() -> None:
    assert _check_variants("Fruition Attainment") == ["Fruition Attainment"]


def test_check_variants_empty_returns_empty() -> None:
    assert _check_variants("") == []
    assert _check_variants("   ") == []


# ---------------------------------------------------------------------------
# missing_translator_terms — noise reduction in action
# ---------------------------------------------------------------------------


def test_missing_terms_parenthetical_passes_when_substantive_present() -> None:
    # `Demonic Path (philosophy/affiliation)` should pass if `Demonic Path`
    # appears in the translation.
    g = [_entry("Demonic Path (philosophy/affiliation)", term_zh="魔道")]
    src = "他选择了魔道。"
    trans = "He chose the Demonic Path."
    assert missing_translator_terms(src, trans, g) == []


def test_missing_terms_slash_passes_when_either_appears() -> None:
    g = [_entry("Karma / Karmic Threads", term_zh="因果")]
    src = "因果之间，命运纠缠。"
    trans = "Between karmic threads, fate entangled."
    assert missing_translator_terms(src, trans, g) == []


def test_missing_terms_atomic_still_case_sensitive() -> None:
    # Atomic locked terms (Chen-Earth) must still flag on casing drift.
    g = [_entry("Chen-Earth", term_zh="辰土", category="other")]
    src = "辰土之力涌动。"
    trans = "The chen-earth force surged."  # wrong casing
    missing = missing_translator_terms(src, trans, g)
    assert missing == [("辰土", "Chen-Earth")]


def test_missing_terms_soft_row_case_insensitive() -> None:
    # Soft locked terms (lowercase-noted) accept any casing in translation.
    g = [_entry("Divine Sense", category="other", notes="lowercase", term_zh="神识")]
    src = "他的神识扫过房间。"
    trans = "His Divine Sense swept the room."  # uppercased — still OK
    assert missing_translator_terms(src, trans, g) == []
    # And lowercase also OK.
    assert missing_translator_terms(src, "his divine sense swept", g) == []


def test_missing_terms_truly_missing_atomic_still_flagged() -> None:
    g = [_entry("Fruition Attainment", term_zh="果位", category="other")]
    src = "果位的奥秘。"
    trans = "The mysteries of seat attainment."  # wrong rendering
    missing = missing_translator_terms(src, trans, g)
    assert missing == [("果位", "Fruition Attainment")]
