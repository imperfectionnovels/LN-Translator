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
    strip_leading_title_line,
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
