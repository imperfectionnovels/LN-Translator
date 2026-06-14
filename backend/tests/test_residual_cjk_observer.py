"""Tests for the residual-CJK observer (translation-review fix 2026-06-14).

`detect_residual_cjk` is a log-only observer: it flags runs of Han ideographs
left in the English output (a glossary term the model never rendered, or an
OCR-garbled source token copied straight through). It scans the OUTPUT only and
is scoped strictly to ideographs (U+3400-U+9FFF) so romanized names like
"Hong Yun" never trip it.
"""

from __future__ import annotations

from backend.services.text_observers import (
    body_correctness_observations,
    detect_residual_cjk,
)


class TestDetectResidualCjk:
    def test_empty(self):
        assert detect_residual_cjk("") == []

    def test_clean_english(self):
        assert detect_residual_cjk("He drew his sword and struck the foe.") == []

    def test_romanized_name_not_flagged(self):
        # "Hong Yun" is an intentional romanized name, not residual CJK.
        assert detect_residual_cjk("The Hong Yun Golden Nature held firm.") == []

    def test_flags_a_leftover_han_run(self):
        flags = detect_residual_cjk("He raised the 天吴 banner high.")
        assert len(flags) == 1
        assert "residual CJK" in flags[0]
        assert "天吴" in flags[0]

    def test_repeated_run_shows_count(self):
        flags = detect_residual_cjk("Here 天吴 and there 天吴 again.")
        assert len(flags) == 1
        assert "(2x)" in flags[0]

    def test_most_frequent_ordered_first(self):
        # 甲 appears 3x, 乙 once → 甲 must precede 乙 in the message.
        flags = detect_residual_cjk("甲 again 甲 and 甲, then 乙.")
        assert len(flags) == 1
        msg = flags[0]
        assert "(3x)" in msg
        assert msg.index("甲") < msg.index("乙")

    def test_more_than_five_distinct_shows_plus_n_more(self):
        # Six distinct single-char runs → five shown + "(+1 more)".
        flags = detect_residual_cjk("Stray: 甲 乙 丙 丁 戊 己 here.")
        assert len(flags) == 1
        assert "(+1 more)" in flags[0]

    def test_fires_via_body_correctness_observations(self):
        found = body_correctness_observations("源文", "A run of 天吴 here.", [])
        assert any("residual CJK" in f for f in found)

    def test_cjk_source_clean_output_does_not_fire(self):
        # CJK in the SOURCE must not fire; the observer scans the output only.
        found = body_correctness_observations(
            "这是中文源文", "Clean English output only.", []
        )
        assert not any("residual CJK" in f for f in found)
