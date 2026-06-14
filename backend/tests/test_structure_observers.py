"""Tests for the calqued-structure observers (audit 2026-06-14).

Covers `detect_what_cleft` and `detect_orphan_which_clause` in
`text_observers` — log-only observers that flag CN topic-prominence carried
into English (是…的 what-clefts, 这说明 / 这意味着 orphan "Which …" fragments).
Both are telemetry, never gates, so the assertions are about what fires vs.
what stays clean — particularly the question-mark and "This …" suppressions
that keep false positives down.
"""

from __future__ import annotations

from backend.models import GlossaryEntry
from backend.services.text_observers import (
    body_correctness_observations,
    detect_orphan_which_clause,
    detect_what_cleft,
)

# ============================================================================
# What-cleft detector
# ============================================================================

class TestWhatCleft:
    def test_flags_basic_what_cleft(self):
        text = "What he wanted this life was the future!"
        flags = detect_what_cleft(text)
        assert len(flags) == 1
        assert "what-cleft" in flags[0].lower()
        assert "What he wanted this life was" in flags[0]

    def test_flags_cleft_with_non_ascii_name(self):
        text = "What Lü Yang was measuring himself against was Soaring Firmament."
        flags = detect_what_cleft(text)
        assert len(flags) == 1
        assert "Lü Yang" in flags[0]

    def test_flags_mid_text_after_terminator(self):
        text = "He paused. What he had carried all along was a single regret."
        flags = detect_what_cleft(text)
        assert len(flags) == 1

    def test_does_not_flag_short_question(self):
        # Copula sits right after "What" — no clause between → not a cleft.
        assert detect_what_cleft("What was that?") == []
        assert detect_what_cleft("What is this place?") == []

    def test_does_not_flag_embedded_question(self):
        # A full clause before the copula but the sentence is interrogative.
        assert detect_what_cleft("What did he say was true here?") == []

    def test_does_not_flag_non_initial_what(self):
        # "what" is not at a sentence start, so the subordinate clause is fine.
        text = "No matter what he wanted, it was already gone."
        assert detect_what_cleft(text) == []

    def test_dedupes_repeated_span(self):
        text = (
            "What he wanted was power. Later, again: What he wanted was power."
        )
        flags = detect_what_cleft(text)
        assert len(flags) == 1  # one issue string
        # the span is listed once despite two occurrences (the quoted
        # "…"-terminated span form, distinct from the example inside the message)
        assert flags[0].count('"What he wanted was…"') == 1

    def test_empty(self):
        assert detect_what_cleft("") == []


# ============================================================================
# Orphan "Which …" detector
# ============================================================================

class TestOrphanWhichClause:
    def test_flags_which_showed(self):
        text = (
            "The requirement was rigid. Which showed that Dao Attainment "
            "belonged to the rare few."
        )
        flags = detect_orphan_which_clause(text)
        assert len(flags) == 1
        assert "which" in flags[0].lower()
        assert "Which showed" in flags[0]

    def test_flags_which_meant_with_connective(self):
        text = "He failed. Which only meant the path was closed."
        flags = detect_orphan_which_clause(text)
        assert len(flags) == 1
        assert "Which only meant" in flags[0]

    def test_does_not_flag_this_showed(self):
        # Valid demonstrative subject — must stay clean.
        text = "He failed. This showed that the path was closed."
        assert detect_orphan_which_clause(text) == []

    def test_does_not_flag_which_question(self):
        assert detect_orphan_which_clause("Which is correct?") == []

    def test_does_not_flag_which_noun(self):
        # "Which path …" is a determiner, not the orphan-pronoun calque.
        text = "He hesitated. Which path he took did not matter."
        assert detect_orphan_which_clause(text) == []

    def test_empty(self):
        assert detect_orphan_which_clause("") == []


# ============================================================================
# Wiring into body_correctness_observations
# ============================================================================

class TestBodyObservationsWiring:
    def test_structure_observers_run_in_body_pass(self):
        glossary: list[GlossaryEntry] = []
        en_text = (
            "What he wanted was the future. The rule was rigid. "
            "Which showed that the path was narrow."
        )
        # source_zh is unused by these two observers; pass a stub.
        found = body_correctness_observations("源文", en_text, glossary)
        joined = " ".join(found)
        assert "what-cleft" in joined.lower()
        assert "which" in joined.lower()

    def test_clean_prose_not_flagged(self):
        glossary: list[GlossaryEntry] = []
        en_text = (
            "He wanted the future. The rule was rigid, which showed that the "
            "path was narrow. This proved his point."
        )
        found = body_correctness_observations("源文", en_text, glossary)
        joined = " ".join(found).lower()
        assert "what-cleft" not in joined
        assert "orphan 'which'" not in joined
