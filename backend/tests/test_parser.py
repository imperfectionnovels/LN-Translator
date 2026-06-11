"""Parser tests — chapter splitting, heading-derived numbering, the chunk
fallback."""

from backend.services.parser import (
    CHUNK_SIZE,
    ParsedChapter,
    extract_heading_number,
    normalize_title_en,
    parse_chapters,
    reconcile_chapter_numbers,
    sanitize_title_punctuation,
    strip_heading_update_marker,
    strip_leading_title_line,
    strip_title_update_marker,
)


def test_single_newline_markerless_file_is_chunked():
    """A marker-less file that separates paragraphs with a single newline
    (a common .txt export style) must still chunk into multiple chapters
    instead of collapsing into one giant chapter. Regression test for the
    blank-line-only split in _chunk_fallback."""
    para = "这是一个测试段落，用来凑够字数。" * 20
    text = "\n".join([para] * 40)
    assert len(text) > CHUNK_SIZE * 2

    chapters = parse_chapters(text)

    assert len(chapters) > 1, "single-newline file collapsed into one chapter"
    # No content dropped — every paragraph survives the round-trip.
    assert sum(c.original_text.count(para) for c in chapters) == 40


def test_blank_line_markerless_file_still_chunks():
    """Blank-line-separated paragraphs continue to chunk as before."""
    para = "另一个测试段落，内容足够长。" * 20
    text = "\n\n".join([para] * 40)

    chapters = parse_chapters(text)

    assert len(chapters) > 1


def test_marker_based_split_unaffected():
    """Explicit chapter markers still drive the split; the fallback is only
    used when no markers are found. Bodies are kept above MIN_CHAPTER_CHARS so
    the too-short-chapter merge doesn't fold them together."""
    body1 = "正文内容一。" * 60
    body2 = "正文内容二。" * 60
    text = f"第一章 开始\n{body1}\n\n第二章 继续\n{body2}"

    chapters = parse_chapters(text)

    assert len(chapters) == 2
    assert chapters[0].chapter_num == 1
    assert chapters[1].chapter_num == 2


def test_empty_input_returns_no_chapters():
    assert parse_chapters("") == []
    assert parse_chapters("   \n  \n ") == []


# --- heading-derived chapter numbering --------------------------------------


def test_extract_heading_number_arabic_and_chinese():
    assert extract_heading_number("第298章 越级偷袭，所向披靡！") == 298
    assert extract_heading_number("第三章 开始") == 3
    assert extract_heading_number("第一百二十八回 大战") == 128
    assert extract_heading_number("Chapter 42: The End") == 42
    assert extract_heading_number("CH 7") == 7
    # Numberless headings and non-headings yield None.
    assert extract_heading_number("序章") is None
    assert extract_heading_number("番外篇") is None
    assert extract_heading_number("prologue") is None
    assert extract_heading_number(None) is None


def _pc(printed: int | None) -> ParsedChapter:
    return ParsedChapter(
        chapter_num=0, title_zh=None, original_text="", printed_num=printed
    )


def test_reconcile_anchors_to_printed_numbers():
    chs = [_pc(296), _pc(297), _pc(298)]
    reconcile_chapter_numbers(chs)
    assert [c.chapter_num for c in chs] == [296, 297, 298]


def test_reconcile_handles_duplicate_and_out_of_order():
    # Duplicate (5) and out-of-order (4) fall to last+1; 9 > 7 is honored.
    chs = [_pc(5), _pc(5), _pc(4), _pc(9)]
    reconcile_chapter_numbers(chs)
    assert [c.chapter_num for c in chs] == [5, 6, 7, 9]


def test_reconcile_numbers_leading_prologue_backward():
    chs = [_pc(None), _pc(1), _pc(2)]
    reconcile_chapter_numbers(chs)
    assert [c.chapter_num for c in chs] == [0, 1, 2]


def test_reconcile_numberless_falls_to_sequential():
    chs = [_pc(None), _pc(None), _pc(None)]
    reconcile_chapter_numbers(chs)
    assert [c.chapter_num for c in chs] == [1, 2, 3]


def test_chapter_number_read_from_heading():
    """A slice of a novel starting at 第296章 numbers chapters 296-298 — not
    1-3 by position. Regression for the published-as-Chapter-304 bug."""
    body = "正文内容。" * 80
    text = f"第296章 甲\n{body}\n\n第297章 乙\n{body}\n\n第298章 丙\n{body}"
    chapters = parse_chapters(text)
    assert [c.chapter_num for c in chapters] == [296, 297, 298]


def test_volume_divider_does_not_consume_a_chapter_number():
    """第N卷 dividers are stripped — they no longer shift later chapters."""
    body = "正文内容。" * 80
    text = (
        f"第一卷 风起云涌\n"
        f"第296章 甲\n{body}\n\n"
        f"第297章 乙\n{body}\n\n"
        f"第298章 丙\n{body}"
    )
    chapters = parse_chapters(text)
    assert [c.chapter_num for c in chapters] == [296, 297, 298]
    assert all("第一卷" not in (c.title_zh or "") for c in chapters)
    assert all("第一卷" not in c.original_text for c in chapters)


# --- title em-dash sanitizer ------------------------------------------------


def test_sanitize_title_single_emdash_to_colon():
    """The most common case: descriptive title with one em-dash separator
    becomes a colon — the canonical title/subtitle form."""
    assert sanitize_title_punctuation("Discovery — A Hidden Truth") == "Discovery: A Hidden Truth"


def test_sanitize_title_multiple_dashes_first_colon_rest_comma():
    """First dash → colon; subsequent dashes → commas. The colon does the
    heavy lifting once; further dashes are list-like."""
    assert (
        sanitize_title_punctuation("Eternal Radiance — Treasure-Light — Grotto-Heaven")
        == "Eternal Radiance: Treasure-Light, Grotto-Heaven"
    )


def test_sanitize_title_en_dash_and_horizontal_bar():
    """All three long-dash glyphs (em U+2014, en U+2013, horizontal bar
    U+2015) get the same treatment."""
    assert sanitize_title_punctuation("X – Y") == "X: Y"
    assert sanitize_title_punctuation("X ― Y") == "X: Y"


def test_sanitize_title_preserves_ascii_hyphen():
    """ASCII hyphens in compound words ("Treasure-Light") are NOT
    sanitized — only long-dash glyphs are."""
    assert sanitize_title_punctuation("Treasure-Light Grotto-Heaven") == "Treasure-Light Grotto-Heaven"


def test_sanitize_title_idempotent_on_clean_input():
    """Re-running on already-clean text is a no-op."""
    clean = "Eternal Radiance: Treasure-Light Grotto-Heaven"
    assert sanitize_title_punctuation(clean) == clean
    assert sanitize_title_punctuation(sanitize_title_punctuation(clean)) == clean


def test_sanitize_title_idempotent_on_already_sanitized():
    """Sanitize -> Sanitize == Sanitize. No drift on a second pass."""
    once = sanitize_title_punctuation("Discovery — A Hidden Truth")
    twice = sanitize_title_punctuation(once)
    assert once == twice


def test_sanitize_title_strips_leading_separator_from_dash_at_start():
    """A dash at the very start would leave a leading "`: `" — strip it,
    a title starting with a separator is not what we want."""
    assert sanitize_title_punctuation("— Eternal Radiance") == "Eternal Radiance"


def test_sanitize_title_empty_and_blank():
    assert sanitize_title_punctuation("") == ""
    assert sanitize_title_punctuation("   ") == ""


def test_sanitize_title_collapses_double_spaces():
    """Sloppy whitespace around the dash is cleaned up."""
    assert sanitize_title_punctuation("A   —   B") == "A: B"


# --- normalize_title_en composes the sanitizer ------------------------------


def test_normalize_title_strips_prefix_dash_and_internal_dash():
    """A model emits 'Chapter 298 — Eternal Radiance — Treasure-Light'. The
    prefix dash is stripped by _TITLE_PREFIX_RE; the descriptive dash is
    sanitized to a colon."""
    out = normalize_title_en("Chapter 298 — Eternal Radiance — Treasure-Light", 298)
    assert out == "Chapter 298: Eternal Radiance: Treasure-Light"


def test_normalize_title_descriptive_emdash_only():
    """The translator emits only the descriptive title (as instructed); a
    surviving descriptive em-dash becomes a colon in the canonical form."""
    out = normalize_title_en("Eternal Radiance — Treasure-Light Grotto-Heaven", 298)
    assert out == "Chapter 298: Eternal Radiance: Treasure-Light Grotto-Heaven"


def test_normalize_title_canonical_no_emdash_unchanged():
    """A title with no long-dash glyphs round-trips through both helpers
    untouched (except the canonical Chapter-N prefix)."""
    out = normalize_title_en("Eternal Radiance Treasure-Light Grotto-Heaven", 298)
    assert out == "Chapter 298: Eternal Radiance Treasure-Light Grotto-Heaven"


def test_normalize_title_empty_yields_bare_chapter():
    """No descriptive title (or "untitled") yields the bare 'Chapter N'."""
    assert normalize_title_en(None, 5) == "Chapter 5"
    assert normalize_title_en("", 5) == "Chapter 5"
    assert normalize_title_en("(untitled)", 5) == "Chapter 5"


def test_normalize_title_dash_only_yields_bare_chapter():
    """A title that is only a dash glyph sanitizes to empty and falls back
    to the bare chapter form, not 'Chapter 5: '."""
    assert normalize_title_en("—", 5) == "Chapter 5"
    assert normalize_title_en(" — ", 5) == "Chapter 5"


# --- author update-count / vote-begging markers ------------------------------


def test_strip_title_update_marker_observed_patterns():
    """Every marker shape observed in live novel 2 strips cleanly."""
    cases = [
        ("第392章 惊变！（第四更！）", "第392章 惊变！"),
        ("第405章 我特么又回来啦！（第五更）", "第405章 我特么又回来啦！"),
        ("第492章 天下第一真君！（四更）", "第492章 天下第一真君！"),
        ("第619章 天府之秘，道主尸身！（五更）", "第619章 天府之秘，道主尸身！"),
        ("第187章 你才是真的畜生啊！（补更！）", "第187章 你才是真的畜生啊！"),
        ("第522章 干！别怂！（补更）", "第522章 干！别怂！"),
        ("第1146章 胜利的一小步（下午五点还有两更）", "第1146章 胜利的一小步"),
        ("第1322章 赌斗（晚上十点还有两更）", "第1322章 赌斗"),
        ("第10章 标题（求月票！）", "第10章 标题"),
        ("第11章 标题（求订阅）", "第11章 标题"),
        ("第12章 标题（求推荐票）", "第12章 标题"),
        ("第13章 标题（加更）", "第13章 标题"),
        ("第14章 标题(第3更)", "第14章 标题"),  # halfwidth parens
    ]
    for raw, want in cases:
        assert strip_title_update_marker(raw) == want, raw


def test_strip_title_update_marker_keeps_legit_parentheticals():
    """Part markers, numbering, and prose containing a bare 更 are not noise."""
    cases = [
        "第50章 大战（上）",
        "第51章 大战（一）",
        "第52章 更上一层楼",
        "第53章 标题（更上一层楼）",
        "第54章 平平无奇",
    ]
    for raw in cases:
        assert strip_title_update_marker(raw) == raw, raw


def test_strip_title_update_marker_none_and_empty():
    assert strip_title_update_marker(None) == ""
    assert strip_title_update_marker("") == ""


def test_strip_title_update_marker_unclosed_trailing_parenthetical():
    """A rescraped source title can arrive truncated mid-marker (live ch426:
    （晚上还有三更 with no closing ）). The unclosed trailing form still strips."""
    cases = [
        ("第426章 不杀剑，终杀一人！（晚上还有三更", "第426章 不杀剑，终杀一人！"),
        ("第99章 标题（求月票", "第99章 标题"),
        ("第98章 标题(第3更", "第98章 标题"),
    ]
    for raw, want in cases:
        assert strip_title_update_marker(raw) == want, raw


def test_strip_title_update_marker_keeps_unclosed_non_marker():
    """A truncated parenthetical WITHOUT marker content is not noise."""
    assert strip_title_update_marker("第50章 大战（上") == "第50章 大战（上"


def test_normalize_title_backstop_fires_on_unclosed_zh_marker():
    """The zh gate must also recognize a truncated, unclosed marker
    parenthetical, so the model's translated marker still gets dropped."""
    out = normalize_title_en(
        "The No-Killing Sword Finally Kills Someone! (Three more chapters tonight)",
        426,
        title_zh="第426章 不杀剑，终杀一人！（晚上还有三更",
    )
    assert out == "Chapter 426: The No-Killing Sword Finally Kills Someone!"


def test_strip_heading_update_marker_cleans_heading_line():
    body = "第392章 惊变！（第四更！）\n\n【离恨天】外。\n\n正文继续。"
    out = strip_heading_update_marker(body)
    assert out.split("\n")[0] == "第392章 惊变！"
    assert "【离恨天】外。" in out
    assert "第四更" not in out


def test_strip_heading_update_marker_leaves_prose_first_line():
    """A body whose first line is prose (no heading) is untouched, even if a
    marker-like parenthetical appears in it."""
    body = "他说（第四更！）这句话。\n\n正文。"
    assert strip_heading_update_marker(body) == body


def test_normalize_title_zh_gated_backstop_strips_trailing_parenthetical():
    out = normalize_title_en(
        "Sudden Turn! (Fourth Update!)", 392,
        title_zh="第392章 惊变！（第四更！）",
    )
    assert out == "Chapter 392: Sudden Turn!"


def test_normalize_title_backstop_inert_without_zh_marker():
    """No marker in the zh title: a trailing parenthetical is a legitimate
    subtitle and stays."""
    out = normalize_title_en("The Duel (Part One)", 392, title_zh="第392章 决斗（上）")
    assert out == "Chapter 392: The Duel (Part One)"
    out2 = normalize_title_en("The Duel (Part One)", 392)
    assert out2 == "Chapter 392: The Duel (Part One)"


def test_leading_prologue_keeps_real_chapter_numbers():
    body = "正文内容。" * 80
    text = f"序章 缘起\n{body}\n\n第1章 启程\n{body}\n\n第2章 历练\n{body}"
    chapters = parse_chapters(text)
    assert [c.chapter_num for c in chapters] == [0, 1, 2]


def test_strip_leading_title_line_prefix_match():
    """The Chapter-N prefix case: the body's first line begins with
    "Chapter 300:". Stripped regardless of how closely it matches title_en
    (the model can reword the echo)."""
    body = (
        "Chapter 300: A Terrifying Scene, Encountering Soaring Firmament Again!\n"
        "\n"
        "Lü Yang stepped onto the platform, breath held."
    )
    title_en = "A Chilling Sight — Encountering Soaring Firmament Again!"
    cleaned, n = strip_leading_title_line(body, title_en)
    assert n == 1
    assert cleaned.startswith("Lü Yang stepped onto the platform")
    assert "Chapter 300" not in cleaned


def test_strip_leading_title_line_no_prefix_loose_match():
    """No "Chapter N:" prefix on the echoed line — falls to the token-Jaccard
    + length-ratio gate. Same wording as title_en should strip."""
    body = (
        "A Chilling Sight — Encountering Soaring Firmament Again!\n"
        "\n"
        "The morning mist clung to the peaks."
    )
    title_en = "A Chilling Sight — Encountering Soaring Firmament Again!"
    cleaned, n = strip_leading_title_line(body, title_en)
    assert n == 1
    assert cleaned.startswith("The morning mist")


def test_strip_leading_title_line_idempotent_on_clean_body():
    """A body whose first line is real narrative prose is left untouched."""
    body = (
        "Lü Yang stepped onto the platform, breath held. He had not seen "
        "Soaring Firmament since the trial of three years past, and the "
        "memory still tightened something in his chest."
    )
    title_en = "A Chilling Sight — Encountering Soaring Firmament Again!"
    cleaned, n = strip_leading_title_line(body, title_en)
    assert n == 0
    assert cleaned == body


def test_strip_leading_title_line_short_opener_not_misstripped():
    """A short opening line that shares only a token or two with the title
    (length ratio well under 0.5) must NOT be stripped."""
    body = (
        "Sword Master!\n"
        "\n"
        "The cry rang out across the courtyard."
    )
    title_en = "The Sword Master Returns to the Northern Peak"
    cleaned, n = strip_leading_title_line(body, title_en)
    assert n == 0
    assert cleaned.startswith("Sword Master!")


def test_strip_leading_title_line_handles_leading_blank_lines():
    """Leading blank lines before the title echo are tolerated."""
    body = (
        "\n\n"
        "Chapter 12: Mountain Mist\n"
        "\n"
        "He woke before dawn."
    )
    cleaned, n = strip_leading_title_line(body, "Mountain Mist")
    assert n == 1
    assert cleaned.startswith("He woke before dawn")


def test_strip_leading_title_line_empty_body():
    """Empty / whitespace input passes through with no strip."""
    cleaned, n = strip_leading_title_line("", "Title")
    assert (cleaned, n) == ("", 0)
    cleaned, n = strip_leading_title_line("   \n\n  ", "Title")
    assert n == 0
