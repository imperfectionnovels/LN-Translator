"""Direct unit tests for backend/scripts/diff_against_edit.py.

This script is a thin async/main() wrapper around re-exported pure helpers
(_clip, _score from ab_style_edits; _align_pairs, _split_paras from
ingest_edited_chapter). We pin the pure diff/score/clip behavior reached
THROUGH the diff_against_edit module namespace (so the static import credits
the script), without touching the DB or running run()/main().
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[2] / "backend" / "scripts")
)
import diff_against_edit  # noqa: E402  (static import -> module-credit mechanic)

# ---------------------------------------------------------------------------
# _split_paras (blank-line splitter + strip + drop-empty)
# ---------------------------------------------------------------------------

def test_split_paras_splits_on_blank_lines_and_strips():
    paras = diff_against_edit._split_paras("  one  \n\n two \n\n\n three ")
    assert paras == ["one", "two", "three"]
    # Whitespace was stripped from each paragraph.
    assert all(p == p.strip() for p in paras)


def test_split_paras_empty_and_whitespace_only_yield_no_paras():
    assert diff_against_edit._split_paras("") == []
    assert diff_against_edit._split_paras(None) == []
    assert diff_against_edit._split_paras("\n\n   \n\n") == []


# ---------------------------------------------------------------------------
# _align_pairs (difflib opcode pairing of changed paragraphs)
# ---------------------------------------------------------------------------

def test_align_pairs_pairs_only_changed_paragraphs():
    draft = ["alpha unchanged", "beta original", "gamma unchanged"]
    edited = ["alpha unchanged", "beta rewritten", "gamma unchanged"]
    pairs, inserted, deleted = diff_against_edit._align_pairs(draft, edited)
    # Only the middle paragraph changed; the equal ones are not paired.
    assert pairs == [("beta original", "beta rewritten")]
    assert inserted == 0
    assert deleted == 0


def test_align_pairs_counts_pure_insertions():
    draft = ["one", "two"]
    edited = ["one", "two", "three added", "four added"]
    pairs, inserted, deleted = diff_against_edit._align_pairs(draft, edited)
    # Appended paragraphs are counted as inserts, not changed-pairs.
    assert pairs == []
    assert inserted == 2
    assert deleted == 0


def test_align_pairs_counts_pure_deletions():
    draft = ["keep this", "drop this one", "drop this two"]
    edited = ["keep this"]
    pairs, inserted, deleted = diff_against_edit._align_pairs(draft, edited)
    assert pairs == []
    assert inserted == 0
    assert deleted == 2


def test_align_pairs_ignores_whitespace_only_differences():
    # _align_pairs normalizes whitespace before deciding a pair "changed".
    draft = ["the   quick brown fox"]
    edited = ["the quick brown   fox"]
    pairs, inserted, deleted = diff_against_edit._align_pairs(draft, edited)
    assert pairs == []
    assert inserted == 0
    assert deleted == 0


# ---------------------------------------------------------------------------
# _score (align candidate to before_text, ratio vs after_text)
# ---------------------------------------------------------------------------

def test_score_picks_candidate_closest_to_before_then_rates_against_after():
    before = "the dragon roared in the valley"
    after = "the dragon bellowed in the valley"
    candidates = [
        "completely unrelated sentence here",
        "the dragon bellowed in the valley",  # matches the edit exactly
    ]
    ratio, best = diff_against_edit._score(candidates, before, after)
    # The candidate closest to `before` is the second one, which equals `after`.
    assert best == "the dragon bellowed in the valley"
    assert ratio == 1.0


def test_score_empty_candidate_list_returns_zero():
    ratio, best = diff_against_edit._score([], "before", "after")
    assert ratio == 0.0
    assert best == ""


def test_score_ratio_is_bounded_and_reflects_dissimilarity():
    ratio, best = diff_against_edit._score(
        ["xxxxx yyyyy zzzzz"], "the cat sat", "the dog ran"
    )
    # A wholly dissimilar candidate yields a low (but well-defined) ratio.
    assert 0.0 <= ratio < 0.5
    assert best == "xxxxx yyyyy zzzzz"


# ---------------------------------------------------------------------------
# _clip (whitespace-collapse + truncate with ellipsis)
# ---------------------------------------------------------------------------

def test_clip_collapses_whitespace_and_truncates_long_text():
    long_text = "word " * 100  # 500 chars before collapse, well over 220
    clipped = diff_against_edit._clip(long_text)
    assert clipped.endswith("...")
    # default cap is 220 content chars + the 3-char ellipsis
    assert len(clipped) == 223


def test_clip_short_text_is_collapsed_but_not_truncated():
    clipped = diff_against_edit._clip("  hello\n\tworld  ")
    assert clipped == "hello world"
    assert not clipped.endswith("...")
