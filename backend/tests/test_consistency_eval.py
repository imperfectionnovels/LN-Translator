"""Unit tests for the read-only consistency eval harness (Phase 0).

Exercises the pure metric helpers directly, no DB. The async DB loaders are
thin SELECTs; the logic worth pinning lives in the pure functions.
"""

from __future__ import annotations

from backend.models import GlossaryEntry
from backend.scripts.consistency_eval import (
    Segment,
    bootstrap_ci,
    bracketed_identity_rate,
    mcnemar,
    segment_reuse_stats,
    segment_source,
    tcr_for_glossary,
)
from backend.services import tm as tm_svc


def _entry(**kw) -> GlossaryEntry:
    base = dict(
        id=1,
        novel_id=1,
        term_zh="金丹",
        term_en="Golden Core",
        category="technique",
        notes=None,
        usage_note=None,
        auto_detected=False,
        locked=True,
    )
    base.update(kw)
    return GlossaryEntry(**base)


# --- segment_source ---------------------------------------------------------


def test_segment_source_hash_matches_tm_keying():
    src = "第一章 测试\n\n这是第一段。\n\n这是第二段，比较长一点点的内容。"
    segs = segment_source(src)
    # Leading Chinese heading is dropped, two paragraphs remain.
    assert len(segs) == 2
    # The hash must equal what tm.py would store for the same paragraph.
    paras = tm_svc._drop_leading_heading(tm_svc._split_paragraphs(src))
    assert segs[0].source_hash == tm_svc._hash_source(paras[0])
    assert segs[1].source_hash == tm_svc._hash_source(paras[1])


def test_segment_source_flags_bracketed_and_substantive():
    src = (
        "短句。\n\n"
        "【系统提示：这是一个很长的系统面板描述文本内容，长度足够超过三十个字符的阈值。】"
    )
    segs = segment_source(src)
    assert segs[0].substantive is False  # short line
    assert segs[1].bracketed is True
    assert segs[1].substantive is True  # bracketed panel is long enough


# --- segment_reuse_stats ----------------------------------------------------


def test_segment_reuse_stats_counts_reuse_after_first_seen():
    chapters = [
        (1, [Segment("h1", 50, False, True), Segment("h2", 5, False, False)]),
        (2, [Segment("h1", 50, False, True), Segment("h3", 40, False, True)]),
        (3, [Segment("h1", 50, False, True)]),
    ]
    stored_first = {"h1": 1, "h2": 1, "h3": 2}
    stats = segment_reuse_stats(chapters, stored_first)

    alls = stats["all"]
    assert alls["segments"] == 5
    # h1 reused in ch2 and ch3 (2 of 5 segments); chars 100 of 195.
    assert abs(alls["reuse_rate"] - 2 / 5) < 1e-9
    assert abs(alls["reuse_rate_chars"] - 100 / 195) < 1e-9
    assert abs(alls["llm_coverage_chars"] - (1 - 100 / 195)) < 1e-9
    assert abs(alls["recurrence_rate"] - 2 / 5) < 1e-9

    sub = stats["substantive"]
    assert sub["segments"] == 4  # h2 excluded
    assert abs(sub["reuse_rate"] - 2 / 4) < 1e-9
    assert abs(sub["reuse_rate_chars"] - 100 / 190) < 1e-9


def test_segment_reuse_first_chapter_never_reuses():
    chapters = [(1, [Segment("h1", 10, False, False)])]
    stats = segment_reuse_stats(chapters, {"h1": 1})
    assert stats["all"]["reuse_rate"] == 0.0


# --- mcnemar ----------------------------------------------------------------


def test_mcnemar_symmetric_is_not_significant():
    _stat, p = mcnemar(8, 8)
    assert p > 0.5


def test_mcnemar_lopsided_is_significant():
    _stat, p = mcnemar(30, 4)
    assert p < 0.05


def test_mcnemar_no_discordant_pairs():
    stat, p = mcnemar(0, 0)
    assert stat == 0.0 and p == 1.0


# --- bootstrap_ci -----------------------------------------------------------


def test_bootstrap_ci_constant_values_collapse():
    lo, hi = bootstrap_ci([0.5, 0.5, 0.5, 0.5])
    assert lo == 0.5 and hi == 0.5


def test_bootstrap_ci_brackets_the_mean_and_is_deterministic():
    vals = [0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0]
    lo1, hi1 = bootstrap_ci(vals, seed=7)
    lo2, hi2 = bootstrap_ci(vals, seed=7)
    assert (lo1, hi1) == (lo2, hi2)  # deterministic
    assert lo1 <= 0.5 <= hi1


# --- bracketed_identity_rate ------------------------------------------------


def test_bracketed_identity_rate():
    rows = [
        ("a", "【x】", "A"),
        ("a", "【x】", "A"),  # group a: recurring + identical
        ("b", "【y】", "B1"),
        ("b", "【y】", "B2"),  # group b: recurring + divergent
        ("c", "plain", "C"),
        ("c", "plain", "C"),  # not bracketed, ignored
        ("d", "【z】", "D"),  # single occurrence, not recurring
    ]
    out = bracketed_identity_rate(rows)
    assert out["recurring_blocks"] == 2
    assert out["identical_blocks"] == 1
    assert abs(out["identity_rate"] - 0.5) < 1e-9


# --- tcr_for_glossary -------------------------------------------------------


def test_tcr_counts_present_and_missing():
    glossary = [_entry()]  # locked 金丹 -> Golden Core
    chapters = [
        (1, "他炼成了金丹。", "He formed a Golden Core."),  # consistent
        (2, "金丹境界。", "The golden seat realm."),  # checkable, inconsistent
    ]
    out = tcr_for_glossary(glossary, chapters)
    assert out["checkable"] == 2
    assert out["consistent"] == 1
    assert abs(out["overall_tcr"] - 0.5) < 1e-9
    assert out["by_category"]["technique"]["checkable"] == 2


def test_tcr_ignores_unlocked_and_absent_terms():
    glossary = [
        _entry(id=1, locked=False),  # unlocked: ignored
        _entry(id=2, term_zh="法宝", term_en="Magic Treasure"),  # absent from src
    ]
    chapters = [(1, "他炼成了金丹。", "He formed a Golden Core.")]
    out = tcr_for_glossary(glossary, chapters)
    # Unlocked 金丹 ignored; locked 法宝 never appears -> nothing checkable.
    assert out["checkable"] == 0
    assert out["overall_tcr"] == 1.0
