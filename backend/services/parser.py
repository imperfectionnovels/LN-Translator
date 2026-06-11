"""Parse a blob of Chinese text into chapters.

Strategy: try matching common chapter-heading patterns. If none match (or only
one does), fall back to fixed-size chunks of ~CHUNK_SIZE characters split at
paragraph boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TypedDict

CHUNK_SIZE = 4000
MIN_CHAPTER_CHARS = 200

# Every internal whitespace class uses `[ \t]` (not `\s`) so the pattern can
# never consume a newline. Without that constraint, `\s*` after the chapter
# unit would eat blank lines and `[^\n]*` would then slurp the first body line
# into `m.group(0)`, dropping the first paragraph of every newline-titled
# chapter from the body and baking it into title_zh.
CHAPTER_PATTERNS = [
    re.compile(
        r"^[ \t]*第[ \t]*[\d零〇一二三四五六七八九十百千万两]+[ \t]*[章回节][ \t]*[:：．\.]?[ \t]*[^\n]*",
        re.MULTILINE,
    ),
    re.compile(r"^[ \t]*Chapter[ \t]+\d+\b[^\n]*", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^[ \t]*CH[ \t]*\d+\b[^\n]*", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^[ \t]*(?:楔子|序章|序言|前言|引子|番外)[^\n]*", re.MULTILINE),
]

# Volume / part dividers (第N卷 / 第N篇 / 第N部 / 第N集). These are NOT chapters:
# the chapter unit class in CHAPTER_PATTERNS[0] above was narrowed to [章回节]
# so a volume divider no longer consumes a chapter slot. `parse_chapters`
# strips matching lines before marker detection so the divider text neither
# becomes a chapter nor leaks into a following chapter's body.
_VOLUME_RE = re.compile(
    r"^[ \t]*第[ \t]*[\d零〇一二三四五六七八九十百千万两]+[ \t]*[卷篇部集][ \t]*[:：．\.]?[ \t]*[^\n]*$",
    re.MULTILINE,
)


@dataclass
class ParsedChapter:
    chapter_num: int
    title_zh: str | None
    original_text: str
    # The number printed in the source heading (第298章 → 298), or None for a
    # numberless heading (序章/楔子/番外) or a chunk-fallback slice. The
    # authoritative ordering key is `chapter_num`, assigned by
    # reconcile_chapter_numbers; `printed_num` is the raw signal it consumes.
    printed_num: int | None = None


def _find_markers(text: str) -> list[tuple[int, int, str]]:
    """Return list of (start, end, title_line) for chapter-heading matches.

    De-duplicate by start-of-line: if two patterns both match the same heading
    line (e.g. pattern 1 and pattern 4 firing on `第一章 序章 …`), keep the
    first. Comparing by line rather than a fixed char-distance window means
    legitimately adjacent chapter headings (`第一章\n第二章`) are both kept —
    the previous 5-char window dropped the second every time."""
    markers: list[tuple[int, int, str]] = []
    for pat in CHAPTER_PATTERNS:
        for m in pat.finditer(text):
            title = m.group(0).strip()
            markers.append((m.start(), m.end(), title))
    markers.sort(key=lambda x: x[0])
    seen_lines: set[int] = set()
    deduped: list[tuple[int, int, str]] = []
    for marker in markers:
        nl = text.rfind("\n", 0, marker[0])
        line_start = nl + 1 if nl >= 0 else 0
        if line_start in seen_lines:
            continue
        seen_lines.add(line_start)
        deduped.append(marker)
    return deduped


def split_leading_heading(text: str) -> tuple[str | None, str]:
    """If `text` begins with a chapter-heading line (第N章 …, Chapter N …,
    楔子 …), return (heading, body-without-heading). Otherwise (None, text).

    Used by bulk import, where each uploaded file is one chapter and its real
    title usually sits on the first line. Only the first non-blank line is
    tested — a 第N章 reference deeper in the body is not a heading. A file with
    no newline at all is left untouched (no heading/body split to make)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    stripped = normalized.lstrip("\n \t")
    newline = stripped.find("\n")
    if newline == -1:
        return None, text
    first_line = stripped[:newline]
    rest = stripped[newline + 1:]
    for pat in CHAPTER_PATTERNS:
        # Each pattern ends with `[^\n]*`, so a match consumes the whole line;
        # a truthy match means the first line is entirely a heading.
        if pat.match(first_line):
            return first_line.strip(), rest.strip()
    return None, text


# Matches a chapter-number prefix a translator may have prepended to a title:
# "Chapter 12", "CH 12", "Ch.12", "第十二章" — plus an optional separator.
_TITLE_PREFIX_RE = re.compile(
    r"""^\s*["'“”]?\s*
        (?:
            (?:chapter|ch)\.?\s*\d+
          | 第\s*[\d零〇一二三四五六七八九十百千万两]+\s*[章回节卷篇]
        )
        \s*[:：.\-–—、,]?\s*
    """,
    re.IGNORECASE | re.VERBOSE,
)


# Author postscript markers web-novel authors append to chapter headings:
# update-count notes (（第四更！）/（四更）/（补更）/（下午五点还有两更）) and
# vote-begging asides (求月票 / 求订阅 / 求推荐票). They are upload metadata,
# not title content — strip them from prompt inputs so the model never
# translates one into the English title. Anchored to a parenthetical whose
# content carries one of the marker shapes; a bare 更 in real title prose
# (更上一层楼) never matches.
_TITLE_NOISE_INNER = (
    r"(?:第?[\d一二三四五六七八九十两]+更"  # 第四更 / 四更 / 还有两更
    r"|[补加]更"                            # 补更 / 加更
    r"|求[^（）()]{0,6}?(?:票|订阅)"        # 求月票 / 求票 / 求订阅 / 求推荐票
    r"|月票)"
)
_TITLE_NOISE_RE = re.compile(
    r"[（(][^（）()]*" + _TITLE_NOISE_INNER + r"[^（）()]*[）)]"
)
# Truncated variant: a rescraped source title can arrive cut off mid-marker
# (live ch426: 第426章 …！（晚上还有三更 with no closing ）). End-anchored so an
# unclosed marker parenthetical at the tail still strips; an unclosed
# NON-marker parenthetical (第50章 大战（上) carries no marker shape and never
# matches.
_TITLE_NOISE_OPEN_RE = re.compile(
    r"[（(][^（）()]*" + _TITLE_NOISE_INNER + r"[^（）()]*$"
)


def strip_title_update_marker(title: str | None) -> str:
    """Remove author update-count / vote-begging parentheticals from a chapter
    heading: ``第392章 惊变！（第四更！）`` → ``第392章 惊变！``. Handles both
    the closed form and a truncated, unclosed trailing form. Returns ``""``
    for None. Idempotent; a heading without a marker passes through unchanged."""
    if not title:
        return ""
    out = _TITLE_NOISE_RE.sub("", title)
    out = _TITLE_NOISE_OPEN_RE.sub("", out)
    return re.sub(r"\s{2,}", " ", out).strip()


def strip_heading_update_marker(body: str) -> str:
    """Apply ``strip_title_update_marker`` to the leading chapter-heading line
    of ``body``, if there is one. The model reads the heading from the body
    too, so cleaning only the CHAPTER TITLE prompt line would leave the marker
    visible there. Non-heading first lines (ordinary prose) are untouched."""
    if not body:
        return body
    lines = body.split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return body
    first = lines[idx].strip()
    if not _TITLE_PREFIX_RE.match(first):
        return body
    stripped = strip_title_update_marker(first)
    if stripped == first:
        return body
    lines[idx] = stripped
    return "\n".join(lines)


# Long-dash glyphs the model occasionally emits inside a descriptive chapter
# title: U+2014 em-dash, U+2013 en-dash, U+2015 horizontal bar. ASCII hyphen
# (`-`) is deliberately excluded — compound words like "Treasure-Light" and
# "Grotto-Heaven" are legitimate.
_TITLE_DASH_RE = re.compile(r"\s*[—–―]+\s*")


def sanitize_title_punctuation(title: str) -> str:
    """Replace em-dash / en-dash / horizontal-bar runs inside a descriptive
    chapter title with title-appropriate punctuation.

    The body em-dash enforcer in ``text_fixups.enforce_em_dash`` chooses
    ``. `` or ``, `` based on the case of the following character. For prose
    that works; in a title (where every word is Title-Cased) it produces
    ``"Eternal Radiance. Treasure-Light Grotto-Heaven"`` — rule-compliant
    but ugly as an H1. This helper is title-aware: the first dash run becomes
    ``: `` (the canonical title/subtitle separator), any subsequent runs
    become ``, ``. ASCII hyphens are out of scope. Idempotent."""
    if not title:
        return title
    out: list[str] = []
    cursor = 0
    for i, m in enumerate(_TITLE_DASH_RE.finditer(title)):
        out.append(title[cursor:m.start()])
        out.append(": " if i == 0 else ", ")
        cursor = m.end()
    out.append(title[cursor:])
    result = "".join(out)
    # Collapse any double spaces from sloppy surrounding whitespace and trim.
    result = re.sub(r"\s{2,}", " ", result).strip()
    # If a dash sat at the very start of the title, the replacement leaves a
    # leading "`: `" or "`, `". Strip — a title that starts with the separator
    # is not what we want.
    result = re.sub(r"^[:,]\s*", "", result)
    return result


def normalize_title_en(
    raw: str | None, chapter_num: int, title_zh: str | None = None
) -> str:
    """Return a canonical ``Chapter {n}: {title}`` string.

    The translator is told to emit only the descriptive title, but models
    drift — some prepend "Chapter N", some use a dash, some a colon, some
    nothing. This strips whatever number-prefix the model emitted and
    recomposes the title from the authoritative ``chapter_num`` so every
    chapter in a novel is formatted identically. A title that strips to
    nothing (or "(untitled)") yields the bare ``Chapter {n}``.

    When ``title_zh`` is supplied and carries an author update marker
    (（第四更！）, 求月票 — see ``_TITLE_NOISE_RE``), a trailing parenthetical
    in the model's title is dropped: the EN wording of a translated marker
    varies too much to pattern-match, but the zh gate makes the strip
    zero-false-positive on legitimate parenthetical subtitles.

    Em-dash / en-dash runs that survive into the descriptive title (where
    the body em-dash enforcer never runs) are normalized via
    ``sanitize_title_punctuation`` so the H1 never carries a dash glyph.

    Kept here, in the dependency-light parser module, so the title-backfill
    script can reuse it without importing the LLM-backend stack."""
    title = (raw or "").strip()
    # Strip a model-emitted chapter-number prefix; loop so "Chapter 2 - 第二章"
    # collapses fully.
    for _ in range(2):
        stripped = _TITLE_PREFIX_RE.sub("", title, count=1)
        if stripped == title:
            break
        title = stripped.strip()
    title = title.strip("\"'“”").strip()
    if title_zh and (
        _TITLE_NOISE_RE.search(title_zh) or _TITLE_NOISE_OPEN_RE.search(title_zh)
    ):
        title = re.sub(r"\s*[（(][^（）()]*[）)]\s*$", "", title).strip()
    title = sanitize_title_punctuation(title)
    if not title or title.lower() in ("(untitled)", "untitled"):
        return f"Chapter {chapter_num}"
    return f"Chapter {chapter_num}: {title}"


# Tokenizer for the loose title-vs-line comparison in strip_leading_title_line.
# Captures alphanumeric runs (case-insensitive at compare time) and individual
# CJK characters; punctuation and whitespace are discarded. Same shape as the
# review-diff tokenizer in queue.py — token-set comparison is robust to small
# wording drifts (the model rewords the echoed title) without false-positiving
# on word order.
_TITLE_TOKEN_RE = re.compile(r"[A-Za-z0-9']+|[㐀-鿿豈-﫿]")

# Upper bound on a line we will consider stripping as a duplicated title.
# Real opening paragraphs in cultivation novels run well past this; chapter
# titles cap out around 80 chars even with embedded glossary terms. The gate
# is belt-and-suspenders: an exact prefix match (case 1) is already
# zero-false-positive, but the fuzzy match (case 2) needs a length guard.
_LEADING_TITLE_MAX_CHARS = 200


def _title_token_jaccard(a: str, b: str) -> float:
    """Token-set Jaccard similarity between two short strings."""
    ta = {t.lower() for t in _TITLE_TOKEN_RE.findall(a)}
    tb = {t.lower() for t in _TITLE_TOKEN_RE.findall(b)}
    if not ta or not tb:
        return 0.0
    union = ta | tb
    return len(ta & tb) / len(union)


def strip_leading_title_line(
    body: str, title_en: str | None = None
) -> tuple[str, int]:
    """Drop a duplicated chapter-title line from the top of `body`.

    Some models echo the chapter title at the top of `translated_text` even
    though it is also being returned in the structured `title_en` field. The
    reader then renders both — once under the H1 and once at the top of the
    body — and the em-dash / bracket guardrails (which only run on the body)
    can leave the two copies cosmetically different. The canonical title lives
    on `title_en`; stripping the body copy makes the rendered chapter match.

    Two detection cases:
    - **Prefix match**: the first non-blank line begins with a
      `Chapter N` / `CH N` / `第N章` token (matched by `_TITLE_PREFIX_RE`).
      Narrative prose effectively never starts with these, so this is a
      zero-false-positive strip.
    - **Loose match against `title_en`**: the first non-blank line is short,
      has a length within 0.5×–2.0× the descriptive title length, and shares
      ≥60 % of its tokens with the descriptive title. Catches the case where
      the model echoes the title without the "Chapter N:" prefix.

    Returns ``(cleaned_body, n_stripped)`` — n_stripped is 0 or 1. Idempotent:
    a body with no title echo passes through unchanged.
    """
    if not body:
        return body, 0
    lines = body.split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return body, 0
    first = lines[idx].strip()
    # Length gate guards both cases — a long line is a paragraph, not a title.
    if len(first) > _LEADING_TITLE_MAX_CHARS:
        return body, 0

    matched = False
    # Case 1: explicit Chapter-N / 第N章 prefix.
    if _TITLE_PREFIX_RE.match(first):
        matched = True
    # Case 2: looks like the descriptive title (no prefix on the echoed line).
    elif title_en:
        desc = _TITLE_PREFIX_RE.sub("", title_en, count=1).strip()
        desc = desc.strip("\"'“”").strip()
        if desc:
            # Length-ratio gate first — cheap, eliminates short stubs that
            # share a token with the title by coincidence ("Sword Master!"
            # vs "The Sword Master Returns" → ratio 0.3, skipped).
            ratio = len(first) / max(1, len(desc))
            if 0.5 <= ratio <= 2.0 and _title_token_jaccard(first, desc) >= 0.6:
                matched = True

    if not matched:
        return body, 0

    remaining = "\n".join(lines[idx + 1:]).lstrip("\n")
    return remaining, 1


# Chinese-numeral → int. Covers the 0–99999 range that chapter headings use.
_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _cn_to_int(s: str) -> int | None:
    """Convert an Arabic- or Chinese-numeral string to int. Returns None if the
    string holds a character that is neither digit nor recognized numeral.

    Standard place-value walk: digits set the pending value, units flush it.
    A bare unit ("十二" = 12) means an implicit leading 1."""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total = 0
    section = 0
    current = 0
    for ch in s:
        if ch in _CN_DIGIT:
            current = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if unit == 10000:
                total = (total + section + current) * unit
                section = 0
            else:
                section += (current or 1) * unit
            current = 0
        else:
            return None
    return total + section + current


_HEADING_NUM_RE = re.compile(
    r"第[ \t]*([\d零〇一二三四五六七八九十百千万两]+)[ \t]*[章回节]"
)
_EN_HEADING_NUM_RE = re.compile(r"\b(?:chapter|ch)\b\.?[ \t]*(\d+)", re.IGNORECASE)


def extract_heading_number(heading: str | None) -> int | None:
    """Pull the printed chapter number out of a heading line.

    Handles `第298章 …` (Arabic or Chinese numerals) and `Chapter 298 …` /
    `CH 298 …`. Returns None for a numberless heading (序章/楔子/番外), a
    filename-derived title, or anything unparseable."""
    if not heading:
        return None
    m = _HEADING_NUM_RE.search(heading)
    if m:
        return _cn_to_int(m.group(1))
    m = _EN_HEADING_NUM_RE.search(heading)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def reconcile_chapter_numbers(chapters: list[ParsedChapter]) -> None:
    """Assign each chapter's `chapter_num` from its `printed_num`, anchored to
    the source but kept strictly increasing and unique so
    UNIQUE(novel_id, chapter_num) always holds.

    A printed number is used when it is strictly greater than the last
    assigned number; otherwise (numberless prologue, duplicate, or
    out-of-order heading) the chapter falls to last+1. Leading numberless
    chapters before the first numbered one are counted backward from it so the
    real chapters keep their printed numbers (the count may reach 0 or go
    negative when several prologues precede chapter 1 — rare, and accepted)."""
    if not chapters:
        return
    first_numbered = next(
        (i for i, ch in enumerate(chapters) if ch.printed_num is not None), None
    )
    last = 0
    for i, ch in enumerate(chapters):
        if first_numbered is not None and i < first_numbered:
            anchor = chapters[first_numbered].printed_num
            assert anchor is not None  # first_numbered points at a numbered ch
            ch.chapter_num = anchor - (first_numbered - i)
            last = ch.chapter_num
            continue
        pn = ch.printed_num
        ch.chapter_num = pn if (pn is not None and pn > last) else last + 1
        last = ch.chapter_num


# Author-note / update-announcement vocabulary. In a bulk import each uploaded
# file becomes one chapter, so an author's update post (求月票, 请假, 更新调整 …)
# would otherwise take a chapter number and shift every chapter after it — this
# is exactly what happened to novel 3 (a constant +6 offset from six such posts
# imported as chapters). The token set is deliberately narrow: it only ever runs
# against a heading-less block, and these words are vanishingly rare in
# narrative prose.
NON_CHAPTER_MAX_CHARS = 2500
_NON_CHAPTER_RE = re.compile(
    r"求月票|求推荐|求订阅|求收藏|推荐票|月票|抽奖|中奖|万订|更新|"
    r"加更|补更|欠更|欠一[章更]|停更|断更|请[^\n]{0,3}假|卡文|休息|"
    r"打赏|白银盟|盟主|上架|完结|正文完|单章说明|公告|感言|感想|本月总结|"
    r"感谢大家|感谢各位|[一二三四五六七八九十两]+更(?!半|天黑|鼓|夫)|"
    r"新春|元旦|周年|推书"
)


def has_author_note_markers(text: str) -> bool:
    """True if the start of `text` carries author update-post vocabulary
    (求月票, 请假, 更新调整, 月票抽奖 …). The heading-agnostic core of
    `is_non_chapter_block` — also used to spot an author note hiding behind a
    numberless heading (`番外+活动预告！`)."""
    if not text:
        return False
    return _NON_CHAPTER_RE.search(text.strip()[:400]) is not None


def is_non_chapter_block(text: str) -> bool:
    """True if `text` is an author's update/announcement post rather than a
    story chapter — e.g. `求月票！`, `请假一天`, `今天五更放到晚上八点`.

    Used by bulk import: each uploaded file is one chapter, so an author-note
    file would otherwise become a numbered chapter and shift every chapter
    after it. Deliberately conservative — fires only on a short block that
    carries no chapter heading and matches announcement vocabulary, so it
    cannot swallow a real chapter."""
    if not text:
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > NON_CHAPTER_MAX_CHARS:
        return False
    first_line = stripped.split("\n", 1)[0]
    for pat in CHAPTER_PATTERNS:
        if pat.match(first_line):
            return False
    return has_author_note_markers(stripped)


def _chunk_fallback(text: str) -> list[ParsedChapter]:
    text = text.strip()
    paragraphs = re.split(r"\n\s*\n", text)
    # Some .txt exports separate paragraphs with a single newline, not a blank
    # line. The blank-line split then returns the whole file as one giant
    # "paragraph", so the size-based chunking never triggers and the entire
    # novel collapses into one chapter. Fall back to single-newline splitting.
    if len(paragraphs) == 1 and "\n" in text:
        paragraphs = text.split("\n")
    chunks: list[ParsedChapter] = []
    buf: list[str] = []
    size = 0
    num = 1
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        buf.append(p)
        size += len(p)
        if size >= CHUNK_SIZE:
            chunks.append(
                ParsedChapter(
                    chapter_num=num,
                    title_zh=None,
                    original_text="\n\n".join(buf).strip(),
                )
            )
            num += 1
            buf = []
            size = 0
    if buf:
        chunks.append(
            ParsedChapter(
                chapter_num=num,
                title_zh=None,
                original_text="\n\n".join(buf).strip(),
            )
        )
    return chunks


class OcrIssueReport(TypedDict):
    """Fixed shape returned by `detect_ocr_issues`, mirrored by the
    `OcrIssues` Pydantic model the saturation route serializes. Capturing it
    here makes the producer's contract type-checkable at the source rather
    than only at the distant response model."""
    score: int
    issues: list[str]
    flagged: bool


def detect_ocr_issues(text: str) -> OcrIssueReport:
    """Score a CN chapter for OCR-corruption indicators.

    Returns `{score: int 0..100, issues: list[str], flagged: bool}`. Score is
    a coarse heuristic — anything >= 25 is worth user review. Issues are
    short labels the UI can render as chips.

    Heuristics (each fires on cheap regex checks, no LLM):
    - missing terminal punctuation: long runs of CJK with no 。！？ at the
      end of a paragraph
    - repeated identical lines: same line appearing >= 3 times (a common
      OCR-buffer-flush artifact)
    - stray Latin runs: long Latin-only sequences inside an otherwise CJK
      text (page-number headers / footers that leaked into the body)
    - excessive whitespace: paragraphs that are mostly whitespace
    - replacement-char density: U+FFFD count > 5

    Returns blank `flagged=False` payload on empty input."""
    if not text or len(text) < 200:
        return {"score": 0, "issues": [], "flagged": False}

    issues: list[str] = []
    score = 0

    # 1. Missing terminal punctuation on long paragraphs.
    long_unterminated = 0
    for para in text.split("\n\n"):
        p = para.strip()
        if len(p) < 200:
            continue
        if not re.search(r"[。！？\.!?…\"」']\s*$", p):
            long_unterminated += 1
    if long_unterminated >= 2:
        issues.append(f"{long_unterminated} long paragraphs without terminal punctuation")
        score += min(30, long_unterminated * 6)

    # 2. Repeated identical non-trivial lines.
    line_counts: dict[str, int] = {}
    for line in text.split("\n"):
        s = line.strip()
        if len(s) < 8:
            continue
        line_counts[s] = line_counts.get(s, 0) + 1
    repeats = sum(1 for c in line_counts.values() if c >= 3)
    if repeats:
        issues.append(f"{repeats} duplicate line{'s' if repeats != 1 else ''} (≥3×)")
        score += min(25, repeats * 5)

    # 3. Stray Latin runs in CN text.
    cjk_chars = len(re.findall(r"[一-鿿]", text))
    latin_runs = re.findall(r"[A-Za-z]{20,}", text)
    if cjk_chars > 500 and latin_runs:
        issues.append(f"{len(latin_runs)} long Latin runs in CN text")
        score += min(15, len(latin_runs) * 5)

    # 4. Replacement-char density.
    fffd = text.count("�")
    if fffd > 5:
        issues.append(f"{fffd} replacement chars (U+FFFD)")
        score += min(40, fffd)

    # 5. Whitespace-heavy paragraphs.
    bad_ws = 0
    for para in text.split("\n\n"):
        if len(para) > 100:
            non_ws = re.sub(r"\s", "", para)
            if len(non_ws) < len(para) * 0.4:
                bad_ws += 1
    if bad_ws:
        issues.append(f"{bad_ws} whitespace-heavy paragraph{'s' if bad_ws != 1 else ''}")
        score += min(15, bad_ws * 5)

    score = min(100, score)
    return {"score": score, "issues": issues, "flagged": score >= 25}


def parse_chapters(text: str) -> list[ParsedChapter]:
    """Split text into chapters. Returns at least one chapter if input is non-empty."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    # Drop volume / part divider lines (第N卷 …) so they neither become
    # chapters nor shift chapter numbering. Subbing to "" leaves a blank line,
    # which is harmless — blank lines are stripped from bodies downstream.
    text = _VOLUME_RE.sub("", text)

    markers = _find_markers(text)
    if not markers:
        return _chunk_fallback(text)

    chapters: list[ParsedChapter] = []
    # `carry` holds heading + body text from too-short slices that had no
    # previous chapter to merge into; it gets prepended to the next chapter's
    # body so we never silently drop source content. Also seeded from any
    # preface text before the first marker so a single-chapter file with a
    # leading preamble doesn't drop the preamble.
    carry: list[str] = []
    preface = text[: markers[0][0]].strip()
    if preface:
        carry.append(preface)
    for idx, (start, end, title) in enumerate(markers):
        body_start = end
        body_end = markers[idx + 1][0] if idx + 1 < len(markers) else len(text)
        body = text[body_start:body_end].strip()
        heading = text[start:end].strip()
        is_last = idx + 1 >= len(markers)

        if len(body) < MIN_CHAPTER_CHARS and not is_last:
            # Too short to stand alone. Prefer merging into the previous
            # chapter (keeps chapter numbering closer to the source); only
            # carry forward when there's no previous chapter yet.
            if chapters:
                prev = chapters[-1]
                joined = "\n\n".join(p for p in (prev.original_text, heading, body) if p)
                chapters[-1] = ParsedChapter(
                    chapter_num=prev.chapter_num,
                    title_zh=prev.title_zh,
                    original_text=joined,
                    printed_num=prev.printed_num,
                )
            else:
                if heading:
                    carry.append(heading)
                if body:
                    carry.append(body)
            continue

        full_body = "\n\n".join(carry + [body]).strip() if carry else body
        chapters.append(
            ParsedChapter(
                chapter_num=len(chapters) + 1,  # placeholder; reconciled below
                title_zh=title,
                original_text=full_body,
                printed_num=extract_heading_number(title),
            )
        )
        carry = []

    if not chapters:
        return _chunk_fallback(text)

    reconcile_chapter_numbers(chapters)
    return chapters
