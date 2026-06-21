"""Unit tests for the casing re-apply tool's pure diff helpers."""

from __future__ import annotations

from backend.scripts.reapply_term_casing import _word_pair, diff_runs


def test_diff_runs_finds_casing_changes():
    old = "Dao of dual cultivation"
    new = "Dao of Dual Cultivation"
    runs = diff_runs(old, new)
    # Two single-char flips: 'd'->'D' and 'c'->'C'.
    assert runs == [(7, 8), (12, 13)]


def test_diff_runs_empty_when_identical():
    assert diff_runs("same text", "same text") == []


def test_diff_runs_guards_length_mismatch():
    assert diff_runs("abc", "abcd") == []


def test_word_pair_expands_to_whole_word():
    old = "Dao of dual cultivation"
    new = "Dao of Dual Cultivation"
    # The diff at index 7 ('d'->'D') should expand to the whole word.
    assert _word_pair(old, new, 7, 8) == ("dual", "Dual")
    # The diff at index 12 ('c'->'C') -> "cultivation"/"Cultivation".
    assert _word_pair(old, new, 12, 13) == ("cultivation", "Cultivation")
