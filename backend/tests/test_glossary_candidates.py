"""Tests for filter_glossary_candidates — which extracted new_terms are
admitted to the glossary.

A term is admitted when it appears inside a 【】 system-interface span OR
recurs at least twice in the chapter. The recurrence gate replaced the old
【】-only restriction so recurring narrative vocabulary (越级, 偷袭) reaches
the glossary and stays consistent across chapters.
"""

from backend.models import NewTerm
from backend.services.glossary import filter_glossary_candidates


def _term(zh: str, en: str = "X") -> NewTerm:
    return NewTerm(zh=zh, en=en, category="other")


def test_bracketed_term_admitted_even_when_it_appears_once():
    chapter = "他打开面板，【状态：修炼中】只出现一次。"
    kept = filter_glossary_candidates(chapter, [_term("状态")])
    assert [t.zh for t in kept] == ["状态"]


def test_recurring_narrative_term_admitted():
    # 越级 recurs three times, no 【】 anywhere — the old filter dropped it.
    chapter = "他越级挑战。越级而战需要勇气。又一次越级。"
    kept = filter_glossary_candidates(chapter, [_term("越级", "cross-realm")])
    assert [t.zh for t in kept] == ["越级"]


def test_one_off_narrative_term_rejected():
    chapter = "这个生僻词只出现一次而已，背景里没有方括号。"
    kept = filter_glossary_candidates(chapter, [_term("生僻词")])
    assert kept == []


def test_bracket_free_chapter_still_yields_recurring_terms():
    # filter_to_bracketed_terms used to return [] for any chapter with no 【】.
    chapter = "偷袭得手。再次偷袭。"
    kept = filter_glossary_candidates(chapter, [_term("偷袭", "sneak attack")])
    assert [t.zh for t in kept] == ["偷袭"]


def test_empty_chapter_yields_nothing():
    assert filter_glossary_candidates("", [_term("越级")]) == []
