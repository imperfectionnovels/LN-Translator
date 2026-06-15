"""Routing-logic tests for the learn-from-edits ingest tool. The DB/async path
is exercised by hand against a real chapter; here we pin the pure classifier so
a delta lands in the right bucket (glossary-casing / mechanical / style) and the
paragraph aligner pairs changed paragraphs correctly."""
from __future__ import annotations

from backend.models import GlossaryEntry
from backend.scripts.ingest_edited_chapter import (
    _align_pairs,
    _apply_fixups,
    _classify,
    _split_paras,
)


def _g(term_en, *, term_zh="X", notes="lowercase", locked=True, category="other"):
    return GlossaryEntry(
        id=1, novel_id=1, term_zh=term_zh, term_en=term_en, category=category,
        notes=notes, auto_detected=False, locked=locked,
    )


def _ctx(glossary):
    term_en_list = sorted(
        {g.term_en.strip() for g in glossary if g.term_en.strip()}, key=len, reverse=True
    )
    term_lower = {t.lower() for t in term_en_list} | {g.term_zh.lower() for g in glossary}
    by_en = {g.term_en.strip().lower(): g for g in glossary}
    return term_lower, term_en_list, by_en


def test_align_pairs_only_changed_paragraphs():
    draft = ["Alpha stands.", "Beta runs fast.", "Gamma waits."]
    edited = ["Alpha stands.", "Beta sprints onward.", "Gamma waits."]
    pairs, ins, dele = _align_pairs(draft, edited)
    assert pairs == [("Beta runs fast.", "Beta sprints onward.")]
    assert ins == 0 and dele == 0


def test_classify_casing_only_glossary_term():
    g = [_g("reincarnation", term_zh="轮回")]
    term_lower, term_en_list, by_en = _ctx(g)
    route, detail = _classify(
        "He kept the Reincarnation here.",
        "He kept the reincarnation here.",
        term_lower, term_en_list, by_en, g,
    )
    assert route == "glossary-casing"
    assert any("Reincarnation -> reincarnation" in t for t in detail["terms"])
    # term_en is already lowercased, so it is NOT a half-applied hatch.
    assert detail["half_applied"] == []


def test_classify_casing_only_flags_half_applied_hatch():
    # A Title-Cased term_en with a lowercase note is the half-applied hatch.
    g = [_g("Backlash", term_zh="反噬", notes="lowercase")]
    term_lower, term_en_list, by_en = _ctx(g)
    route, detail = _classify(
        "the Backlash struck.", "the backlash struck.",
        term_lower, term_en_list, by_en, g,
    )
    assert route == "glossary-casing"
    assert detail["half_applied"] == ["Backlash"]


def test_classify_mechanical_spaced_hyphen():
    route, detail = _classify(
        "He raised his hand - the qi gathered.",
        "He raised his hand, the qi gathered.",
        set(), [], {}, [],
    )
    assert route == "mechanical"
    assert detail["complete"] is True
    assert "enforce_spaced_hyphen_dash" in detail["fixups"]


def test_classify_style_rewrite():
    route, _ = _classify(
        "This trick worked unfailingly as bait.",
        "This trick is truly unstoppable for fishing.",
        set(), [], {}, [],
    )
    assert route == "style"


def test_apply_fixups_reports_firing():
    out, fired = _apply_fixups("a True Person - ruthless and cold.", [])
    assert " - " not in out
    assert "enforce_spaced_hyphen_dash" in fired


def test_split_paras_double_newline():
    assert _split_paras("A.\n\nB.\n\n\nC.") == ["A.", "B.", "C."]
