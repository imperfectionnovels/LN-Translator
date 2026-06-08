"""Direct unit tests for backend/scripts/ab_style_edits.py.

The A/B style-edits harness is a thin async/main() wrapper around a handful
of PURE helper functions (_clip, _score, _dedupe_pairs) that are also reused
by the sibling learn-from-edits scripts. We pin the pure clipping / scoring /
dedup behavior here WITHOUT touching the DB, the provider, or running
run()/main() (those do real model + DB work).

The static `import ab_style_edits` at module top is deliberate: the coverage
tool maps a test file to the production module it credits by the static import
statements, so importing the script by name (after putting backend/scripts on
sys.path) is what attributes this coverage to
backend/scripts/ab_style_edits.py.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[2] / "backend" / "scripts")
)
import ab_style_edits  # noqa: E402  (static import -> module-credit mechanic)

# ---------------------------------------------------------------------------
# _clip (whitespace-collapse + truncate with trailing ellipsis)
# ---------------------------------------------------------------------------

def test_clip_collapses_internal_whitespace_runs():
    # Tabs, newlines, and multi-space runs all collapse to single spaces,
    # and leading/trailing whitespace is stripped.
    clipped = ab_style_edits._clip("  hello\n\tworld   again  ")
    assert clipped == "hello world again"
    assert "\n" not in clipped
    assert "\t" not in clipped
    assert "  " not in clipped


def test_clip_short_text_is_not_truncated():
    clipped = ab_style_edits._clip("a short line")
    assert clipped == "a short line"
    assert not clipped.endswith("...")
    assert len(clipped) == len("a short line")


def test_clip_truncates_long_text_to_n_plus_ellipsis():
    long_text = "word " * 100  # 500 chars; collapses to "word word ... word"
    clipped = ab_style_edits._clip(long_text)
    # Default cap is 220 content chars, then a literal "..." is appended.
    assert clipped.endswith("...")
    assert len(clipped) == 223
    # The retained head is exactly the first 220 collapsed chars.
    collapsed = " ".join(long_text.split())
    assert clipped == collapsed[:220] + "..."


def test_clip_respects_custom_n_and_handles_none():
    # Explicit n controls the cap; below-cap text is returned verbatim.
    assert ab_style_edits._clip("abcdef", n=3) == "abc..."
    assert ab_style_edits._clip("ab", n=3) == "ab"
    # None / empty input collapses to the empty string, never raises.
    assert ab_style_edits._clip(None) == ""
    assert ab_style_edits._clip("") == ""


def test_clip_boundary_exactly_n_is_not_truncated():
    text = "x" * 220
    clipped = ab_style_edits._clip(text)
    # Exactly n chars -> returned as-is (the cap is "len <= n").
    assert clipped == text
    assert not clipped.endswith("...")
    # One char over the cap -> truncated.
    over = "y" * 221
    assert ab_style_edits._clip(over) == "y" * 220 + "..."


# ---------------------------------------------------------------------------
# _score (align candidate to before_text, ratio vs after_text)
# ---------------------------------------------------------------------------

def test_score_picks_candidate_closest_to_before_then_rates_against_after():
    before = "the dragon roared in the valley"
    after = "the dragon bellowed in the valley"
    candidates = [
        "completely unrelated sentence here",
        "the dragon bellowed in the valley",  # equals the edit exactly
    ]
    ratio, best = ab_style_edits._score(candidates, before, after)
    # Of the two, the second is closest to `before`; it also equals `after`,
    # so the returned similarity to `after` is a perfect 1.0.
    assert best == "the dragon bellowed in the valley"
    assert ratio == 1.0


def test_score_alignment_is_by_before_not_after():
    # One candidate is identical to `after` but FAR from `before`; another is
    # close to `before` but only moderately close to `after`. Alignment must
    # use `before`, so the moderate candidate wins the alignment and its
    # (lower) similarity to `after` is what gets scored.
    before = "alpha beta gamma delta"
    after = "completely different target string"
    candidates = [
        "alpha beta gamma epsilon",        # near `before`, far from `after`
        "completely different target string",  # equals `after`, far from `before`
    ]
    ratio, best = ab_style_edits._score(candidates, before, after)
    assert best == "alpha beta gamma epsilon"
    # Because we did NOT align on `after`, the perfect-match candidate was
    # not chosen, so the score is strictly below 1.0.
    assert ratio < 1.0


def test_score_empty_candidate_list_returns_zero():
    ratio, best = ab_style_edits._score([], "before", "after")
    assert ratio == 0.0
    assert best == ""


def test_score_ratio_is_bounded_and_low_for_dissimilar():
    ratio, best = ab_style_edits._score(
        ["xxxxx yyyyy zzzzz"], "the cat sat", "the dog ran"
    )
    # A wholly dissimilar candidate yields a low, well-defined ratio in [0, 1).
    assert 0.0 <= ratio < 0.5
    assert best == "xxxxx yyyyy zzzzz"


def test_score_single_candidate_is_always_selected():
    # With one candidate there is nothing to choose; it is the "best" and the
    # ratio is its raw difflib similarity to `after`.
    import difflib

    cand = "a partially matching line of prose"
    after = "a partly matching line of text"
    ratio, best = ab_style_edits._score([cand], "any before text", after)
    assert best == cand
    expected = difflib.SequenceMatcher(None, cand, after).ratio()
    assert ratio == expected


# ---------------------------------------------------------------------------
# _dedupe_pairs (mirror fetch_style_edits' within-window dedup)
# ---------------------------------------------------------------------------

class _Row(dict):
    """Stand-in for an aiosqlite.Row: supports row["col"] access."""


def _row(before, after):
    return _Row(before_text=before, after_text=after)


def test_dedupe_pairs_preserves_order_and_drops_exact_dupes():
    rows = [
        _row("draft one", "edit one"),
        _row("draft two", "edit two"),
        _row("draft one", "edit one"),  # exact dup of the first -> dropped
        _row("draft three", "edit three"),
    ]
    out = ab_style_edits._dedupe_pairs(rows)
    assert out == [
        ("draft one", "edit one"),
        ("draft two", "edit two"),
        ("draft three", "edit three"),
    ]
    # Output is a list of (before, after) tuples, not Row objects.
    assert all(isinstance(p, tuple) and len(p) == 2 for p in out)


def test_dedupe_pairs_treats_differing_after_as_distinct():
    # Same before_text but a different after_text is a genuinely different
    # pair and must be kept.
    rows = [
        _row("same draft", "first edit"),
        _row("same draft", "second edit"),
    ]
    out = ab_style_edits._dedupe_pairs(rows)
    assert out == [("same draft", "first edit"), ("same draft", "second edit")]
    assert len(out) == 2


def test_dedupe_pairs_empty_input_returns_empty_list():
    out = ab_style_edits._dedupe_pairs([])
    assert out == []
    assert isinstance(out, list)
