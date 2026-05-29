"""Unit tests for backend.services.text_fixups + text_observers.

`enforce_em_dash` / `enforce_brackets` are pure (no DB, no LLM, no I/O) so the
tests hand-build minimal inputs; both return (transformed_text, count).

The final test exercises the translation queue worker end-to-end to prove the
deterministic text fixups run on every translator commit.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

from backend.db import SCHEMA, open_conn
from backend.main import app
from backend.models import GlossaryEntry
from backend.services import queue
from backend.services.text_fixups import (
    enforce_brackets,
    enforce_em_dash,
    enforce_locked_term_casing,
    enforce_stem_branch_casing,
)
from backend.services.text_observers import (
    detect_double_possessive,
    detect_glossary_predicate_loss,
    detect_locked_idiom_grammar,
    detect_malformed_compounds,
    detect_mid_sentence_paragraph_break,
    detect_mt_texture,
)

DB_PATH = Path(os.environ["DB_PATH"])


def _malformed_glossary_entry(en: str) -> GlossaryEntry:
    return GlossaryEntry(
        id=0, novel_id=1, term_zh="筑基", term_en=en, category="place",
        notes=None, auto_detected=False, locked=True,
    )


def test_malformed_compound_flagged_with_glossary() -> None:
    g = [_malformed_glossary_entry("Foundation Establishment")]
    flagged = detect_malformed_compounds(
        "It was a small early Foundation Establishment clan in the valley.", g
    )
    assert flagged == ["early Foundation Establishment clan"]


def test_malformed_compound_cultivator_is_fine() -> None:
    # A cultivator can hold a stage; only collective nouns are malformed.
    g = [_malformed_glossary_entry("Foundation Establishment")]
    assert detect_malformed_compounds(
        "He was an early Foundation Establishment cultivator.", g
    ) == []


def test_malformed_compound_unknown_realm_not_flagged_with_glossary() -> None:
    # With a glossary, the middle phrase must be a known term — keeps it precise.
    g = [_malformed_glossary_entry("Foundation Establishment")]
    assert detect_malformed_compounds("a late Golden Core sect appeared", g) == []


def test_malformed_compound_no_glossary_fallback() -> None:
    # Without a glossary, a two-word capitalized realm still trips the check.
    assert detect_malformed_compounds("a late Golden Core sect appeared") == [
        "late Golden Core sect"
    ]


def test_malformed_compound_indirect_form_flagged() -> None:
    # The indirect prepositional form ("clan at … the early stage of X") is the
    # same defect as the direct stack — a stage attributed to the group.
    g = [_malformed_glossary_entry("Foundation Establishment")]
    flagged = detect_malformed_compounds(
        "It was a small clan at only the early stage of Foundation Establishment.",
        g,
    )
    assert flagged == [
        "clan at only the early stage of Foundation Establishment"
    ]


def test_malformed_compound_indirect_non_group_subject_not_flagged() -> None:
    # No collective noun before "the early stage of …" — a plain progress
    # phrase must not trip the indirect check.
    g = [_malformed_glossary_entry("Foundation Establishment")]
    assert detect_malformed_compounds(
        "For the early stage of Foundation Establishment, this was top-tier "
        "equipment.",
        g,
    ) == []


def test_malformed_compound_indirect_verb_object_not_flagged() -> None:
    # A transitive verb governs the collective noun, so "at the early stage of
    # …" describes the subject, not the group — not a malformed compound.
    g = [_malformed_glossary_entry("Foundation Establishment")]
    for sentence in (
        "He left the sect at the early stage of Foundation Establishment.",
        "He joined the clan at the late stage of Foundation Establishment.",
        "She founded the family at the early stage of Foundation Establishment.",
    ):
        assert detect_malformed_compounds(sentence, g) == [], sentence


def test_malformed_compound_indirect_subject_position_still_flagged() -> None:
    # With no governing verb, the group itself is characterized by the stage —
    # the indirect defect must still be caught.
    g = [_malformed_glossary_entry("Foundation Establishment")]
    flagged = detect_malformed_compounds(
        "The clan at the early stage of Foundation Establishment had no elders.",
        g,
    )
    assert flagged == ["clan at the early stage of Foundation Establishment"]


def test_malformed_compound_empty() -> None:
    assert detect_malformed_compounds("") == []


def test_mt_texture_flags_heavy_chapter() -> None:
    text = (
        "He could not help but frown. He couldn't help but sigh. "
        "A hint of anger flashed across his eyes. "
        "It must be said that the situation was dire."
    )
    flagged = detect_mt_texture(text)
    assert flagged  # 4 tells — at threshold
    assert any("could not help but" in f for f in flagged)


def test_mt_texture_clean_prose_not_flagged() -> None:
    assert detect_mt_texture(
        "She crossed the courtyard and pushed the gate open. "
        "The morning air was sharp and clean."
    ) == []


def test_mt_texture_is_density_gated() -> None:
    # One isolated tic is below threshold — must not flag.
    assert detect_mt_texture("He could not help but smile at the sight.") == []


def test_mt_texture_empty() -> None:
    assert detect_mt_texture("") == []


def _entry(term_en: str, locked: bool = True, term_zh: str = "") -> GlossaryEntry:
    return GlossaryEntry(
        id=1,
        novel_id=1,
        term_zh=term_zh or term_en,
        term_en=term_en,
        category="character",
        notes=None,
        auto_detected=False,
        locked=locked,
    )


def _idiom_entry(term_en: str, locked: bool = True) -> GlossaryEntry:
    return GlossaryEntry(
        id=2,
        novel_id=1,
        term_zh="找死",
        term_en=term_en,
        category="idiom",
        notes=None,
        auto_detected=not locked,
        locked=locked,
    )


# ---------------------------------------------------------------------------
# enforce_em_dash
# ---------------------------------------------------------------------------


def test_em_dash_mid_clause_becomes_comma():
    text = "He raised his hand — the qi gathered around his fingertips."
    out, count = enforce_em_dash(text)
    assert "—" not in out
    assert "hand, the qi" in out
    assert count == 1


def test_em_dash_before_uppercase_becomes_period():
    text = "He hesitated — Then he struck."
    out, count = enforce_em_dash(text)
    assert "—" not in out
    assert "hesitated. Then" in out
    assert count == 1


def test_em_dash_cutoff_speech_preserved():
    # Skill rule: cut-off speech (`—"` immediately followed by closing quote)
    # is the one allowed case.
    text = 'He shouted, "Lü, you shameless—"'
    out, count = enforce_em_dash(text)
    assert "—" in out
    assert out == text
    assert count == 0


def test_em_dash_cutoff_with_smart_quote_preserved():
    text = "He shouted, “Lü, you shameless—”"
    out, count = enforce_em_dash(text)
    assert out == text
    assert count == 0


def test_em_dash_cjk_double_run_collapsed():
    # `——` is two U+2014 chars; we treat the whole run as one dash insertion.
    text = "He stared ahead —— silent and unmoving."
    out, count = enforce_em_dash(text)
    assert "—" not in out
    assert "ahead, silent" in out
    assert count == 1


def test_em_dash_en_dash_also_replaced():
    text = "He stared ahead – silent."
    out, count = enforce_em_dash(text)
    assert "–" not in out
    assert "ahead, silent" in out
    assert count == 1


def test_em_dash_multiple_in_text_all_handled():
    text = 'He spoke — softly — and then louder. "I told you—"'
    out, count = enforce_em_dash(text)
    # Two narrative em-dashes get replaced, the cutoff one stays.
    assert out.count("—") == 1
    assert count == 2


def test_em_dash_idempotent_on_clean_text():
    text = "He raised his hand, the qi gathered."
    out, count = enforce_em_dash(text)
    assert out == text
    assert count == 0


# ---------------------------------------------------------------------------
# enforce_brackets
# ---------------------------------------------------------------------------


def test_brackets_system_block_preserved():
    text = "He saw the panel.\n\n**【Realm: Foundation Establishment】**\n\nHe grinned."
    out, count = enforce_brackets(text)
    assert out == text
    assert count == 0


def test_brackets_short_status_announcement_preserved():
    text = "The system chimed.\n\n**【New Skill Acquired: Sword Qi】**"
    out, count = enforce_brackets(text)
    assert out == text
    assert count == 0


def test_brackets_narrative_dialogue_stripped():
    # Inner contains a CN sentence-final char → narrative trigger.
    text = "He said 【这是我的剑！】 and turned away."
    out, count = enforce_brackets(text)
    assert "【" not in out
    assert "】" not in out
    assert "这是我的剑！" in out
    assert count == 1


def test_brackets_long_inner_text_stripped():
    inner = "x" * 100
    text = f"prefix 【{inner}】 suffix"
    out, count = enforce_brackets(text)
    assert "【" not in out
    assert inner in out
    assert count == 1


def test_brackets_english_quote_inside_treated_narrative():
    # An English quote inside brackets is a strong narrative signal.
    text = 'before 【He said "no" loudly】 after'
    out, count = enforce_brackets(text)
    assert "【" not in out
    assert "】" not in out
    assert "before He said" in out
    assert count == 1


def test_brackets_mixed_system_and_narrative():
    text = (
        "**【Quest Complete: Save the village】**\n\n"
        "He muttered 【真是麻烦啊。】 to himself."
    )
    out, count = enforce_brackets(text)
    # System block kept; narrative span stripped.
    assert "【Quest Complete: Save the village】" in out
    assert "【真是麻烦啊" not in out
    assert "真是麻烦啊" in out
    assert count == 1


def test_brackets_inline_english_span_stripped():
    # A bracketed span appearing mid-prose (not its own paragraph) is inline
    # emphasis, not a UI line. Strip even though inner has no CN punctuation
    # and isn't long.
    text = "He stepped into the **【Hall of Yama】** and surveyed the spirits."
    out, count = enforce_brackets(text)
    assert "【" not in out
    assert "】" not in out
    # Adjacent bold wrappers should be stripped with the brackets.
    assert "**" not in out
    assert "Hall of Yama" in out
    assert count == 1


def test_brackets_standalone_glossary_callout_stripped():
    # `**【Hall of Yama】**!` as a standalone paragraph — the trailing `!`
    # is paragraph prose outside the span, so the inline check fires.
    glossary = [_entry("Hall of Yama")]
    text = "He prepared the secret art.\n\n**【Hall of Yama】**!\n\nThe gates opened."
    out, count = enforce_brackets(text, glossary=glossary)
    assert "【Hall of Yama】" not in out
    assert "**Hall of Yama**" not in out
    assert "Hall of Yama!" in out
    assert count == 1


def test_brackets_glossary_term_alone_in_paragraph_stripped():
    # Bracketed glossary term entirely alone in its paragraph — no other
    # prose. The glossary-term check (not the inline check) fires.
    glossary = [_entry("Hall of Yama")]
    text = "Setup line.\n\n**【Hall of Yama】**\n\nFollowup line."
    out, count = enforce_brackets(text, glossary=glossary)
    assert "【Hall of Yama】" not in out
    assert "**Hall of Yama**" not in out
    assert "\n\nHall of Yama\n\n" in out
    assert count == 1


def test_brackets_unknown_short_callout_kept_when_no_glossary():
    # Without glossary context, a standalone `**【Something】**` paragraph
    # with no other prose looks like a system block. Keep it.
    text = "Setup.\n\n**【Hall of Yama】**\n\nFollowup."
    out, count = enforce_brackets(text)
    assert "【Hall of Yama】" in out
    assert count == 0


def test_brackets_system_block_with_glossary_overlap_kept():
    # If a system-style label happens to overlap with a glossary term it
    # still strips — but a genuine label like "Realm: Foundation Establishment"
    # has structure (colon, etc.) that means its inner text won't match a
    # glossary term verbatim. This test pins the legitimate-system path.
    glossary = [_entry("Hall of Yama"), _entry("Foundation Establishment")]
    text = "He saw the panel.\n\n**【Realm: Foundation Establishment】**\n\nHe grinned."
    out, count = enforce_brackets(text, glossary=glossary)
    assert "【Realm: Foundation Establishment】" in out
    assert count == 0


def test_brackets_long_system_pane_preserved():
    # Regression: a standalone `**【Label: long description】**` skill / talent
    # pane is a system block, not mis-bracketed narrative. The inner text is
    # ~150 chars and contains sentence punctuation, but neither is evidence of
    # narrative for a bold, colon-structured standalone pane. The pane must
    # survive verbatim. (Previously the >80-char cap deleted it.)
    glossary = [_entry("Marionette", term_zh="提线木偶")]
    pane = (
        "**【Marionette: Seize another person's fortune and bear their karma. "
        "By doing so, you may hide beneath their appearance and control them "
        "like a puppet. No one can divine your true origins.】**"
    )
    text = f"He read the prompt.\n\n{pane}\n\nIf it said no one could, then no one could."
    out, count = enforce_brackets(text, glossary=glossary)
    assert pane in out
    assert count == 0


def test_brackets_long_pane_preserved_without_glossary():
    # Same protection holds with no glossary context: bold + colon = pane.
    pane = "**【Status: " + "a long description that runs well past eighty characters in total" + "】**"
    text = f"Setup.\n\n{pane}\n\nFollowup."
    out, count = enforce_brackets(text)
    assert pane in out
    assert count == 0


def test_brackets_unstructured_standalone_narrative_still_stripped():
    # A standalone span with NO bold wrapper and NO colon, carrying CN
    # sentence punctuation, is still treated as mis-bracketed narrative.
    text = "Setup.\n\n【这是一段被错误括起来的叙述。】\n\nFollowup."
    out, count = enforce_brackets(text)
    assert "【" not in out
    assert "这是一段被错误括起来的叙述。" in out
    assert count == 1


def test_brackets_idempotent_on_clean_text():
    text = "He said this is my sword and turned away."
    out, count = enforce_brackets(text)
    assert out == text
    assert count == 0


# ---------------------------------------------------------------------------
# enforce_stem_branch_casing
# ---------------------------------------------------------------------------


def test_stem_branch_lowercase_phase_fixed() -> None:
    for bad, good in [
        ("Si-fire", "Si-Fire"),
        ("Chen-earth", "Chen-Earth"),
        ("Geng-metal", "Geng-Metal"),
        ("Wu-fire", "Wu-Fire"),  # Branch Wu, not Stem Wu
        ("Jia-wood", "Jia-Wood"),
        ("Gui-water", "Gui-Water"),
    ]:
        out, n = enforce_stem_branch_casing(f"the {bad} essence")
        assert good in out, bad
        assert n == 1, bad


def test_stem_branch_already_correct_unchanged() -> None:
    text = "the Geng-Metal spirit-immortal touched Si-Fire and Chen-Earth"
    out, n = enforce_stem_branch_casing(text)
    assert out == text
    assert n == 0


def test_stem_branch_idempotent() -> None:
    text = "Si-fire and Chen-earth and Geng-metal"
    out1, n1 = enforce_stem_branch_casing(text)
    out2, n2 = enforce_stem_branch_casing(out1)
    assert n1 == 3
    assert n2 == 0
    assert out1 == out2


def test_stem_branch_non_stem_hyphens_untouched() -> None:
    # 'fire-water' is not a Stem-Phase compound — must not match.
    text = "a fire-water duality and a sword-Fire technique"
    out, n = enforce_stem_branch_casing(text)
    assert out == text
    assert n == 0


def test_stem_branch_only_matches_at_word_boundary() -> None:
    # 'Sifire' (no hyphen) is not a Stem-Phase compound.
    text = "the Sifire word and meta-fire compound"
    out, n = enforce_stem_branch_casing(text)
    assert out == text
    assert n == 0


# ---------------------------------------------------------------------------
# enforce_locked_term_casing
# ---------------------------------------------------------------------------


def _atomic_entry(term_en: str, category: str = "other", notes: str | None = None) -> GlossaryEntry:
    return GlossaryEntry(
        id=99, novel_id=1, term_zh="X", term_en=term_en, category=category,
        notes=notes, auto_detected=False, locked=True,
    )


def test_locked_term_casing_atomic_proper_noun_normalized() -> None:
    g = [_atomic_entry("Fruition Attainment", category="other")]
    text = "He broke through to fruition attainment after long retreat."
    out, n = enforce_locked_term_casing(text, g)
    assert "Fruition Attainment" in out
    assert "fruition attainment" not in out
    assert n == 1


def test_locked_term_casing_hyphenated_other_normalized() -> None:
    g = [_atomic_entry("Chen-Earth", category="other")]
    text = "The Chen-earth force gathered, and Chen-EARTH descended."
    out, n = enforce_locked_term_casing(text, g)
    # Both lowercased and uppercased variants normalize to canonical.
    assert out.count("Chen-Earth") == 2
    assert "Chen-earth" not in out
    assert "Chen-EARTH" not in out
    assert n == 2


def test_locked_term_casing_inside_italics_preserves_markers() -> None:
    # Written-work title legitimately italicized. Casing fixed, *…* preserved.
    g = [_atomic_entry("Supreme Brightness of Golden Lamp and Jade Light", category="technique")]
    text = "He recited *supreme brightness of golden lamp and jade light* aloud."
    out, n = enforce_locked_term_casing(text, g)
    assert "*Supreme Brightness of Golden Lamp and Jade Light*" in out
    assert n == 1


def test_locked_term_casing_skips_lowercase_note_rows() -> None:
    # A locked row whose notes say `lowercase` is soft — never force-cased.
    g = [_atomic_entry("Spiritual Power", category="other", notes="lowercase")]
    text = "his spiritual power surged within him"
    out, n = enforce_locked_term_casing(text, g)
    assert out == text
    assert n == 0


def test_locked_term_casing_cross_entry_soft_dedup() -> None:
    # Two rows share the same `term_en`; one has `lowercase` notes. The whole
    # English form must be treated as soft (Divine Sense case).
    g = [
        _atomic_entry("Divine Sense", category="other", notes="frequent | lowercase"),
        _atomic_entry("Divine Sense", category="other", notes=None),
    ]
    text = "his divine sense swept the room"
    out, n = enforce_locked_term_casing(text, g)
    assert out == text
    assert n == 0


def test_locked_term_casing_skips_slash_alternative_rows() -> None:
    g = [_atomic_entry("Karma / Karmic Threads", category="other")]
    text = "he traced the karma and karmic threads back to their source"
    out, n = enforce_locked_term_casing(text, g)
    assert out == text
    assert n == 0


def test_locked_term_casing_skips_parenthetical_rows() -> None:
    g = [_atomic_entry("Demonic Path (philosophy/affiliation)", category="other")]
    text = "his demonic path leanings became plain"
    out, n = enforce_locked_term_casing(text, g)
    assert out == text
    assert n == 0


def test_locked_term_casing_skips_generic_rank_rows() -> None:
    # Locked entries that are entirely a generic rank descriptor must not be
    # force-cased — the translator deliberately lowercases them mid-sentence.
    g = [_atomic_entry("Second-Rank", category="other")]
    text = "a second-rank cultivator passed by"
    out, n = enforce_locked_term_casing(text, g)
    assert out == text
    assert n == 0


def test_locked_term_casing_idempotent() -> None:
    g = [_atomic_entry("Fruition Attainment", category="other")]
    text = "He reached fruition attainment swiftly."
    once, n1 = enforce_locked_term_casing(text, g)
    twice, n2 = enforce_locked_term_casing(once, g)
    assert n1 == 1
    assert n2 == 0
    assert once == twice


def test_locked_term_casing_leaves_sea_roar_carrier_alone() -> None:
    # Sea's Roar's gaze is a carrier-syntax issue (B1), not a casing one — the
    # locked-term casing post-fix must not rewrite it.
    g = [_atomic_entry("True Person Sea's Roar", category="character")]
    text = "True Person Sea's Roar's gaze swept the chamber"
    out, n = enforce_locked_term_casing(text, g)
    assert "True Person Sea's Roar" in out
    # Carrier collision is preserved verbatim (detector catches it instead).
    assert "Sea's Roar's gaze" in out


def test_locked_term_casing_skips_code_fences() -> None:
    g = [_atomic_entry("Chen-Earth", category="other")]
    text = "```python\nx = 'Chen-earth'\n```\nThe Chen-earth field"
    out, n = enforce_locked_term_casing(text, g)
    # Only the outside-code-fence occurrence is fixed.
    assert "```python\nx = 'Chen-earth'\n```" in out
    assert "The Chen-Earth field" in out
    assert n == 1


def test_locked_term_casing_skips_system_panes() -> None:
    g = [_atomic_entry("Chen-Earth", category="other")]
    text = "**【Realm: Chen-earth】**\n\nThe Chen-earth pulse arrived."
    out, n = enforce_locked_term_casing(text, g)
    # System pane preserved verbatim, prose occurrence normalized.
    assert "**【Realm: Chen-earth】**" in out
    assert "The Chen-Earth pulse" in out
    assert n == 1


# ---------------------------------------------------------------------------
# detect_double_possessive
# ---------------------------------------------------------------------------


def test_double_possessive_caught_straight_apostrophe() -> None:
    issues = detect_double_possessive("True Person Sea's Roar's confidence firmed.")
    assert len(issues) == 1
    assert "Sea's Roar's" in issues[0]


def test_double_possessive_caught_curly_apostrophe() -> None:
    issues = detect_double_possessive("Sea’s Roar’s gaze pierced.")
    assert len(issues) == 1
    assert "Sea’s Roar’s" in issues[0]


def test_double_possessive_single_possessive_clean() -> None:
    assert detect_double_possessive("Sea's Roar firmed his resolve.") == []


def test_double_possessive_simple_chain_flagged() -> None:
    # `John's brother's car` is a legitimate possessive chain in English, but
    # in cultivation prose this pattern is almost always a glossary-name
    # collision. The detector errs on the side of flagging.
    issues = detect_double_possessive("John's brother's car was parked outside.")
    assert len(issues) == 1


def test_double_possessive_contraction_not_flagged() -> None:
    # "It's gone" is a contraction (it is), not a possessive.
    assert detect_double_possessive("Sea's Roar's gone for the day.") == []


def test_double_possessive_dedup() -> None:
    text = "Sea's Roar's hand. Sea's Roar's foot. Sea's Roar's voice."
    issues = detect_double_possessive(text)
    assert len(issues) == 1  # single issue string
    # Verify span appears once in the message (deduped).
    assert issues[0].count("Sea's Roar's") <= 3  # at most one mention + context


def test_double_possessive_empty() -> None:
    assert detect_double_possessive("") == []


# ---------------------------------------------------------------------------
# detect_locked_idiom_grammar
# ---------------------------------------------------------------------------


def test_locked_idiom_grammar_flags_prepositional_context() -> None:
    issues = detect_locked_idiom_grammar(
        "How was that different from you court death?",
        [_idiom_entry("you court death")],
    )
    assert len(issues) == 1
    assert "courting death" in issues[0]


def test_locked_idiom_grammar_allows_standalone_insult() -> None:
    assert detect_locked_idiom_grammar(
        "You court death!",
        [_idiom_entry("you court death")],
    ) == []


def test_locked_idiom_grammar_ignores_unlocked_or_other_idioms() -> None:
    assert detect_locked_idiom_grammar(
        "How was that different from you court death?",
        [_idiom_entry("you court death", locked=False)],
    ) == []


# ---------------------------------------------------------------------------
# detect_mid_sentence_paragraph_break
# ---------------------------------------------------------------------------


def test_mid_sentence_break_comma_before_break_flagged() -> None:
    text = (
        "As a Geng-Metal spirit-immortal in this life, "
        "if he could nourish himself within it,\n\n"
        "the benefits would be enormous!"
    )
    issues = detect_mid_sentence_paragraph_break(text)
    assert len(issues) == 1
    assert "Mid-sentence paragraph break" in issues[0]


def test_mid_sentence_break_lowercase_letter_before_break_flagged() -> None:
    text = "the elder paused\n\nand then continued."
    issues = detect_mid_sentence_paragraph_break(text)
    assert len(issues) == 1


def test_mid_sentence_break_terminator_clean() -> None:
    text = "The benefits would be enormous!\n\nLü Yang nodded slowly."
    assert detect_mid_sentence_paragraph_break(text) == []


def test_mid_sentence_break_dialogue_opener_suppresses() -> None:
    # "He said:" + new paragraph opening with a quote is legitimate.
    text = "He said:\n\n\"I refuse.\""
    assert detect_mid_sentence_paragraph_break(text) == []


def test_mid_sentence_break_em_dash_dialogue_opener_suppresses() -> None:
    # A new paragraph starting with an em-dash (cut-off speech opening) is
    # legitimate even when the prior paragraph ends in a colon.
    text = "He whispered:\n\n— I cannot."
    assert detect_mid_sentence_paragraph_break(text) == []


def test_mid_sentence_break_italic_opener_suppresses() -> None:
    # A new paragraph opening with italics (inner thought standalone) is fine.
    text = "He stared blankly.\n\n*This cannot be.*"
    assert detect_mid_sentence_paragraph_break(text) == []


def test_mid_sentence_break_single_paragraph_clean() -> None:
    assert detect_mid_sentence_paragraph_break("Just one paragraph here.") == []


def test_mid_sentence_break_uppercase_letter_clean() -> None:
    # Headings / fragments ending uppercase don't trigger.
    text = "CHAPTER ONE\n\nThe sun rose."
    assert detect_mid_sentence_paragraph_break(text) == []


def test_mid_sentence_break_empty() -> None:
    assert detect_mid_sentence_paragraph_break("") == []


# ---------------------------------------------------------------------------
# Worker-level: the translation queue worker cleans translator output.
# Exercises the real `_translate_chapter_in_db` worker end-to-end.
# ---------------------------------------------------------------------------


def _reset_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


async def _noop_run(novel_id, chapter_id):
    """Stub for queue._run_translate so the route's spawned background task
    opens no DB connection — these tests drive `_translate_chapter_in_db`
    directly instead, deterministically and without an orphan handle."""
    return


# Translator output deliberately carries both AI tells: a mid-clause em-dash
# (NOT cut-off speech) and a narrative 【…】 span (CN sentence-final char
# inside, so it classifies as narrative).
_DIRTY_TRANSLATION = (
    "He raised his hand — the qi gathered around his fingertips.\n\n"
    "He muttered 【这是我的剑！】 and turned away."
)


async def _fake_translate(
    original, title_zh, glossary, previous_context=None, style_edits=None,
    use_cache=True, style_note=None, provider=None, **_unused,
):
    from backend.models import TranslationResult

    return TranslationResult(
        title_en="A Quiet Morning",
        translated_text=_DIRTY_TRANSLATION,
        new_terms=[],
    )


def _seed_one_chapter_novel(title: str) -> tuple[int, int]:
    """Create a novel with one pending chapter via the bulk route; return
    (novel_id, chapter_id)."""
    client = TestClient(app)
    r = client.post(
        "/api/translate/bulk",
        data={"title": title},
        files=[("files", ("ch1.txt", BytesIO(("原文" * 100).encode("utf-8")), "text/plain"))],
    )
    assert r.status_code == 200, r.text
    novel_id = r.json()["novel_id"]
    conn = sqlite3.connect(DB_PATH)
    chapter_id = conn.execute(
        "SELECT id FROM chapters WHERE novel_id = ?", (novel_id,)
    ).fetchone()[0]
    conn.close()
    return novel_id, chapter_id


def _seed_glossary_entry(
    novel_id: int,
    zh: str,
    en: str,
    *,
    category: str = "character",
    locked: bool = True,
) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO glossary_entries "
            "(novel_id, term_zh, term_en, category, auto_detected, locked) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (novel_id, zh, en, category, 0 if locked else 1, 1 if locked else 0),
        )
        conn.commit()
    finally:
        conn.close()


def _chapter_row(chapter_id: int) -> sqlite3.Row:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT status, translated_text FROM chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()
    finally:
        conn.close()


def _assert_cleaned(body: str) -> None:
    assert "—" not in body
    assert "【" not in body and "】" not in body
    assert "这是我的剑！" in body
    assert "hand, the qi" in body


def test_translation_worker_cleans_em_dash_and_brackets(monkeypatch) -> None:
    _reset_db()
    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake_translate)
    novel_id, chapter_id = _seed_one_chapter_novel("Guardrail Novel")

    # Flag the chapter for translation, then run the worker directly (no
    # background-task timing race).
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapters SET translate_queued = 1 WHERE id = ?", (chapter_id,)
    )
    conn.commit()
    conn.close()

    async def _run() -> None:
        async with open_conn() as worker_conn:
            await queue._translate_chapter_in_db(worker_conn, novel_id, chapter_id)

    asyncio.run(_run())

    row = _chapter_row(chapter_id)
    assert row["status"] == "done"
    _assert_cleaned(row["translated_text"])


def test_translation_worker_does_not_silently_recast_double_possessive(
    monkeypatch,
) -> None:
    _reset_db()

    async def _double_possessive_translate(
        original, title_zh, glossary, previous_context=None, style_edits=None,
        use_cache=True, style_note=None, provider=None, **_unused,
    ):
        from backend.models import TranslationResult

        return TranslationResult(
            title_en="A Quiet Morning",
            translated_text=(
                "True Person Sea's Roar's gaze swept the chamber. "
                "The disciples lowered their heads in silence."
            ),
            new_terms=[],
        )

    monkeypatch.setattr(
        "backend.services.queue.translate_chapter",
        _double_possessive_translate,
    )
    novel_id, chapter_id = _seed_one_chapter_novel("Double Possessive Novel")
    _seed_glossary_entry(novel_id, "啸海真人", "True Person Sea's Roar")

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapters SET translate_queued = 1 WHERE id = ?", (chapter_id,)
    )
    conn.commit()
    conn.close()

    async def _run() -> None:
        async with open_conn() as worker_conn:
            await queue._translate_chapter_in_db(worker_conn, novel_id, chapter_id)

    asyncio.run(_run())

    row = _chapter_row(chapter_id)
    assert row["status"] == "done"
    assert "True Person Sea's Roar's gaze" in row["translated_text"]
    assert "the gaze of True Person Sea's Roar" not in row["translated_text"]


def test_translation_worker_does_not_silently_join_mid_sentence_break(
    monkeypatch,
) -> None:
    _reset_db()

    async def _mid_break_translate(
        original, title_zh, glossary, previous_context=None, style_edits=None,
        use_cache=True, style_note=None, provider=None, **_unused,
    ):
        from backend.models import TranslationResult

        return TranslationResult(
            title_en="A Quiet Morning",
            translated_text="He paused,\n\nand then continued toward the hall.",
            new_terms=[],
        )

    monkeypatch.setattr(
        "backend.services.queue.translate_chapter",
        _mid_break_translate,
    )
    novel_id, chapter_id = _seed_one_chapter_novel("Mid Break Novel")

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapters SET translate_queued = 1 WHERE id = ?", (chapter_id,)
    )
    conn.commit()
    conn.close()

    async def _run() -> None:
        async with open_conn() as worker_conn:
            await queue._translate_chapter_in_db(worker_conn, novel_id, chapter_id)

    asyncio.run(_run())

    row = _chapter_row(chapter_id)
    assert row["status"] == "done"
    assert "paused,\n\nand then" in row["translated_text"]


def test_retranslate_route_runs_guardrails(monkeypatch) -> None:
    """The retranslate route resets the row and routes through the same
    `_translate_chapter_in_db` worker — so a retranslated chapter is cleaned
    too. Drives the real route handler, then runs the worker the route
    would have spawned."""
    from backend.routes.chapters import retranslate_chapter

    _reset_db()
    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake_translate)
    # The route's queue_translation spawns _run_translate; stub it so we run
    # the worker deterministically below instead.
    monkeypatch.setattr("backend.services.queue._run_translate", _noop_run)
    novel_id, chapter_id = _seed_one_chapter_novel("Retranslate Novel")

    async def _run() -> None:
        # First translation: chapter goes pending -> done.
        async with open_conn() as conn:
            await conn.execute(
                "UPDATE chapters SET translate_queued = 1 WHERE id = ?",
                (chapter_id,),
            )
            await conn.commit()
        async with open_conn() as conn:
            await queue._translate_chapter_in_db(conn, novel_id, chapter_id)

        # Now exercise the real retranslate route handler. It resets the row
        # to 'pending' and queues a worker task.
        async with open_conn() as conn:
            resp = await retranslate_chapter(novel_id, 1, conn=conn)
        assert resp == {"status": "queued"}

        async with open_conn() as conn:
            row = await (
                await conn.execute(
                    "SELECT status, translate_queued FROM chapters WHERE id = ?",
                    (chapter_id,),
                )
            ).fetchone()
            assert row["status"] == "pending"
            assert row["translate_queued"] == 1
        async with open_conn() as conn:
            await queue._translate_chapter_in_db(conn, novel_id, chapter_id)

    asyncio.run(_run())

    row = _chapter_row(chapter_id)
    assert row["status"] == "done"
    _assert_cleaned(row["translated_text"])


def test_retranslate_sets_force_retranslate_and_bypasses_cache(monkeypatch) -> None:
    """The retranslate route flags `force_retranslate`; the worker reads it,
    passes `use_cache=False` to the translator (so an explicit Retranslate
    re-runs the LLM rather than returning the cached result), and clears the
    flag as it claims the row."""
    from backend.routes.chapters import retranslate_chapter

    _reset_db()
    use_cache_calls: list[bool] = []

    async def _recording_translate(
        original, title_zh, glossary, previous_context=None,
        style_edits=None, use_cache=True, style_note=None,
        provider=None, **_unused,
    ):
        from backend.models import TranslationResult

        use_cache_calls.append(use_cache)
        return TranslationResult(
            title_en="A Quiet Morning",
            translated_text=_DIRTY_TRANSLATION,
            new_terms=[],
        )

    monkeypatch.setattr(
        "backend.services.queue.translate_chapter", _recording_translate
    )
    monkeypatch.setattr("backend.services.queue._run_translate", _noop_run)
    novel_id, chapter_id = _seed_one_chapter_novel("Force Retranslate Novel")

    async def _force_retranslate(chapter_id_local: int) -> int:
        async with open_conn() as conn:
            cur = await conn.execute(
                "SELECT force_retranslate FROM chapters WHERE id = ?",
                (chapter_id_local,),
            )
            return (await cur.fetchone())["force_retranslate"]

    async def _run() -> None:
        # First translation: a normal queued translate — cache allowed.
        async with open_conn() as conn:
            await conn.execute(
                "UPDATE chapters SET translate_queued = 1 WHERE id = ?",
                (chapter_id,),
            )
            await conn.commit()
        async with open_conn() as conn:
            await queue._translate_chapter_in_db(conn, novel_id, chapter_id)

        # Retranslate route flags the row for a cache-bypassing redo.
        async with open_conn() as conn:
            await retranslate_chapter(novel_id, 1, conn=conn)
        assert await _force_retranslate(chapter_id) == 1

        # The worker run for the retranslate consumes the flag.
        async with open_conn() as conn:
            await queue._translate_chapter_in_db(conn, novel_id, chapter_id)
        assert await _force_retranslate(chapter_id) == 0

    asyncio.run(_run())

    # First call cached (use_cache=True); the retranslate bypassed it (False).
    assert use_cache_calls == [True, False]
    row = _chapter_row(chapter_id)
    assert row["status"] == "done"

    asyncio.run(_run())

    row = _chapter_row(chapter_id)
    assert row["status"] == "done"
    _assert_cleaned(row["translated_text"])


# ---------------------------------------------------------------------------
# Glossary-anchored predicate-loss detection (detect_glossary_predicate_loss)
# ---------------------------------------------------------------------------


def _predicate_locked(
    zh: str, en: str, category: str = "place"
) -> GlossaryEntry:
    return GlossaryEntry(
        id=0, novel_id=1, term_zh=zh, term_en=en, category=category,
        notes=None, auto_detected=False, locked=True,
    )


def test_predicate_loss_flags_title_drop_of_encounter() -> None:
    # The motivating Chapter-300 failure: locked term preserved + adverb
    # preserved, but the predicate `再遇` / encounter is gone.
    g = [_predicate_locked("昂霄", "Soaring Firmament")]
    issues = detect_glossary_predicate_loss(
        "惊悚一幕，再遇昂霄！",
        "A Chilling Scene, Soaring Firmament Once Again!",
        g,
        source_label="chapter title",
    )
    assert len(issues) == 1
    assert "Soaring Firmament" in issues[0]
    assert "encounter" in issues[0].lower() or "meet" in issues[0].lower()


def test_predicate_loss_pass_title_with_encounter() -> None:
    # Same Chinese title, faithful English — the predicate IS rendered.
    g = [_predicate_locked("昂霄", "Soaring Firmament")]
    assert detect_glossary_predicate_loss(
        "惊悚一幕，再遇昂霄！",
        "A Chilling Scene, Encountering Soaring Firmament Again!",
        g,
        source_label="chapter title",
    ) == []


def test_predicate_loss_flags_body_drop_of_strike() -> None:
    # Body case: 暗中对X出手 → "X from the shadows" drops 出手 entirely.
    g = [_predicate_locked("鸿运道人", "Daoist Hong Yun", "character")]
    issues = detect_glossary_predicate_loss(
        "他暗中对鸿运道人出手。后来形势骤变。",
        "Daoist Hong Yun from the shadows. The situation then turned sharply.",
        g,
        source_label="chapter body",
    )
    assert len(issues) == 1
    assert "Daoist Hong Yun" in issues[0]


def test_predicate_loss_pass_body_with_made_a_move() -> None:
    g = [_predicate_locked("鸿运道人", "Daoist Hong Yun", "character")]
    assert detect_glossary_predicate_loss(
        "他暗中对鸿运道人出手。",
        "He made a move against Daoist Hong Yun from the shadows.",
        g,
        source_label="chapter body",
    ) == []


def test_predicate_loss_skips_when_term_absent_from_english() -> None:
    # When the glossary term itself never lands in the English,
    # missing_translator_terms owns the flag — predicate-loss must NOT
    # double-report.
    g = [_predicate_locked("昂霄", "Soaring Firmament")]
    assert detect_glossary_predicate_loss(
        "再遇昂霄！",
        "And then it happened once more!",
        g,
        source_label="chapter title",
    ) == []


def test_predicate_loss_ignores_unlocked_entries() -> None:
    # Auto-extracted unlocked entries are too noisy for pair-checking.
    g = [GlossaryEntry(
        id=0, novel_id=1, term_zh="昂霄", term_en="Soaring Firmament",
        category="place", notes=None, auto_detected=True, locked=False,
    )]
    assert detect_glossary_predicate_loss(
        "再遇昂霄！", "Soaring Firmament Once Again!", g,
    ) == []


def test_predicate_loss_flags_dropped_find() -> None:
    g = [_predicate_locked("洞天碎片", "Grotto-Heaven fragment", "item")]
    issues = detect_glossary_predicate_loss(
        "他发现这座洞天碎片。",
        "This Grotto-Heaven fragment appeared before him.",
        g,
        source_label="chapter body",
    )
    assert len(issues) == 1
    assert "Grotto-Heaven fragment" in issues[0]


def test_predicate_loss_body_accepts_adjacent_segment_predicate() -> None:
    # Body mode: the predicate may land in a sentence adjacent to the
    # sentence carrying the term. "He met him again. Soaring Firmament
    # stood there..." — the term is in segment 1, "met" is in segment 0,
    # which counts under the body-mode adjacency rule.
    g = [_predicate_locked("昂霄", "Soaring Firmament")]
    assert detect_glossary_predicate_loss(
        "他再遇昂霄。两人对视良久。",
        "He met him again. Soaring Firmament stood there, unmoving.",
        g,
        source_label="chapter body",
    ) == []


def test_predicate_loss_title_does_not_relax_to_adjacent() -> None:
    # Same shape as the previous test, but in title mode the adjacency rule
    # is OFF — the predicate must sit in the term's own segment. Titles are
    # short; we hold them to the strict standard.
    g = [_predicate_locked("昂霄", "Soaring Firmament")]
    issues = detect_glossary_predicate_loss(
        "再遇昂霄！",
        "He met him again. Soaring Firmament once more!",
        g,
        source_label="chapter title",
    )
    assert len(issues) == 1


def test_predicate_loss_proximity_gate_skips_far_pairs() -> None:
    # Multi-clause Chinese segment with two locked terms and two verbs.
    # The CN comma `，` is NOT a segment boundary, so both `发现` and the
    # later `攻击`-clause sit in one segment. The proximity gate plus the
    # adjacent-segment relaxation keep this from spuriously flagging:
    # `发现` is paired with 洞天碎片 (close); the wrong pairing 发现 → 鸿运道人
    # is either rejected by proximity or accepted by adjacency because the
    # English carries "found" in the same English segment.
    g = [
        _predicate_locked("洞天碎片", "Grotto-Heaven fragment", "item"),
        _predicate_locked("鸿运道人", "Daoist Hong Yun", "character"),
    ]
    src = "他发现了这座洞天碎片，随后又转身朝鸿运道人发起了猛烈攻击"
    en = (
        "He found this Grotto-Heaven fragment, then wheeled on Daoist "
        "Hong Yun and pressed his assault."
    )
    assert detect_glossary_predicate_loss(
        src, en, g, source_label="chapter body",
    ) == []


def test_predicate_loss_handles_apostrophe_in_term() -> None:
    # `\b` boundaries would mis-handle an apostrophe at the edge of a
    # glossary name. Custom non-alphanumeric boundary lookarounds must
    # match `True Person Sea's Roar` cleanly.
    g = [_predicate_locked("啸海真人", "True Person Sea's Roar", "character")]
    assert detect_glossary_predicate_loss(
        "他终于遇见啸海真人。",
        "He finally met True Person Sea's Roar.",
        g,
        source_label="chapter body",
    ) == []


def test_predicate_loss_does_not_cross_chinese_clause_break() -> None:
    # Chapter 300 regression: the source sentence says Chen-Earth was
    # discovered, then later mentions the Great Tribulation after a Chinese
    # comma. The detector must not attach 发现 / discovered to the unrelated
    # Great Tribulation glossary term.
    g = [_predicate_locked("千年大劫", "Great Tribulation", "other")]
    assert detect_glossary_predicate_loss(
        "近日发现【辰土】似有变动，奈何正值千年大劫，天机蒙蔽。",
        (
            "In recent days, I discovered that Chen-Earth seems to have changed. "
            "Unfortunately, this is during the Great Tribulation."
        ),
        g,
        source_label="chapter body",
    ) == []


def test_predicate_loss_matches_simple_english_plural_term() -> None:
    # Chapter 300 regression: glossary term is singular "jade slip", but
    # natural prose says "jade slips were not left behind". The detector
    # should find that local segment and accept the predicate.
    g = [_predicate_locked("玉简", "jade slip", "item")]
    assert detect_glossary_predicate_loss(
        "这些玉简不是鸿运道人留下的。",
        "These jade slips were not left behind by Daoist Hong Yun.",
        g,
        source_label="chapter body",
    ) == []


def test_predicate_loss_returns_empty_for_missing_glossary() -> None:
    # Defensive: None / empty glossary is a fast no-op.
    assert detect_glossary_predicate_loss("再遇昂霄！", "Whatever.", None) == []
    assert detect_glossary_predicate_loss("再遇昂霄！", "Whatever.", []) == []


def test_predicate_loss_caps_at_five_issues() -> None:
    # Eight distinct locked terms, all with `再遇` dropped in the English →
    # detector must cap at 5 issues, not emit one per term.
    cn_chars = ("甲", "乙", "丙", "丁", "戊", "己", "庚", "辛")
    en_names = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta"]
    src = "。".join(f"再遇{c}号洞天" for c in cn_chars)
    en = ". ".join(f"Cave-Heaven {n} once more" for n in en_names) + "."
    glossary = [
        _predicate_locked(f"{c}号洞天", f"Cave-Heaven {n}", "place")
        for c, n in zip(cn_chars, en_names)
    ]
    issues = detect_glossary_predicate_loss(
        src, en, glossary, source_label="chapter body",
    )
    assert len(issues) <= 5
    assert len(issues) >= 1
