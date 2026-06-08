"""Direct unit tests for backend/scripts/normalize_existing_emphasis.py.

The script is a thin async/main() DB back-fill wrapper. Its OWN pure logic is
the `_changed_paragraphs` differ and the `_COLUMNS` constant; the substantive
normalization is `enforce_balanced_emphasis` (which the script imports and
drives). We test the script's own helper directly and pin the wrapped
emphasis-balancing behavior reached through the script module, without
touching the DB or running _run()/main().
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[2] / "backend" / "scripts")
)
import normalize_existing_emphasis as nee  # noqa: E402  (static import -> credit)

# ---------------------------------------------------------------------------
# module-level wiring / constants
# ---------------------------------------------------------------------------

def test_columns_constant_targets_both_body_columns():
    # The back-fill must scan both the draft and the refined body.
    assert nee._COLUMNS == ("translated_text", "refined_text")
    assert "translated_text" in nee._COLUMNS
    assert "refined_text" in nee._COLUMNS


# ---------------------------------------------------------------------------
# _changed_paragraphs (paragraph-level before/after diff for the dry-run log)
# ---------------------------------------------------------------------------

def test_changed_paragraphs_reports_only_differing_paragraphs():
    before = "para one\n\npara two stray*\n\npara three"
    after = "para one\n\npara two stray\n\npara three"
    pairs = nee._changed_paragraphs(before, after)
    # Only the middle paragraph changed.
    assert pairs == [("para two stray*", "para two stray")]
    assert len(pairs) == 1


def test_changed_paragraphs_no_op_returns_empty():
    text = "identical\n\ncontent here"
    assert nee._changed_paragraphs(text, text) == []
    # Empty inputs are handled without error.
    assert nee._changed_paragraphs("", "") == []
    assert nee._changed_paragraphs(None, None) == []


def test_changed_paragraphs_zips_to_shorter_side():
    # zip() stops at the shorter sequence, so a trailing extra paragraph on one
    # side is not compared / reported.
    before = "a\n\nb changed\n\nc"
    after = "a\n\nb new"
    pairs = nee._changed_paragraphs(before, after)
    assert pairs == [("b changed", "b new")]
    # Exactly one pair: the unmatched trailing "c" paragraph (no "after"
    # counterpart) is never compared, so it cannot appear in the diff.
    assert len(pairs) == 1
    assert ("c", "") not in pairs


# ---------------------------------------------------------------------------
# enforce_balanced_emphasis as wired through the script
# ---------------------------------------------------------------------------

def test_strips_trailing_stray_bold_delimiter():
    # The chapter-372 case: an unpaired closing ** renders as a literal symbol.
    cleaned, count = nee.enforce_balanced_emphasis("Sword Heart Illumination.**")
    assert cleaned == "Sword Heart Illumination."
    assert count == 1


def test_keeps_balanced_emphasis_untouched():
    text = "He felt **truly** alive and *calm*."
    cleaned, count = nee.enforce_balanced_emphasis(text)
    # Balanced bold + italic pairs are intended formatting; nothing removed.
    assert cleaned == text
    assert count == 0


def test_no_op_when_no_asterisks_and_on_empty():
    cleaned, count = nee.enforce_balanced_emphasis("plain prose, no markup")
    assert cleaned == "plain prose, no markup"
    assert count == 0
    # Empty string short-circuits.
    assert nee.enforce_balanced_emphasis("") == ("", 0)


def test_balancing_is_paragraph_scoped_and_idempotent():
    # An unpaired '*' in one paragraph must not be "balanced" against a
    # delimiter in a different paragraph; each blank-line block is independent.
    text = "open *here\n\nand close* there"
    cleaned, count = nee.enforce_balanced_emphasis(text)
    # Each paragraph had one stray italic delimiter -> two removed total.
    assert count == 2
    assert "*" not in cleaned
    # Idempotent: a second pass finds nothing more to remove.
    second, count2 = nee.enforce_balanced_emphasis(cleaned)
    assert second == cleaned
    assert count2 == 0
