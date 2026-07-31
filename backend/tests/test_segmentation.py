"""Unit tests for backend/services/segmentation.py (CAT Phase 1).

The heuristic is frozen once shipped (SEGMENTATION_VERSION stamps it), so
these tests pin the deliberate terminal-punctuation decisions: CJK and ASCII
sentence enders, quote-final dialogue, conditional closing brackets, divider
lines, multi-way join chains, CJK-vs-Latin join separators, and idempotence.
"""

from __future__ import annotations

from backend.services.segmentation import (
    SEGMENTATION_VERSION,
    effective_source_paragraphs,
    split_target_paragraphs,
)

# ---------------------------------------------------------------------------
# Version constant
# ---------------------------------------------------------------------------


def test_segmentation_version_is_two():
    # v2 (2026-07-31): chapter_source_paragraphs composed the author-update
    # marker strip into the canonical recipe (writer unification).
    assert SEGMENTATION_VERSION == 2


# ---------------------------------------------------------------------------
# Heading strip
# ---------------------------------------------------------------------------


def test_leading_chapter_heading_is_dropped():
    text = "第392章 惊变！\n\n白莲教举教齐至。\n\n正文继续。"
    assert effective_source_paragraphs(text) == ["白莲教举教齐至。", "正文继续。"]


def test_heading_variants_hui_and_jie_are_dropped():
    assert effective_source_paragraphs("第三回 大闹天宫\n\n正文。") == ["正文。"]
    assert effective_source_paragraphs("第12节 序幕\n\n正文。") == ["正文。"]


def test_non_heading_first_paragraph_is_kept():
    text = "他走了进来。\n\n屋里很静。"
    assert effective_source_paragraphs(text) == ["他走了进来。", "屋里很静。"]


def test_heading_only_text_yields_empty_list():
    assert effective_source_paragraphs("第1章 开端") == []


def test_ordinal_time_phrase_is_not_a_heading():
    # 第二天 ("the next day") starts with 第 + a numeral but has no 章/回/节
    # marker: it is prose and must survive. A future widening of _HEADING_RE
    # that swallows it re-keys stored segmentations and must fail here.
    text = "第二天，他醒了。\n\n窗外下着雨。"
    assert effective_source_paragraphs(text) == ["第二天，他醒了。", "窗外下着雨。"]


def test_english_chapter_heading_is_not_dropped():
    # The detector is deliberately conservative: only the CJK 第N章/回/节
    # shape counts, so an English "Chapter N" first line is NOT dropped.
    # (It then joins the next paragraph because "Gate" is a non-terminal
    # ending; the point pinned here is that the line survives at all.)
    text = "Chapter 5: The Gate\n\nHe stepped through."
    assert effective_source_paragraphs(text) == [
        "Chapter 5: The Gate He stepped through.",
    ]


# ---------------------------------------------------------------------------
# Blank-line split
# ---------------------------------------------------------------------------


def test_crlf_blank_lines_split_like_lf():
    text = "第一段。\r\n\r\n第二段。"
    assert effective_source_paragraphs(text) == ["第一段。", "第二段。"]


def test_multiple_blank_lines_collapse_to_one_break():
    text = "第一段。\n\n\n\n第二段。"
    assert effective_source_paragraphs(text) == ["第一段。", "第二段。"]


def test_paragraph_internal_single_newlines_are_preserved():
    text = "第一行，\n第二行。\n\n下一段。"
    assert effective_source_paragraphs(text) == ["第一行，\n第二行。", "下一段。"]


# ---------------------------------------------------------------------------
# Terminal endings: no join
# ---------------------------------------------------------------------------


def test_cjk_terminal_punctuation_does_not_join():
    for punct in "。！？…；：":
        text = f"前一段{punct}\n\n后一段。"
        assert effective_source_paragraphs(text) == [f"前一段{punct}", "后一段。"], punct


def test_ascii_terminal_punctuation_does_not_join():
    for punct in ".!?;:":
        text = f"First paragraph{punct}\n\nSecond one."
        got = effective_source_paragraphs(text)
        assert got == [f"First paragraph{punct}", "Second one."], punct


def test_source_ellipsis_run_does_not_join():
    text = "他沉默了……\n\n良久才开口。"
    assert effective_source_paragraphs(text) == ["他沉默了……", "良久才开口。"]


def test_closing_quote_final_dialogue_does_not_join():
    # Dialogue often ends with no punctuation inside the quote; the closing
    # quote itself marks a complete utterance.
    text = "「你来了」\n\n他说道。"
    assert effective_source_paragraphs(text) == ["「你来了」", "他说道。"]


def test_fullwidth_double_quote_final_does_not_join():
    text = "“我知道了。”\n\n她转身离开。"
    assert effective_source_paragraphs(text) == ["“我知道了。”", "她转身离开。"]


def test_lenticular_bracket_status_line_does_not_join():
    text = "【境界：筑基三层】\n\n他睁开眼。"
    assert effective_source_paragraphs(text) == ["【境界：筑基三层】", "他睁开眼。"]


def test_closing_bracket_after_terminal_punct_does_not_join():
    text = "他大喝一声（好胆！）\n\n随即出剑。"
    assert effective_source_paragraphs(text) == ["他大喝一声（好胆！）", "随即出剑。"]


def test_pure_divider_line_does_not_join():
    text = "上一幕结束。\n\n***\n\n新的一幕开始。"
    assert effective_source_paragraphs(text) == ["上一幕结束。", "***", "新的一幕开始。"]


# ---------------------------------------------------------------------------
# Mid-sentence endings: join
# ---------------------------------------------------------------------------


def test_cjk_comma_ending_joins_with_no_separator():
    text = "他说，\n\n这不可能。"
    assert effective_source_paragraphs(text) == ["他说，这不可能。"]


def test_bare_ideograph_ending_joins_with_no_separator():
    text = "他缓缓地\n\n抬起头。"
    assert effective_source_paragraphs(text) == ["他缓缓地抬起头。"]


def test_latin_word_ending_joins_with_single_space():
    text = "He said\n\nthat it was over."
    assert effective_source_paragraphs(text) == ["He said that it was over."]


def test_ascii_comma_ending_joins_with_single_space():
    text = "He paused,\n\nthen spoke."
    assert effective_source_paragraphs(text) == ["He paused, then spoke."]


def test_closing_bracket_without_terminal_punct_joins():
    # A mid-sentence parenthetical does not end the sentence.
    text = "他说（大概）\n\n还要三天。"
    assert effective_source_paragraphs(text) == ["他说（大概）还要三天。"]


def test_multi_way_chain_joins_until_terminal():
    text = "第一片，\n\n第二片\n\n第三片。\n\n独立段。"
    assert effective_source_paragraphs(text) == ["第一片，第二片第三片。", "独立段。"]


def test_trailing_non_terminal_paragraph_is_kept():
    # Nothing to join onto: the final fragment stays a paragraph of its own.
    text = "完整的一段。\n\n未完的一段，"
    assert effective_source_paragraphs(text) == ["完整的一段。", "未完的一段，"]


# ---------------------------------------------------------------------------
# Empty / whitespace input
# ---------------------------------------------------------------------------


def test_empty_and_whitespace_input():
    assert effective_source_paragraphs("") == []
    assert effective_source_paragraphs("   \n\n  \r\n ") == []


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_idempotent_on_already_joined_text():
    text = (
        "第7章 山门\n\n"
        "他说，\n\n这不可能。\n\n"
        "「你来了」\n\n"
        "He said\n\nthat it was over.\n\n"
        "***\n\n"
        "尾声。"
    )
    first = effective_source_paragraphs(text)
    second = effective_source_paragraphs("\n\n".join(first))
    assert second == first


# ---------------------------------------------------------------------------
# split_target_paragraphs
# ---------------------------------------------------------------------------


def test_split_target_paragraphs_blank_line_split():
    body = "Para one.\n\nPara two.\n\n\nPara three."
    assert split_target_paragraphs(body) == ["Para one.", "Para two.", "Para three."]


def test_split_target_paragraphs_crlf_and_empties():
    body = "A.\r\n\r\n\r\nB.\r\n\r\n   \r\n\r\nC."
    assert split_target_paragraphs(body) == ["A.", "B.", "C."]


def test_split_target_paragraphs_empty():
    assert split_target_paragraphs("") == []
    assert split_target_paragraphs("  \n\n ") == []
