"""Tests for filter_glossary_for_chapter.

This filter is load-bearing for the per-chapter token reduction: it must be
lossless (every term the LLM will encounter in the chapter is kept) without
including unrelated glossary entries.
"""

from backend.models import GlossaryEntry
from backend.services.glossary import filter_glossary_for_chapter


def _entry(zh: str, en: str, locked: bool = False) -> GlossaryEntry:
    return GlossaryEntry(
        id=0,
        novel_id=1,
        term_zh=zh,
        term_en=en,
        category="character",
        notes=None,
        auto_detected=not locked,
        locked=locked,
    )


def test_filter_drops_terms_not_in_chapter() -> None:
    glossary = [
        _entry("天剑", "Heaven Sword"),
        _entry("地剑", "Earth Sword"),
        _entry("人剑", "Human Sword"),
    ]
    chapter = "他挥动着天剑，斩向敌人。"
    kept = filter_glossary_for_chapter(glossary, chapter)
    assert [g.term_zh for g in kept] == ["天剑"]


def test_filter_keeps_compound_substrings() -> None:
    # Per the plan: a chapter containing 天剑诀 should keep BOTH 天剑 and 天剑诀
    # so the longest-first rule in the LLM prompt still resolves correctly.
    glossary = [
        _entry("天剑", "Heaven Sword"),
        _entry("天剑诀", "Heaven Sword Art"),
    ]
    chapter = "他领悟了天剑诀的奥秘。"
    kept = sorted(g.term_zh for g in filter_glossary_for_chapter(glossary, chapter))
    assert kept == ["天剑", "天剑诀"]


def test_filter_matches_english_haystack_too() -> None:
    # Humanizer pass: haystack is the English translation, glossary terms
    # should still match via their `term_en`.
    glossary = [
        _entry("天剑", "Heaven Sword"),
        _entry("地剑", "Earth Sword"),
    ]
    english = "He raised the Heaven Sword and struck."
    kept = [g.term_zh for g in filter_glossary_for_chapter(glossary, english)]
    assert kept == ["天剑"]


def test_filter_empty_inputs_return_empty() -> None:
    assert filter_glossary_for_chapter([], "anything") == []
    assert filter_glossary_for_chapter([_entry("天", "Heaven")], "") == []
    assert filter_glossary_for_chapter([_entry("天", "Heaven")]) == []


def test_filter_handles_multiple_haystacks() -> None:
    glossary = [
        _entry("天剑", "Heaven Sword"),
        _entry("地剑", "Earth Sword"),
    ]
    # term appears in second haystack only — should still be kept.
    kept = [
        g.term_zh
        for g in filter_glossary_for_chapter(glossary, "no terms here", "天剑 appears here")
    ]
    assert kept == ["天剑"]


def test_filter_returns_locked_state_unchanged() -> None:
    locked = _entry("天剑", "Heaven Sword", locked=True)
    auto = _entry("地剑", "Earth Sword", locked=False)
    chapter = "天剑 与 地剑"
    kept = filter_glossary_for_chapter([locked, auto], chapter)
    by_zh = {g.term_zh: g for g in kept}
    assert by_zh["天剑"].locked is True
    assert by_zh["地剑"].locked is False
