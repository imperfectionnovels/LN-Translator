"""Phase 3 tests: glossary-context observers.

Covers:
- The new `detect_intensifier_inflation_on_glossary_term` detector — flags
  "the formidable Soaring Firmament" patterns the prompt rule bans.
- The expanded `_PREDICATE_GROUPS` — cast/release, channel/invoke, wield,
  master/learn, practice/cultivate, destroy/shatter, recognize.
- The widened `_GENERIC_RANK_RE` — `top-grade`, `upper-tier`.
"""

from __future__ import annotations

import pytest

from backend.models import GlossaryEntry
from backend.services.glossary_casing import (
    _GENERIC_RANK_RE,
    _normalize_extracted_casing,
)
from backend.services.text_observers import (
    detect_glossary_predicate_loss,
    detect_intensifier_inflation_on_glossary_term,
)


def _locked_entry(zh: str, en: str, category: str = "character") -> GlossaryEntry:
    return GlossaryEntry(
        id=1, novel_id=1, term_zh=zh, term_en=en, category=category,
        notes=None, usage_note=None, auto_detected=False, locked=True,
    )


# ============================================================================
# Intensifier inflation detector
# ============================================================================

class TestIntensifierInflation:
    def test_flags_formidable_before_locked_term(self):
        glossary = [_locked_entry("昂霄", "Soaring Firmament")]
        text = "He turned to face the formidable Soaring Firmament."
        flags = detect_intensifier_inflation_on_glossary_term(text, glossary)
        assert len(flags) == 1
        assert "formidable" in flags[0].lower()
        assert "soaring firmament" in flags[0].lower()

    def test_flags_mighty_before_locked_term(self):
        glossary = [_locked_entry("庚金", "Geng-Metal", category="other")]
        text = "The mighty Geng-Metal swept through the chamber."
        flags = detect_intensifier_inflation_on_glossary_term(text, glossary)
        assert len(flags) == 1
        assert "mighty" in flags[0].lower()

    def test_no_flag_when_intensifier_is_part_of_locked_term(self):
        """If the combined `<intensifier> <term>` phrase is itself a locked
        term (e.g. some novel has 'Mighty Sword Art' as a legitimate name),
        the suppression branch must keep it from firing."""
        glossary = [_locked_entry("强剑诀", "Mighty Sword Art", category="technique")]
        text = "He drew the Mighty Sword Art from its scabbard."
        flags = detect_intensifier_inflation_on_glossary_term(text, glossary)
        assert flags == []

    def test_no_flag_on_unlocked_term(self):
        """Auto-detected entries don't get the protection — only locked
        (user-curated) terms must sit bare."""
        entry = _locked_entry("昂霄", "Soaring Firmament")
        entry = entry.model_copy(update={"locked": False, "auto_detected": True})
        text = "The mighty Soaring Firmament rose into the sky."
        flags = detect_intensifier_inflation_on_glossary_term(text, [entry])
        assert flags == []

    def test_no_flag_without_glossary(self):
        text = "The formidable warrior advanced."
        assert detect_intensifier_inflation_on_glossary_term(text, []) == []
        assert detect_intensifier_inflation_on_glossary_term(text, None) == []

    def test_no_flag_when_intensifier_modifies_unrelated_noun(self):
        glossary = [_locked_entry("昂霄", "Soaring Firmament")]
        text = "The formidable peak loomed ahead. Soaring Firmament stood beside him."
        flags = detect_intensifier_inflation_on_glossary_term(text, glossary)
        # "formidable peak" is not a locked term; no flag.
        assert flags == []

    def test_handles_the_prefix(self):
        glossary = [_locked_entry("昂霄", "Soaring Firmament")]
        text = "Above them rose the mighty Soaring Firmament."
        flags = detect_intensifier_inflation_on_glossary_term(text, glossary)
        assert len(flags) == 1

    def test_flags_partial_match_with_trailing_word(self):
        """A locked term followed by an extra modifier in the text still flags
        the intensifier — 'formidable Soaring Firmament technique' should
        flag because the head 'Soaring Firmament' is the locked term."""
        glossary = [_locked_entry("昂霄", "Soaring Firmament")]
        text = "He invoked the formidable Soaring Firmament technique."
        flags = detect_intensifier_inflation_on_glossary_term(text, glossary)
        assert len(flags) == 1

    def test_deduplicates_repeat_offenses(self):
        glossary = [_locked_entry("昂霄", "Soaring Firmament")]
        text = (
            "The mighty Soaring Firmament struck. "
            "The mighty Soaring Firmament struck again."
        )
        flags = detect_intensifier_inflation_on_glossary_term(text, glossary)
        # Same span repeated → single flag (dedup).
        assert len(flags) == 1

    def test_does_not_flag_divine_supreme_eternal(self):
        """Common cultivation-domain proper-noun words like 'Divine' /
        'Supreme' / 'Eternal' are NOT in the intensifier list because they
        are frequently legitimate parts of glossary names. Verify by
        constructing a case that would falsely flag if they were included."""
        glossary = [_locked_entry("光辉", "Radiance")]
        text = "Above the city shone the eternal Radiance."
        # 'eternal' is not in _INTENSIFIER_WORDS — must not flag.
        flags = detect_intensifier_inflation_on_glossary_term(text, glossary)
        assert flags == []

    def test_does_not_overcapture_trailing_lowercase_prose(self):
        """Regex without re.IGNORECASE on the whole pattern: the term
        capture must stop at the locked title boundary instead of slurping
        trailing lowercase words like 'struck again' into the term group."""
        glossary = [_locked_entry("昂霄", "Soaring Firmament")]
        text = "The mighty Soaring Firmament struck again from the shadows."
        flags = detect_intensifier_inflation_on_glossary_term(text, glossary)
        assert len(flags) == 1
        # The flagged span should be the intensifier + locked term, NOT
        # spilled into trailing prose. We check for the absence of "struck"
        # and "shadows" — those are downstream of the term boundary.
        assert "struck" not in flags[0].lower()
        assert "shadows" not in flags[0].lower()
        # And the actual flagged span should still contain both halves.
        assert "soaring firmament" in flags[0].lower()
        assert "mighty" in flags[0].lower()

    def test_nested_locked_aliases_do_not_false_flag(self):
        """When BOTH 'Mighty Sword Art' (full name) and 'Sword Art' (nested
        type) are locked, the model writing the legitimate full name must
        not be flagged. The head-match would land on 'Sword Art' and
        falsely flag the intensifier — suppression branch (b) must catch
        this by checking whether `<intensifier> <head_match>` is also locked.
        """
        glossary = [
            _locked_entry("强剑诀", "Mighty Sword Art", category="technique"),
            _locked_entry("剑诀", "Sword Art", category="technique"),
        ]
        text = "He drew the Mighty Sword Art from its scabbard."
        flags = detect_intensifier_inflation_on_glossary_term(text, glossary)
        assert flags == [], (
            f"nested-locked false positive: {flags!r}. "
            f"'Mighty Sword Art' is the legitimate full name."
        )

    def test_intensifier_word_is_case_insensitive(self):
        """The intensifier alternation itself must still match regardless
        of casing (so 'MIGHTY' or 'Mighty' triggers the same as 'mighty')
        — only the TERM capture is case-sensitive."""
        glossary = [_locked_entry("昂霄", "Soaring Firmament")]
        for variant in (
            "Mighty Soaring Firmament rose.",
            "MIGHTY Soaring Firmament rose.",
            "the mighty Soaring Firmament rose.",
        ):
            flags = detect_intensifier_inflation_on_glossary_term(variant, glossary)
            assert len(flags) == 1, f"failed on {variant!r}: {flags!r}"


# ============================================================================
# Expanded predicate-loss verb groups
# ============================================================================

class TestExpandedPredicateGroups:
    def test_cast_release_group_flags_loss(self):
        glossary = [_locked_entry("剑诀", "Sword Technique", category="technique")]
        cn = "他施展剑诀，斩向妖魔。"
        en = "He drew Sword Technique against the demon."  # missing cast/release
        flags = detect_glossary_predicate_loss(cn, en, glossary)
        assert any("施展" in f for f in flags)

    def test_cast_release_group_accepts_unleashed(self):
        glossary = [_locked_entry("剑诀", "Sword Technique", category="technique")]
        cn = "他施展剑诀，斩向妖魔。"
        en = "He unleashed Sword Technique against the demon."
        flags = detect_glossary_predicate_loss(cn, en, glossary)
        assert flags == []

    def test_channel_invoke_group_flags_loss(self):
        glossary = [_locked_entry("真元", "Primal Essence", category="other")]
        cn = "他催动真元，护体而行。"
        en = "He held Primal Essence around him as he walked."  # missing channel/invoke
        flags = detect_glossary_predicate_loss(cn, en, glossary)
        assert any("催动" in f for f in flags)

    def test_channel_invoke_accepts_channeled(self):
        glossary = [_locked_entry("真元", "Primal Essence", category="other")]
        cn = "他催动真元，护体而行。"
        en = "He channeled Primal Essence around him as he walked."
        flags = detect_glossary_predicate_loss(cn, en, glossary)
        assert flags == []

    def test_wield_hold_group_flags_loss(self):
        glossary = [_locked_entry("拓地舟", "Territory-Expanding Ship", category="item")]
        cn = "他手持拓地舟，立于山巅。"
        en = "He stood on the peak with Territory-Expanding Ship beside him."  # missing wield
        flags = detect_glossary_predicate_loss(cn, en, glossary)
        assert any("手持" in f for f in flags)

    def test_wield_hold_accepts_gripped(self):
        glossary = [_locked_entry("拓地舟", "Territory-Expanding Ship", category="item")]
        cn = "他手持拓地舟，立于山巅。"
        en = "He gripped Territory-Expanding Ship and stood on the peak."
        flags = detect_glossary_predicate_loss(cn, en, glossary)
        assert flags == []

    def test_master_learn_group_flags_loss(self):
        glossary = [_locked_entry("剑意", "Sword Intent", category="technique")]
        cn = "他终于领悟剑意。"
        en = "Sword Intent finally settled in him."  # missing master/learn/comprehend
        flags = detect_glossary_predicate_loss(cn, en, glossary)
        assert any("领悟" in f for f in flags)

    def test_master_learn_accepts_comprehended(self):
        glossary = [_locked_entry("剑意", "Sword Intent", category="technique")]
        cn = "他终于领悟剑意。"
        en = "He finally comprehended Sword Intent."
        flags = detect_glossary_predicate_loss(cn, en, glossary)
        assert flags == []

    def test_practice_cultivate_flags_loss(self):
        glossary = [_locked_entry("功法", "Method", category="technique")]
        cn = "他修炼功法多年。"
        en = "Method had been his constant companion for years."  # missing practice/cultivate
        flags = detect_glossary_predicate_loss(cn, en, glossary)
        assert any("修炼" in f for f in flags)

    def test_practice_cultivate_accepts_cultivated(self):
        glossary = [_locked_entry("功法", "Method", category="technique")]
        cn = "他修炼功法多年。"
        en = "He cultivated Method for many years."
        flags = detect_glossary_predicate_loss(cn, en, glossary)
        assert flags == []

    def test_destroy_shatter_flags_loss(self):
        glossary = [_locked_entry("印记", "Sigil", category="item")]
        cn = "他摧毁印记，斩断牵连。"
        en = "Sigil lay before him, the connection severed."  # missing destroy/shatter
        flags = detect_glossary_predicate_loss(cn, en, glossary)
        assert any("摧毁" in f for f in flags)

    def test_destroy_shatter_accepts_shattered(self):
        glossary = [_locked_entry("印记", "Sigil", category="item")]
        cn = "他摧毁印记，斩断牵连。"
        en = "He shattered Sigil, severing the connection."
        flags = detect_glossary_predicate_loss(cn, en, glossary)
        assert flags == []

    def test_recognize_identify_flags_loss(self):
        glossary = [_locked_entry("洞天", "Grotto-Heaven", category="place")]
        cn = "他认出洞天的形态。"
        en = "Grotto-Heaven took shape before his eyes."  # missing recognize/identify
        flags = detect_glossary_predicate_loss(cn, en, glossary)
        assert any("认出" in f for f in flags)

    def test_recognize_identify_accepts_recognized(self):
        glossary = [_locked_entry("洞天", "Grotto-Heaven", category="place")]
        cn = "他认出洞天的形态。"
        en = "He recognized Grotto-Heaven by its shape."
        flags = detect_glossary_predicate_loss(cn, en, glossary)
        assert flags == []


# ============================================================================
# Widened _GENERIC_RANK_RE (top, upper)
# ============================================================================

class TestGenericRankPrefixes:
    @pytest.mark.parametrize("term", [
        "top-grade", "top-rank", "top-tier",
        "upper-tier", "upper-grade", "upper-rank",
        "Top-Grade", "UPPER-RANK",  # case-insensitive
    ])
    def test_top_upper_treated_as_rank_descriptors(self, term):
        assert _GENERIC_RANK_RE.match(term) is not None

    @pytest.mark.parametrize("term", [
        "top-grade", "Top-Grade", "TOP-GRADE",
        "upper-tier", "Upper-Tier",
    ])
    def test_top_upper_lowercased_on_extraction(self, term):
        # Whole-string match → lowercase.
        result = _normalize_extracted_casing(term, "other")
        assert result == term.lower()

    def test_multiword_term_with_top_not_lowercased(self):
        """`_normalize_extracted_casing` only lowercases when the WHOLE
        string matches. A technique name that happens to start with
        'Top-Grade' but continues with proper-noun words must stay as-is."""
        # Anchored regex won't match a multi-word string.
        result = _normalize_extracted_casing("Top-Grade Spirit Stone", "item")
        assert result == "Top-Grade Spirit Stone"


class TestNamedCategoryCasingRepair:
    """A named-category term that arrives all-lowercase is proper-cased on
    extraction, so a lowercase named technique can't be stored and then pinned
    into prose by enforce_locked_term_casing (the 知見障 -> 'cognitive barrier'
    failure)."""

    @pytest.mark.parametrize("category", ["technique", "place", "character", "item"])
    def test_all_lowercase_named_term_titlecased(self, category):
        assert _normalize_extracted_casing("cognitive barrier", category) == (
            "Cognitive Barrier"
        )

    def test_hyphen_parts_capitalized(self):
        assert _normalize_extracted_casing("treasure-light mirror", "item") == (
            "Treasure-Light Mirror"
        )

    def test_interior_function_words_stay_lowercase(self):
        assert _normalize_extracted_casing("hall of yama", "place") == "Hall of Yama"

    def test_existing_casing_left_untouched(self):
        # Any uppercase already present => deliberate; don't re-case.
        assert _normalize_extracted_casing("Marionette", "technique") == "Marionette"
        assert _normalize_extracted_casing("iPhone Sect", "place") == "iPhone Sect"

    def test_other_category_lowercase_preserved(self):
        # `other` mixes named concepts and generics; leave it to the model.
        assert _normalize_extracted_casing("spiritual power", "other") == (
            "spiritual power"
        )

    def test_idiom_lowercase_preserved(self):
        assert _normalize_extracted_casing("courting death", "idiom") == (
            "courting death"
        )
