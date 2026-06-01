"""File-decode + chapter-insertion helpers for the translate routes.

Pure machinery — no FastAPI routing, no multipart parsing. The route layer
(`backend/routes/translate.py`) handles HTTP intake and form parsing, then
calls into here for the byte-level encoding detection and the transactional
novel / chapter inserts.

Three groups live here:

- **Encoding detection** (`_score_decode`, `_decode_with_fallback`,
  `_strip_bom`, `_read_text_file`, `_read_bulk_file`) — scored CJK-density
  decoding that closes the chardet-misdetects-windows-1252 trap.

- **Atomic create** (`_insert_novel_row`, `_insert_parsed_chapters`,
  `_atomic_create_novel`, `_create_novel_and_chapters`) — novel + chapter
  rows committed in a single BEGIN IMMEDIATE transaction so a crash can't
  leave an orphan novel.

- **Atomic append** (`_ensure_novel_exists`, `_max_chapter_num`,
  `_append_parsed_chapters`, `_append_with_offset`) — appends parsed
  chapters either at their printed numbers (when they fit above the existing
  max) or shifted by MAX(chapter_num), inside one write transaction.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass

import aiosqlite
import chardet
from fastapi import HTTPException, UploadFile

from backend.services import lang_detect
from backend.services.parser import ParsedChapter, parse_chapters

logger = logging.getLogger(__name__)


MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB single-file cap; use /bulk for longer raws.
# Generous ceiling: even the longest xianxia rarely exceeds ~6000 chapters, and
# UploadFile spools to disk past 1 MB so memory stays bounded. Starlette's own
# multipart parser caps at 1000 by default — the /bulk endpoints parse the
# form manually with max_files=MAX_BULK_FILES to lift that.
MAX_BULK_FILES = 10000
# Aggregate-bytes cap for one bulk request. Per-file MAX_UPLOAD_BYTES is enforced
# inside each read, but without an aggregate cap a misclick on a directory drag
# of 10000 × 25 MB = 250 GB worth of files could OOM the local process. 512 MB
# is comfortably above any realistic full-novel batch and well under typical
# local-machine RAM.
MAX_BULK_TOTAL_BYTES = 512 * 1024 * 1024

# Decompression-bomb guard for ZIP-container uploads (EPUB / DOCX). MAX_UPLOAD_BYTES
# caps the COMPRESSED bytes only; a ~50 MB archive can declare gigabytes of
# uncompressed content and OOM the local process when ebooklib / python-docx
# inflate it. We read the central directory's declared sizes (no inflation) and
# reject an implausible total or per-entry ratio before handing bytes to the parser.
MAX_DECOMPRESSED_BYTES = 400 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100  # per-entry uncompressed/compressed cap (above ~1 MB)


def _reject_zip_bomb(raw: bytes, filename: str | None) -> None:
    """Raise HTTP 400 if `raw` is a ZIP whose declared uncompressed size looks
    like a decompression bomb. Non-zip / unreadable input is left for the
    format parser to reject with its own error (this is a guard, not a parser)."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            infos = zf.infolist()
    except Exception:
        return  # not a readable zip — let the format parser surface its error
    total = 0
    for info in infos:
        comp = info.compress_size or 1
        if info.file_size > 1024 * 1024 and info.file_size // comp > _MAX_COMPRESSION_RATIO:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"refusing {filename or 'upload'}: entry {info.filename!r} "
                    f"declares an implausible compression ratio "
                    f"({info.file_size} / {comp}) — possible decompression bomb."
                ),
            )
        total += info.file_size
    if total > MAX_DECOMPRESSED_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"refusing {filename or 'upload'}: declared uncompressed size "
                f"{total} bytes exceeds the {MAX_DECOMPRESSED_BYTES} byte limit "
                "— possible decompression bomb."
            ),
        )


# Encodings we'll try when scoring. utf-8-sig handles the BOM-prefixed case
# (chardet sometimes guesses utf-8 for a BOMed file but raw.decode("utf-8")
# leaves the U+FEFF in the output — utf-8-sig strips it during decode).
# gb18030 is the superset of GBK / GB2312, so a successful gb18030 decode
# covers all simplified-CN raws. big5 / cp950 cover Traditional Chinese from
# Taiwanese sources (CP950 is Microsoft's Big5 superset). utf-16 handles
# Windows-exported files. Order is informational only — the scored decoder
# picks the highest-scoring decode, not the first-success.
_DECODE_CANDIDATES = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "cp950", "utf-16")

# Latin single-byte encodings accept every byte 0x00-0xFF so they decode
# successfully on bytes that are actually CN — producing mojibake. Don't seed
# the candidate set with a Latin chardet guess: a misdetection that scored
# even slightly above utf-8 would otherwise win. Real Latin files will still
# decode correctly under utf-8 (ASCII subset) or via the fallback path below.
_LATIN_ENCODINGS = frozenset({
    "windows-1252", "iso-8859-1", "ascii", "latin-1", "cp1252",
})


def _score_decode(raw: bytes, enc: str) -> tuple[float, str] | None:
    """Try to decode `raw` as `enc` and return (score, text), or None if it
    raised. Score rewards CJK content density and penalises replacement and
    control characters. Pure CJK text scores ~100; Latin-mojibake of CN bytes
    scores near 0 (no CJK chars at all). Mixed Latin-and-CJK content (CN raws
    with author notes, technique names, etc.) lands between."""
    try:
        text = raw.decode(enc)
    except (UnicodeDecodeError, LookupError):
        return None
    n = len(text) or 1
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    replacement = text.count("�")
    control = sum(1 for c in text if c < " " and c not in "\n\r\t")
    score = (cjk / n) * 100.0 - replacement * 5.0 - (control / n) * 50.0
    return score, text


def _decode_with_fallback(raw: bytes) -> tuple[str, str]:
    """Scored encoding detection. Tries chardet's guess (skipped if Latin),
    plus a fixed candidate set covering UTF-8, GB18030/GBK, Big5/CP950, and
    UTF-16. Returns the highest-scoring decode by CJK-density score, which
    closes the prior bug where chardet would confidently guess windows-1252
    for a short GB18030 fragment and the first-success strict-decode path
    would return Latin mojibake.

    Last-ditch: when every candidate raised, fall back to chardet's guess
    (or utf-8) with errors='replace' so the user still gets a usable but
    visibly-marked file."""
    candidates: dict[str, tuple[float, str]] = {}
    detected = chardet.detect(raw)
    guess = (detected.get("encoding") or "").lower()
    if guess and guess not in _LATIN_ENCODINGS:
        scored = _score_decode(raw, guess)
        if scored is not None:
            candidates[guess] = scored
    for enc in _DECODE_CANDIDATES:
        if enc in candidates:
            continue
        scored = _score_decode(raw, enc)
        if scored is not None:
            candidates[enc] = scored
    if candidates:
        enc, (_, text) = max(candidates.items(), key=lambda kv: kv[1][0])
        return text, enc
    # Every strict decode failed (extremely rare — utf-8 strict would only
    # fail on bytes that are also invalid GB18030 and Big5). Lossy fall-back.
    enc = guess or "utf-8"
    try:
        return raw.decode(enc, errors="replace"), enc
    except LookupError:
        return raw.decode("utf-8", errors="replace"), "utf-8"


def _strip_bom(text: str) -> str:
    """Drop a leading U+FEFF so it doesn't end up at the front of the first
    parsed chapter's title. UTF-8 / UTF-16 / UTF-32 all surface as U+FEFF
    after decoding."""
    if text.startswith("﻿"):
        return text[1:]
    return text


async def _read_text_file(file: UploadFile) -> tuple[str, str]:
    """Read an UploadFile, enforce MAX_UPLOAD_BYTES, detect encoding via chardet
    (with confidence-aware fallback), and return (decoded_text, encoding).
    Raises 413 on oversize, 400 on empty."""
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file too large (max {MAX_UPLOAD_BYTES} bytes)",
        )
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file too large (max {MAX_UPLOAD_BYTES} bytes)",
        )
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    text, encoding = _decode_with_fallback(raw)
    return _strip_bom(text), encoding


# ----- Initiative 7: EPUB / DOCX / HTML decoders -----
#
# All three decoders flatten the source document into the same paragraph-
# joined text shape `parse_chapters` already consumes (`"\n\n"` between
# paragraphs, heading conventions like 第N章 / "Chapter N: …" preserved if
# present). The `encoding` field becomes a format label ("epub"/"docx"/"html")
# rather than a byte encoding — UploadFile encoding doesn't apply for these
# formats.
#
# `DecodedDoc` lets the EPUB decoder optionally return a cover image without
# changing every caller — TXT/DOCX/HTML pass `cover_bytes=None` and the upload
# route's cover-side-effect branch becomes a no-op for them.


@dataclass(frozen=True)
class DecodedDoc:
    text: str
    encoding: str
    raw_size: int
    cover_bytes: bytes | None = None
    cover_ext: str | None = None
    # 2026-05-25: structured chapter list when the format carries its own
    # chapter boundaries (EPUB spine items, DOCX Heading-1 styles). When
    # set, the upload route SHOULD use these directly via
    # _atomic_create_novel(chapters=...) instead of running parse_chapters
    # over `text`. NULL = no structural signal; caller falls back to text-
    # blob + heading regex (current behavior).
    pre_parsed_chapters: list[ParsedChapter] | None = None


def _ext_from_filename(filename: str | None) -> str:
    """Lowercase extension without dot, or '' if missing. Used by the upload
    dispatcher; lives here so the test path can call it directly."""
    if not filename:
        return ""
    _, _, ext = filename.rpartition(".")
    return ext.lower() if ext else ""


def _enforce_upload_size(raw_len: int, filename: str | None) -> None:
    if raw_len > MAX_UPLOAD_BYTES:
        name = f"'{filename}' " if filename else ""
        raise HTTPException(
            status_code=413,
            detail=f"file {name}too large (max {MAX_UPLOAD_BYTES} bytes)",
        )


async def _decode_html(file: UploadFile) -> DecodedDoc:
    """Run trafilatura over a raw .html upload to recover narrative body text.
    Mirrors the post-fetch step of `services.scraper.scrape_url`, just sourced
    from upload bytes instead of an HTTP response. Lazy-imports trafilatura to
    keep startup cost in the existing scrape paths."""
    if file.size is not None:
        _enforce_upload_size(file.size, file.filename)
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    _enforce_upload_size(len(raw), file.filename)
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")

    # Decode as text. HTML can be utf-8 / gb18030 / etc.; reuse the scoring
    # decoder so chardet's windows-1252 misdetect doesn't poison the input.
    html_text, encoding = _decode_with_fallback(raw)
    html_text = _strip_bom(html_text)

    import trafilatura  # lazy: pulls lxml + a chunk of static data

    extracted = trafilatura.extract(
        html_text,
        output_format="txt",
        include_comments=False,
        include_tables=False,
        include_links=False,
        favor_recall=True,
    )
    if not extracted or not extracted.strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "no extractable article content in the HTML upload. The file "
                "may be a TOC / index page, or render its body via JavaScript "
                "the parser can't see."
            ),
        )
    return DecodedDoc(
        text=_strip_bom(extracted),
        encoding="html",  # format label, not byte encoding
        raw_size=len(raw),
    )


# Stylesheet runs / image runs in a DOCX paragraph don't carry semantic
# content for us — we squash whitespace to keep the parser's heading-detection
# regex matches reliable.
_WS_COLLAPSE_RE = re.compile(r"[ \t　 ]+")


_DOCX_HEADING_1_STYLES = frozenset({
    "heading 1",     # English default
    "标题 1",         # Simplified Chinese
    "標題 1",         # Traditional Chinese
    "見出し 1",       # Japanese
    "제목 1",         # Korean
})

_DOCX_HEADING_MIN_CHAPTERS = 2
"""DOCX heading-style → chapter split must yield at least N chapters to
take the structured path. A 1-chapter result means the user just styled
one paragraph as Heading 1 in a flat document; the regex-fallback path
will do a better job there."""


def _docx_is_heading_1(style_name: str) -> bool:
    """Match Heading-1 style (and common localized variants). Used by the
    structured-chapter splitter. Heading-2 and below are NOT counted as
    chapter starts — they're inline section headers."""
    s = (style_name or "").strip().lower()
    return s in _DOCX_HEADING_1_STYLES


async def _decode_docx(file: UploadFile) -> DecodedDoc:
    """Extract paragraph text from a .docx.

    2026-05-25 (F07): if the document has ≥_DOCX_HEADING_MIN_CHAPTERS
    paragraphs styled Heading 1 (or a localized variant), structurally
    split into one ParsedChapter per heading. Each Heading-1 paragraph
    becomes the next chapter's title; subsequent non-Heading-1
    paragraphs accumulate as the body until the next Heading-1.

    Documents without that structural signal fall back to the legacy
    text-blob path — every paragraph joined with `\\n\\n`, then
    `parse_chapters` runs heading-detection regex over the blob.

    No inline formatting (em/strong/br) is preserved in v1."""
    if file.size is not None:
        _enforce_upload_size(file.size, file.filename)
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    _enforce_upload_size(len(raw), file.filename)
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    _reject_zip_bomb(raw, file.filename)

    try:
        from docx import Document  # python-docx
    except ImportError as e:  # pragma: no cover — pyproject guarantees presence
        raise HTTPException(
            status_code=500,
            detail=f"python-docx not installed: {e}",
        ) from e

    try:
        doc = Document(io.BytesIO(raw))
    except Exception as e:
        # python-docx raises PackageNotFoundError for non-zip / non-docx
        # uploads. Surface as 400 so the client knows it's the file, not
        # the server.
        raise HTTPException(
            status_code=400,
            detail=f"could not parse .docx: {type(e).__name__}: {e}",
        ) from e

    # Two-pass extraction: collect (text, is_heading_1) tuples first; if
    # ≥2 headings landed, do the structural split. Otherwise emit the
    # flat text blob as before. Single-pass would force us to commit to
    # one shape before we know what we have.
    rows: list[tuple[str, bool]] = []
    for p in doc.paragraphs:
        text = (p.text or "").strip()
        if not text:
            continue
        text = _WS_COLLAPSE_RE.sub(" ", text)
        style_name = ""
        try:
            style_name = p.style.name or "" if p.style else ""
        except Exception:
            style_name = ""
        rows.append((text, _docx_is_heading_1(style_name)))

    if not rows:
        raise HTTPException(
            status_code=400,
            detail="DOCX contained no extractable paragraphs",
        )

    heading_count = sum(1 for _, is_h1 in rows if is_h1)
    if heading_count >= _DOCX_HEADING_MIN_CHAPTERS:
        pre_parsed = _docx_split_by_headings(rows)
        if pre_parsed:
            # Flat text still returned for backward-compat / debugging;
            # caller consumes pre_parsed_chapters when present.
            text = "\n\n".join(t for t, _ in rows)
            return DecodedDoc(
                text=_strip_bom(text),
                encoding="docx",
                raw_size=len(raw),
                pre_parsed_chapters=pre_parsed,
            )

    text = "\n\n".join(t for t, _ in rows)
    return DecodedDoc(text=_strip_bom(text), encoding="docx", raw_size=len(raw))


def _docx_split_by_headings(
    rows: list[tuple[str, bool]],
) -> list[ParsedChapter] | None:
    """Walk (text, is_heading_1) rows; each Heading-1 starts a new chapter.
    Preamble before the first Heading-1 is dropped (typically a title page
    or copyright that the publisher styled as Title, not Heading 1)."""
    chapters: list[ParsedChapter] = []
    current_title: str | None = None
    current_body: list[str] = []
    chapter_idx = 0

    def _close():
        nonlocal current_title, current_body, chapter_idx
        if current_title is None:
            return
        body = "\n\n".join(current_body).strip()
        if not body:
            # Empty chapter body — heading without content. Skip silently
            # (the next heading takes over).
            current_title = None
            current_body = []
            return
        chapter_idx += 1
        chapters.append(
            ParsedChapter(
                chapter_num=chapter_idx,
                title_zh=current_title,
                original_text=body,
                printed_num=None,
            )
        )
        current_title = None
        current_body = []

    for text, is_h1 in rows:
        if is_h1:
            _close()
            current_title = text
        else:
            if current_title is None:
                # Preamble before first heading — drop.
                continue
            current_body.append(text)
    _close()

    if len(chapters) < _DOCX_HEADING_MIN_CHAPTERS:
        return None
    return chapters


# Tags whose textual content we keep verbatim (no rewriting / no <br/>
# expansion in v1). Anything not in this set has its tail text discarded
# so we don't pull in CSS-style or script content.
_EPUB_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "blockquote",
    "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "pre", "td", "th",
})

# Cover-image MIME → file extension, used when an EPUB ships its cover at
# a `<meta name="cover"/>`-pointed item.
_EPUB_COVER_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _epub_extract_cover(book) -> tuple[bytes, str] | None:
    """Return (image_bytes, ext) for the EPUB cover image, or None when the
    archive doesn't expose one. Tries the common locations:

    1. The item flagged via `<meta name="cover" content="<id>"/>` (EPUB 2).
    2. Any item with property "cover-image" (EPUB 3).
    3. An item whose href ends in 'cover.<ext>'.

    Cover sniffing here mirrors how Calibre / iBooks resolve covers; we copy
    the bytes out so the upload route can hand them to the existing cover-
    write helper without ebooklib needing to stick around."""
    try:
        from ebooklib import ITEM_COVER, ITEM_IMAGE  # type: ignore[attr-defined]
    except ImportError:  # pragma: no cover
        return None

    # EPUB 2: <meta name="cover" content="cover-image-id"/>
    cover_id: str | None = None
    try:
        for name, value in book.get_metadata("OPF", "meta") or []:  # type: ignore[union-attr]
            if isinstance(value, dict) and value.get("name") == "cover":
                cover_id = value.get("content")
                break
    except Exception:
        pass
    if cover_id:
        item = book.get_item_with_id(cover_id)
        if item is not None and item.get_content():
            mime = (item.media_type or "").lower()
            ext = _EPUB_COVER_EXT_BY_MIME.get(mime)
            if ext:
                return bytes(item.get_content()), ext

    # EPUB 3: items with type ITEM_COVER.
    try:
        cover_items = list(book.get_items_of_type(ITEM_COVER))
    except Exception:
        cover_items = []
    for item in cover_items:
        content = item.get_content() or b""
        if not content:
            continue
        mime = (item.media_type or "").lower()
        ext = _EPUB_COVER_EXT_BY_MIME.get(mime)
        if ext:
            return bytes(content), ext

    # Fallback: any image item whose href ends with 'cover.*'.
    try:
        image_items = list(book.get_items_of_type(ITEM_IMAGE))
    except Exception:
        image_items = []
    for item in image_items:
        href = (item.get_name() or "").lower()
        if "cover" in href:
            content = item.get_content() or b""
            if not content:
                continue
            mime = (item.media_type or "").lower()
            ext = _EPUB_COVER_EXT_BY_MIME.get(mime) or href.rsplit(".", 1)[-1]
            if ext in {"jpg", "jpeg", "png", "gif", "webp"}:
                return bytes(content), "jpg" if ext == "jpeg" else ext
    return None


def _epub_extract_text(book) -> str:
    """Flatten an EPUB book's reading-order documents to paragraph-split
    text. Walks the spine in order so chapter sequencing matches the
    publisher's intended order rather than alphabetical filename order.

    Per-document extraction: parse the XHTML, collect text from block-level
    tags, treat each h1/h2/h3 as its own paragraph (so it becomes a heading
    line `parse_chapters` can match), and join everything with `\\n\\n`."""
    try:
        from ebooklib import ITEM_DOCUMENT  # type: ignore[attr-defined]
    except ImportError as e:  # pragma: no cover
        raise HTTPException(
            status_code=500,
            detail=f"ebooklib not installed: {e}",
        ) from e

    # Spine ordering: book.spine is a list of (id, linear) tuples.
    spine_ids = [s[0] for s in (book.spine or [])]
    items_by_id: dict[str, object] = {}
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        items_by_id[item.get_id()] = item
    ordered_items = [items_by_id[i] for i in spine_ids if i in items_by_id]
    # If the spine is empty, fall back to ITEM_DOCUMENT iteration order.
    if not ordered_items:
        ordered_items = list(book.get_items_of_type(ITEM_DOCUMENT))

    paragraphs: list[str] = []
    for item in ordered_items:
        content = item.get_content() or b""
        if not content:
            continue
        # Defer to lxml's html parser via BeautifulSoup-style traversal. We
        # avoid BeautifulSoup as a hard dep; ebooklib ships lxml.
        try:
            from lxml import html as lxml_html  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover
            raise HTTPException(
                status_code=500,
                detail=f"lxml not installed: {e}",
            ) from e

        try:
            tree = lxml_html.fromstring(content)
        except Exception:
            # Skip a single corrupt document rather than fail the whole upload.
            continue
        # Strip <script>/<style>/<nav> outright so their text doesn't leak in.
        for tag in tree.xpath("//script | //style | //nav"):
            tag.getparent().remove(tag) if tag.getparent() is not None else None

        # h1/h2/h3 first — emit each as its own paragraph (so the parser's
        # heading-detection regex sees them on their own line). Then p tags.
        for heading in tree.xpath(".//h1 | .//h2 | .//h3"):
            text = " ".join(heading.itertext()).strip()
            text = _WS_COLLAPSE_RE.sub(" ", text)
            if text:
                paragraphs.append(text)
        for para in tree.xpath(".//p"):
            text = " ".join(para.itertext()).strip()
            text = _WS_COLLAPSE_RE.sub(" ", text)
            if text:
                paragraphs.append(text)
        # A few EPUB authors use <div>/<br> for paragraph splits instead of
        # <p>. As a fallback, when we got NOTHING from this item via p/h*,
        # split on the document's overall text by double-newline.
        if not any(paragraphs):
            text_only = (tree.text_content() or "").strip()
            for chunk in re.split(r"\n\s*\n+", text_only):
                chunk = _WS_COLLAPSE_RE.sub(" ", chunk).strip()
                if chunk:
                    paragraphs.append(chunk)

    if not paragraphs:
        raise HTTPException(
            status_code=400,
            detail="EPUB contained no extractable paragraphs",
        )
    return "\n\n".join(paragraphs)


_EPUB_SPINE_MIN_ITEMS = 3
"""Minimum spine-item count for the spine-as-chapter path. Books with
fewer items (single-file novellas, "all-in-one" EPUBs that compile
chapters into one document) fall back to text-blob + heading regex so
parse_chapters can recover the chapter boundaries from the body."""


def _epub_extract_spine_chapters(book) -> list[ParsedChapter] | None:
    """Each spine item becomes one chapter (2026-05-25, F07 structural win).

    Returns a list of ParsedChapter if the spine has at least
    _EPUB_SPINE_MIN_ITEMS non-empty documents, otherwise None — caller
    falls back to the flat-text path so heading detection can run.

    For each spine item:
    - title_zh: first <h1>/<h2>/<h3>/<title> text, falling back to the
      item's filename stem.
    - original_text: paragraph-joined body (h1/h2/h3 included as their
      own lines so reader rendering keeps the heading visible).
    - chapter_num: 1-indexed spine position. (parse_chapters' heading-
      anchored reconciliation isn't run for spine-derived chapters —
      structural EPUBs intentionally don't have printed numbers in
      every chapter and we'd lose ordering to false positives.)
    """
    try:
        from ebooklib import ITEM_DOCUMENT  # type: ignore[attr-defined]
        from lxml import html as lxml_html  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        return None

    spine_ids = [s[0] for s in (book.spine or [])]
    if not spine_ids:
        return None

    items_by_id: dict[str, object] = {}
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        items_by_id[item.get_id()] = item
    ordered_items = [items_by_id[i] for i in spine_ids if i in items_by_id]
    if len(ordered_items) < _EPUB_SPINE_MIN_ITEMS:
        return None

    chapters: list[ParsedChapter] = []
    for i, item in enumerate(ordered_items, start=1):
        content = item.get_content() or b""
        if not content:
            continue
        try:
            tree = lxml_html.fromstring(content)
        except Exception:
            continue
        for tag in tree.xpath("//script | //style | //nav"):
            tag.getparent().remove(tag) if tag.getparent() is not None else None

        title_text: str | None = None
        # Prefer the first heading; fall back to <title> element.
        for path in (".//h1", ".//h2", ".//h3"):
            for heading in tree.xpath(path):
                t = " ".join(heading.itertext()).strip()
                t = _WS_COLLAPSE_RE.sub(" ", t)
                if t:
                    title_text = t
                    break
            if title_text:
                break
        if not title_text:
            for tnode in tree.xpath(".//title"):
                t = " ".join(tnode.itertext()).strip()
                t = _WS_COLLAPSE_RE.sub(" ", t)
                if t:
                    title_text = t
                    break
        if not title_text:
            # Last resort: filename stem.
            file_name = getattr(item, "file_name", "") or ""
            stem = file_name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            title_text = stem or f"Chapter {i}"

        paragraphs: list[str] = []
        for heading in tree.xpath(".//h1 | .//h2 | .//h3"):
            t = " ".join(heading.itertext()).strip()
            t = _WS_COLLAPSE_RE.sub(" ", t)
            if t:
                paragraphs.append(t)
        for para in tree.xpath(".//p"):
            t = " ".join(para.itertext()).strip()
            t = _WS_COLLAPSE_RE.sub(" ", t)
            if t:
                paragraphs.append(t)
        if not paragraphs:
            text_only = (tree.text_content() or "").strip()
            for chunk in re.split(r"\n\s*\n+", text_only):
                chunk = _WS_COLLAPSE_RE.sub(" ", chunk).strip()
                if chunk:
                    paragraphs.append(chunk)

        body = "\n\n".join(paragraphs).strip()
        if not body:
            continue
        chapters.append(
            ParsedChapter(
                chapter_num=i,
                title_zh=title_text,
                original_text=body,
                printed_num=None,
            )
        )

    if len(chapters) < _EPUB_SPINE_MIN_ITEMS:
        # Trimmed below threshold after dropping empty items — fall back.
        return None
    return chapters


async def _decode_epub(file: UploadFile) -> DecodedDoc:
    """Parse an EPUB upload via ebooklib. Returns paragraph-joined text and,
    when the EPUB ships a cover image, the cover bytes + extension so the
    upload route can write it via the cover-storage helper.

    2026-05-25 (F07): if the spine has ≥_EPUB_SPINE_MIN_ITEMS items, also
    return pre_parsed_chapters with one ParsedChapter per spine item. The
    upload route uses that directly via _atomic_create_novel(chapters=...)
    and skips the text-blob + parse_chapters fallback path."""
    if file.size is not None:
        _enforce_upload_size(file.size, file.filename)
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    _enforce_upload_size(len(raw), file.filename)
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    _reject_zip_bomb(raw, file.filename)

    try:
        from ebooklib import epub  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise HTTPException(
            status_code=500,
            detail=f"ebooklib not installed: {e}",
        ) from e

    try:
        book = epub.read_epub(io.BytesIO(raw))
    except Exception as e:
        # ebooklib is permissive but the underlying zipfile / lxml can still
        # raise on corrupted archives. Surface as 400.
        raise HTTPException(
            status_code=400,
            detail=f"could not parse .epub: {type(e).__name__}: {e}",
        ) from e

    spine_chapters = _epub_extract_spine_chapters(book)
    text = _epub_extract_text(book) if spine_chapters is None else ""
    cover = _epub_extract_cover(book)
    cover_bytes = cover[0] if cover is not None else None
    cover_ext = cover[1] if cover is not None else None
    return DecodedDoc(
        text=_strip_bom(text),
        encoding="epub",
        raw_size=len(raw),
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        pre_parsed_chapters=spine_chapters,
    )


async def _read_bulk_file(file: UploadFile) -> tuple[str, str, int] | None:
    """Variant of _read_text_file for bulk upload: oversize raises (whole batch
    fails), but empty files are skipped (returns None) instead of erroring.
    Returns (decoded_text, encoding, raw_byte_size) so the caller can both
    aggregate detected encodings AND enforce the aggregate-bytes cap. The
    raw size is the on-the-wire byte count, NOT the decoded char count —
    that's what the cap should defend against (memory used to hold the file)."""
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file '{file.filename}' too large (max {MAX_UPLOAD_BYTES} bytes)",
        )
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file '{file.filename}' too large (max {MAX_UPLOAD_BYTES} bytes)",
        )
    if not raw:
        return None
    text, encoding = _decode_with_fallback(raw)
    text = _strip_bom(text)
    if not text.strip():
        # Whitespace-only post-decode is a strong signal of encoding
        # misdetection — control characters that .strip() removed. Logged so
        # a grep can surface the case; still skipped (same as truly empty).
        logger.info(
            "bulk: file '%s' decoded to whitespace under encoding=%s; skipping",
            file.filename, encoding,
        )
        return None
    return text, encoding, len(raw)


# Batch size for executemany on chapter insertion. Bulk uploads with thousands
# of files would otherwise hold the entire batch's decoded text in a single
# Python list passed to executemany at once.
_INSERT_BATCH_SIZE = 500


async def _resolve_novel_defaults(conn: aiosqlite.Connection) -> dict:
    """Reads config_kv['novel_defaults'] and returns a dict of column→value
    to apply at novel-creation time. Only non-NULL / non-empty values land
    in the result. The whole point of the defaults precedence:

      request body field → config_kv default → NULL → runtime fallback

    NULL on the novel row means "no per-novel override, fall back to the
    system default at runtime" (the existing translator chooses the default
    provider, the existing prompt loader chooses DEFAULT_GENRE). The
    defaults written via Settings give a user-tunable middle layer that
    survives without permanently stamping every column.

    Existing novels are NEVER backfilled by this — it only fires inside
    the INSERT path for newly created novels. The per-novel dialog in
    Library remains the explicit override surface for an existing novel.
    """
    import json
    cur = await conn.execute(
        "SELECT value FROM config_kv WHERE key = ?", ("novel_defaults",)
    )
    row = await cur.fetchone()
    if row is None:
        return {}
    try:
        parsed = json.loads(row["value"])
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out = {}
    # Only fields we accept here. Anything else in the JSON is ignored —
    # a typo in the blob can't poison an unrelated column. As of 2026-05-25
    # `genre` and `source_language` are deliberately NOT in the whitelist:
    # genre is auto-set on scrape (or picked at import); source_language is
    # auto-detected from the chapter text. Both are properties of the
    # novel, not app-wide defaults. Stale blobs that still carry those
    # keys read as "no entry" here and are silently dropped.
    for col in ("translator_provider_id", "refinement_provider_id"):
        v = parsed.get(col)
        if v is None or v == "":
            continue
        out[col] = v
    return out


async def _insert_novel_row(
    conn: aiosqlite.Connection,
    title: str,
    source_type: str,
    source_url: str | None = None,
    *,
    genre: str | None = None,
    source_language: str | None = None,
) -> int:
    """Inserts ONE novel row without committing. Callers are expected to wrap
    a novel-insert + chapter-insert pair inside a single BEGIN IMMEDIATE /
    commit so a crash between them can't leave an orphan novel.

    Reads novel-creation defaults from config_kv and applies them as INSERT
    columns. Defaults are best-effort — a bad JSON blob in config_kv reads
    as "no defaults" and the row inserts with NULL columns (i.e. system-
    default behavior). See _resolve_novel_defaults for the precedence.

    `genre` and `source_language` are explicit per-import overrides:
    - genre: set by the user (paste/upload/bulk picker) or the recipe's
      default_genre (scrape). Non-NULL value lands on novels.genre.
    - source_language: auto-detected from the first chapter's text via
      services/lang_detect.py. Non-NULL value lands on novels.source_language.

    These are passed in by the caller, NOT pulled from config_kv defaults
    (which intentionally no longer accept them — see _resolve_novel_defaults).
    """
    cols = ["title", "source_type", "source_url"]
    vals: list = [title.strip(), source_type, source_url]
    defaults = await _resolve_novel_defaults(conn)
    for col, val in defaults.items():
        cols.append(col)
        vals.append(val)
    if genre is not None:
        cols.append("genre")
        vals.append(genre)
    if source_language is not None:
        cols.append("source_language")
        vals.append(source_language)
    placeholders = ",".join("?" * len(cols))
    cur = await conn.execute(
        f"INSERT INTO novels ({','.join(cols)}) VALUES ({placeholders})",
        vals,
    )
    return cur.lastrowid


async def _insert_parsed_chapters(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapters: list[ParsedChapter],
) -> None:
    """Stream rows to executemany in batches so a 10,000-file import doesn't
    materialize one giant list at peak. Does NOT commit — see _insert_novel_row."""
    batch: list[tuple[int, int, str | None, str]] = []
    for ch in chapters:
        batch.append((novel_id, ch.chapter_num, ch.title_zh, ch.original_text))
        if len(batch) >= _INSERT_BATCH_SIZE:
            await conn.executemany(
                "INSERT INTO chapters (novel_id, chapter_num, title_zh, original_text, status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                batch,
            )
            batch.clear()
    if batch:
        await conn.executemany(
            "INSERT INTO chapters (novel_id, chapter_num, title_zh, original_text, status) "
            "VALUES (?, ?, ?, ?, 'pending')",
            batch,
        )


async def _atomic_create_novel(
    conn: aiosqlite.Connection,
    title: str,
    chapters: list[ParsedChapter],
    source_type: str,
    source_url: str | None = None,
    *,
    genre: str | None = None,
    source_language: str | None = None,
) -> int:
    """Insert novel row + every chapter inside a single transaction. A crash,
    FK error, or constraint violation between them can't leave an orphan
    novel row (visible in the library, unopenable, with zero chapters)."""
    await conn.execute("BEGIN IMMEDIATE")
    try:
        novel_id = await _insert_novel_row(
            conn, title, source_type, source_url,
            genre=genre, source_language=source_language,
        )
        await _insert_parsed_chapters(conn, novel_id, chapters)
        await conn.commit()
    except Exception:
        # Exception (not BaseException) so signal-driven shutdown propagates
        # immediately without a cooperative rollback round-trip — see the
        # matching comment in _append_with_offset.
        await conn.rollback()
        raise
    return novel_id


# ============================================================
# Resumable-import helpers (2026-05-26)
# ============================================================
# Recipe scrapes pre-create the novel row + N empty chapter rows up-front so
# the slow per-chapter fetch loop can commit each result as it lands. A crash
# mid-fetch then leaves partial state intact: any chapter whose
# `import_fetched_at` is still NULL is still pending.
#
# The skeleton helpers below are designed to coexist with _atomic_create_novel
# (still used by /paste, generic /scrape, and small uploads) rather than
# replace it — the atomic path is fine when the import completes in seconds
# and a crash window is negligible.


@dataclass(frozen=True)
class PlannedChapter:
    """One row queued for incremental fetch. `chapter_num` is locked in at
    planning time (reconciled against the printed numbers in the catalog
    page) so the skeleton row's chapter_num matches the final state.
    `source_url` is the per-chapter URL the runner re-fetches on resume."""
    chapter_num: int
    title_zh: str | None
    source_url: str


async def _create_novel_skeleton(
    conn: aiosqlite.Connection,
    title: str,
    planned: list[PlannedChapter],
    source_type: str,
    source_url: str | None,
    *,
    genre: str | None = None,
    source_language: str | None = None,
) -> int:
    """Insert the novel row (with `import_status='in_progress'`) plus N
    skeleton chapter rows in one short transaction. Returns the novel_id.

    Skeleton chapter rows carry:
    - `original_text = ''` (NOT NULL constraint forces a value; the runner
      identifies "pending" via the import_* sentinels, not the body).
    - `status = 'pending'` (same as a normal pre-translation chapter).
    - `import_source_url = planned.source_url` — what the runner re-fetches.
    - `import_fetched_at = NULL` — flipped to datetime('now') by
      _fill_skeleton_chapter on a successful commit.

    The partial index `idx_chapters_import_pending` makes the runner's
    resume query O(pending) regardless of novel size."""
    await conn.execute("BEGIN IMMEDIATE")
    try:
        novel_id = await _insert_novel_row(
            conn, title, source_type, source_url,
            genre=genre, source_language=source_language,
        )
        await conn.execute(
            "UPDATE novels SET import_status = 'in_progress' WHERE id = ?",
            (novel_id,),
        )
        # Batch-insert skeletons. Same _INSERT_BATCH_SIZE chunking as
        # _insert_parsed_chapters so a 5000-chapter plan doesn't push a
        # single 5000-row executemany at SQLite.
        batch: list[tuple[int, int, str | None, str]] = []
        for p in planned:
            batch.append((novel_id, p.chapter_num, p.title_zh, p.source_url))
            if len(batch) >= _INSERT_BATCH_SIZE:
                await conn.executemany(
                    "INSERT INTO chapters "
                    "(novel_id, chapter_num, title_zh, original_text, status, "
                    "import_source_url) "
                    "VALUES (?, ?, ?, '', 'pending', ?)",
                    batch,
                )
                batch.clear()
        if batch:
            await conn.executemany(
                "INSERT INTO chapters "
                "(novel_id, chapter_num, title_zh, original_text, status, "
                "import_source_url) "
                "VALUES (?, ?, ?, '', 'pending', ?)",
                batch,
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return novel_id


async def _fill_skeleton_chapter(
    conn: aiosqlite.Connection,
    chapter_id: int,
    *,
    title_zh: str | None,
    original_text: str,
) -> bool:
    """Populate one skeleton chapter row and commit. Idempotent: the
    WHERE clause requires `import_fetched_at IS NULL`, so re-running this
    on an already-filled row is a no-op (returns False). Lets the runner
    retry safely on transient errors without double-writing.

    Returns True if this call filled the row; False if it was already
    filled (someone else got there first, or this is a duplicate retry).
    """
    cur = await conn.execute(
        "UPDATE chapters SET original_text = ?, title_zh = ?, "
        "import_fetched_at = datetime('now') "
        "WHERE id = ? AND import_fetched_at IS NULL",
        (original_text, title_zh, chapter_id),
    )
    await conn.commit()
    return (cur.rowcount or 0) > 0


async def _set_novel_import_status(
    conn: aiosqlite.Connection, novel_id: int, status: str,
) -> None:
    """Flip novels.import_status. Valid values per the schema comment:
    'in_progress' | 'paused' | 'done' | 'cancelled'. Callers should also
    use this to set 'done' once the last skeleton row is filled, so the
    library card drops the in-progress badge."""
    await conn.execute(
        "UPDATE novels SET import_status = ? WHERE id = ?",
        (status, novel_id),
    )
    await conn.commit()


async def _count_pending_skeletons(
    conn: aiosqlite.Connection, novel_id: int,
) -> int:
    """How many skeleton chapter rows still need a fetch. Read via the
    partial index `idx_chapters_import_pending` so this is O(pending),
    not O(total chapters). Used by the runner to decide done vs paused."""
    cur = await conn.execute(
        "SELECT COUNT(*) FROM chapters "
        "WHERE novel_id = ? "
        "AND import_fetched_at IS NULL "
        "AND import_source_url IS NOT NULL",
        (novel_id,),
    )
    row = await cur.fetchone()
    return int(row[0] if row else 0)


async def _create_novel_and_chapters(
    conn: aiosqlite.Connection,
    title: str,
    text: str,
    source_type: str,
    source_url: str | None = None,
    *,
    genre: str | None = None,
) -> tuple[int, int]:
    """Returns (novel_id, first_chapter_num). first_chapter is the chapter_num
    of the lowest-numbered chapter in the import — usually 1, but for a partial
    raw starting at 第296章 it's 296 (anchored by parse_chapters via
    reconcile_chapter_numbers). The frontend uses this to land the reader on a
    real chapter rather than hardcoding ?ch=1 and 404'ing.

    Source language is auto-detected from the first chapter's text via
    services/lang_detect.py and stamped on the novel row at insert time.
    Genre, if provided by the caller (paste/upload picker), is stamped
    too; NULL leaves it for the user to set later on the novel page."""
    chapters = parse_chapters(text)
    if not chapters:
        raise HTTPException(status_code=400, detail="no chapters parsed from input")
    # Detect source language from the FIRST chapter's body — short enough
    # to be fast, large enough to be representative. Avoids scanning the
    # whole concatenated text for a per-import one-shot decision.
    detected_lang = lang_detect.detect_source_language(chapters[0].original_text)
    novel_id = await _atomic_create_novel(
        conn, title, chapters, source_type, source_url,
        genre=genre, source_language=detected_lang,
    )
    first_chapter = min(ch.chapter_num for ch in chapters)
    return novel_id, first_chapter


async def _ensure_novel_exists(
    conn: aiosqlite.Connection, novel_id: int
) -> aiosqlite.Row:
    cur = await conn.execute(
        "SELECT id, title, source_type, source_url FROM novels WHERE id = ?",
        (novel_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="novel not found")
    return row


async def _max_chapter_num(conn: aiosqlite.Connection, novel_id: int) -> int:
    cur = await conn.execute(
        "SELECT COALESCE(MAX(chapter_num), 0) AS m FROM chapters WHERE novel_id = ?",
        (novel_id,),
    )
    row = await cur.fetchone()
    return int(row["m"]) if row else 0


async def _append_parsed_chapters(
    conn: aiosqlite.Connection, novel_id: int, text: str
) -> tuple[int, int, bool]:
    """Parse text into chapters and append them after the current max chapter_num.
    Returns (added_count, first_new_chapter_num, chapter_num_collision).
    See _append_with_offset for the collision flag semantics."""
    chapters = parse_chapters(text)
    if not chapters:
        raise HTTPException(status_code=400, detail="no chapters parsed from input")
    first_new, collision = await _append_with_offset(conn, novel_id, chapters)
    return len(chapters), first_new, collision


async def _append_with_offset(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapters: list[ParsedChapter],
) -> tuple[int, bool]:
    """Atomically read MAX(chapter_num) and insert `chapters`.

    When every appended chapter carries a printed heading number and the
    batch's own reconciled numbers all sit above the novel's current
    MAX(chapter_num), the chapters are inserted at those numbers directly — so
    appending the next run of raws (第299章 …) lands at 299, not MAX+1. When
    the batch is not reliably numbered (a missing heading, or numbers that
    would collide with existing rows), every number is shifted by
    MAX(chapter_num) instead — the original positional behavior.

    Returns (first_new_chapter_num, chapter_num_collision). `collision` is
    True when the offset path was chosen specifically because the batch's
    printed numbers would overlap existing rows (all_printed=True but
    fits_above=False) — almost always means the user re-appended raws that
    already exist. Surfaced in the route response so the frontend can warn,
    and logged at WARNING so an operator can spot the case. False means
    either no collision (printed-direct path) or a legitimately positional
    append (no printed numbers at all).

    BEGIN IMMEDIATE acquires the write lock up front so a concurrent append on
    the same novel waits on the lock instead of reading the same MAX and
    producing duplicate chapter_num values that violate UNIQUE(novel_id,
    chapter_num)."""
    await conn.execute("BEGIN IMMEDIATE")
    try:
        offset = await _max_chapter_num(conn, novel_id)
        all_printed = bool(chapters) and all(
            ch.printed_num is not None for ch in chapters
        )
        fits_above = bool(chapters) and min(ch.chapter_num for ch in chapters) > offset
        collision = all_printed and not fits_above
        if all_printed and fits_above:
            rows = [
                (novel_id, ch.chapter_num, ch.title_zh, ch.original_text)
                for ch in chapters
            ]
        else:
            rows = [
                (novel_id, ch.chapter_num + offset, ch.title_zh, ch.original_text)
                for ch in chapters
            ]
        if collision:
            printed_lo = min(ch.printed_num for ch in chapters)
            printed_hi = max(ch.printed_num for ch in chapters)
            logger.warning(
                "append: novel %d printed range %d-%d collides with existing "
                "chapters (max=%d); falling back to offset-mode insertion at "
                "%d-%d. The append was probably a re-upload of existing "
                "chapters; the new rows do NOT overwrite the originals.",
                novel_id, printed_lo, printed_hi, offset,
                rows[0][1], rows[-1][1],
            )
        await conn.executemany(
            "INSERT INTO chapters (novel_id, chapter_num, title_zh, original_text, status) "
            "VALUES (?, ?, ?, ?, 'pending')",
            rows,
        )
        await conn.commit()
    except Exception:
        # Exception (not BaseException) so KeyboardInterrupt / SystemExit
        # propagate without being intercepted for a cooperative rollback —
        # signal-driven shutdown is going away anyway. Matches the pattern
        # from the Phase-0 _drop_glossary_category_check fix.
        await conn.rollback()
        raise
    return (rows[0][1] if rows else offset + 1), collision
