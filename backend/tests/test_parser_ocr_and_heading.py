"""Behavior tests for two previously-uncovered parser units.

- split_leading_heading: used by bulk import, where each uploaded file is one
  chapter and its real title sits on the first line. Pins the first-line-only
  detection rule, the CRLF normalization, the no-newline guard, and the
  no-heading passthrough.
- detect_ocr_issues: the OCR-corruption scorer behind the import pre-check.
  Pins the short-input guard, each of the five heuristics, the score / flagged
  threshold, and the clean-text floor.

Pure functions, no DB / network, so these match the plain-function style of
test_parser.py.
"""

from __future__ import annotations

from backend.services.parser import detect_ocr_issues, split_leading_heading

# ============================================================
# split_leading_heading
# ============================================================

def test_split_heading_cn_chapter_marker():
    """A 第N章 first line is recognized as the heading and split from body."""
    heading, body = split_leading_heading("第十二章 风云突变\n正文开始的内容。")
    assert heading == "第十二章 风云突变"
    assert body == "正文开始的内容。"


def test_split_heading_english_chapter_marker():
    heading, body = split_leading_heading("Chapter 7: The Gate\nFirst paragraph.")
    assert heading == "Chapter 7: The Gate"
    assert body == "First paragraph."


def test_split_heading_prologue_marker():
    """Numberless markers like 楔子 / 序章 are valid headings too."""
    heading, body = split_leading_heading("楔子\n很久以前的故事。")
    assert heading == "楔子"
    assert body == "很久以前的故事。"


def test_split_heading_no_heading_returns_original_text():
    """A first line that is not a chapter heading leaves the text untouched and
    returns None for the heading (not a best-effort first-line guess)."""
    text = "这只是普通的第一段。\n第二段在这里。"
    heading, body = split_leading_heading(text)
    assert heading is None
    assert body == text


def test_split_heading_only_first_line_is_tested():
    """A 第N章 reference deeper in the body is not treated as a heading."""
    text = "这是开头段落，不是标题。\n第三章 后面才出现。"
    heading, body = split_leading_heading(text)
    assert heading is None
    assert body == text


def test_split_heading_no_newline_left_untouched():
    """A single-line file (no newline) has no heading/body split to make."""
    text = "第一章 没有换行的整段内容"
    heading, body = split_leading_heading(text)
    assert heading is None
    assert body == text


def test_split_heading_normalizes_crlf():
    """CRLF / CR line endings are normalized before the first-line split."""
    heading, body = split_leading_heading("Chapter 1 Dawn\r\nThe body text.")
    assert heading == "Chapter 1 Dawn"
    assert body == "The body text."


def test_split_heading_skips_leading_blank_lines():
    """Leading blank lines / whitespace before the heading are stripped so the
    heading on the first real line is still detected."""
    heading, body = split_leading_heading("\n\n  第五章 启程\n出发了。")
    assert heading == "第五章 启程"
    assert body == "出发了。"


# ============================================================
# detect_ocr_issues
# ============================================================

def test_ocr_short_input_is_unflagged():
    """Inputs shorter than 200 chars short-circuit to a clean zero payload."""
    result = detect_ocr_issues("太短了")
    assert result == {"score": 0, "issues": [], "flagged": False}


def test_ocr_empty_input_is_unflagged():
    result = detect_ocr_issues("")
    assert result["score"] == 0
    assert result["flagged"] is False


def test_ocr_clean_text_scores_zero():
    """Well-formed CN prose (terminated paragraphs, no repeats, no Latin runs)
    scores 0 and is not flagged."""
    para = "这是一段写得很好的中文，每一句都以句号结束。" * 12 + "完。"
    text = para + "\n\n" + para
    result = detect_ocr_issues(text)
    assert result["score"] == 0
    assert result["issues"] == []
    assert result["flagged"] is False


def test_ocr_replacement_chars_flagged():
    """A heavy run of U+FFFD replacement chars drives the score over the flag
    threshold on its own. The FFFD heuristic adds min(40, count), so 30 of
    them scores 30, clearing the >= 25 flag line."""
    text = "正常的中文内容用来凑够长度。" * 20 + ("错误" + "�" * 30)
    result = detect_ocr_issues(text)
    assert any("replacement chars" in i for i in result["issues"])
    assert result["score"] == 30
    assert result["flagged"] is True


def test_ocr_replacement_chars_under_threshold_not_counted():
    """Exactly 5 replacement chars (not > 5) does not trip the FFFD heuristic."""
    text = "正常的中文内容用来凑够足够的长度。" * 20 + "�" * 5
    result = detect_ocr_issues(text)
    assert not any("replacement chars" in i for i in result["issues"])


def test_ocr_duplicate_lines_flagged():
    """A line repeated >= 3x (the OCR buffer-flush artifact) is detected."""
    dup = "第二百九十八页 页眉信息重复出现"
    body = "正常的故事内容在这里铺陈开来用来凑足字数。" * 15
    text = body + "\n" + "\n".join([dup] * 4)
    result = detect_ocr_issues(text)
    assert any("duplicate line" in i for i in result["issues"])
    assert result["score"] > 0


def test_ocr_stray_latin_run_flagged():
    """A long Latin-only run inside otherwise-CJK text (leaked header/footer)
    is detected once the CJK char count clears 500."""
    cjk = "中文段落填充内容用来超过五百个汉字的阈值并保持语义连贯。" * 25
    text = cjk + "\n\n" + "abcdefghijklmnopqrstuvwxyz" * 2
    result = detect_ocr_issues(text)
    assert any("Latin runs" in i for i in result["issues"])


def test_ocr_missing_terminal_punctuation_flagged():
    """Two or more long paragraphs lacking terminal punctuation are flagged."""
    unterminated = "这是一段没有结尾标点的长段落内容一直延续下去却始终不收尾" * 8
    text = unterminated + "\n\n" + unterminated
    result = detect_ocr_issues(text)
    assert any("terminal punctuation" in i for i in result["issues"])
    assert result["score"] > 0


def test_ocr_score_caps_at_100_and_flags():
    """Stacking heuristics past 100 pre-cap clamps the score to 100.

    Pre-cap contributions, summing well over 100:
      - 5 unterminated long paragraphs: min(30, 5*6) = 30
      - 5 distinct lines each repeated >=3x: min(25, 5*5) = 25
      - 3 long Latin runs in CN text: min(15, 3*5) = 15
      - 40 U+FFFD chars: min(40, 40) = 40
    Total 110, clamped to 100.
    """
    # 5 long paragraphs with no terminal punctuation (each distinct so they do
    # not also collapse into the duplicate-line heuristic).
    unterminated_paras = "\n\n".join(
        ("没有结尾标点的超长段落内容持续不断地延伸而不收束句尾" * 8) + f"编号{i}"
        for i in range(5)
    )
    # 5 distinct lines, each repeated 4x → 5 lines with count >= 3.
    dup_lines = "\n".join(
        "\n".join([f"重复出现的页眉行内容用来制造重复行问题信号编号{i}"] * 4)
        for i in range(5)
    )
    # CJK >500 + 3 long Latin runs + 40 replacement chars in one block.
    cjk_block = "中文内容填充" * 120 + ("abcdefghijklmnopqrstuvwxyz" * 2 + "。") * 3
    fffd_block = "中文" * 30 + "�" * 40

    text = (
        unterminated_paras + "\n\n" + dup_lines + "\n\n"
        + cjk_block + "\n\n" + fffd_block
    )
    result = detect_ocr_issues(text)
    assert result["score"] == 100
    assert result["flagged"] is True
    assert len(result["issues"]) >= 3
