"""EPUB 3 export for translated novels (Initiative 7).

`build_epub(novel_row, chapters, cover)` returns a single bytes object —
the entire EPUB archive — that the download route hands off to a
StreamingResponse. We build the whole archive in memory because ebooklib's
write API needs a filesystem path; the in-memory roundtrip via tempfile
keeps the route streaming-friendly without exposing the temp path.

Source-of-truth per chapter follows the existing download convention:
`COALESCE(refined_text, translated_text)`. Chapters whose status isn't
'done' are omitted — exporting a partial draft is what `format=txt` is for;
EPUB is the "finished reading copy" output.

Format preservation: paragraphs only. Each chapter body splits on
`"\\n\\n"` and each chunk lands in a `<p>` tag. No inline em/strong/br
round-trip in v1 per the Initiative-7 minimal-scope reading; the model
rarely preserves inline emphasis through CN→EN translation anyway, and
adding inline format hints requires column migrations the export step
doesn't need to drive.
"""

from __future__ import annotations

import html
import logging
import os
import tempfile
from typing import Iterable

logger = logging.getLogger(__name__)


# Chapter body templates. Kept inline as string constants rather than
# Jinja templates — the substitution is two %s slots, not enough to
# justify a template-engine import.
_CHAPTER_HTML_HEAD = (
    # No XML declaration / DOCTYPE here — ebooklib's writer emits the EPUB
    # envelope around our body content and round-trip read parses through
    # lxml.html.document_fromstring which rejects unicode strings carrying
    # an XML encoding declaration.
    '<html xmlns="http://www.w3.org/1999/xhtml" '
    'lang="en" xml:lang="en">\n'
    '<head>\n'
    '<meta charset="utf-8"/>\n'
    '<title>{title}</title>\n'
    '<style>\n'
    # Double-braces here escape against str.format below — readers see
    # single-brace CSS as expected.
    'body {{ font-family: serif; line-height: 1.55; padding: 0 1em; }}\n'
    'h1 {{ font-weight: bold; margin: 1.5em 0 0.5em; text-align: center; }}\n'
    'p {{ margin: 0.5em 0; text-indent: 1.2em; }}\n'
    '</style>\n'
    '</head>\n'
    '<body>\n'
)
_CHAPTER_HTML_TAIL = "</body>\n</html>\n"


def _paragraphs_to_html(body: str) -> str:
    """Convert paragraph-split body text to a `<p>`-wrapped HTML fragment.
    HTML-escapes each paragraph so source punctuation/quotes can't injection
    the chapter file's own markup."""
    parts: list[str] = []
    for chunk in body.split("\n\n"):
        text = chunk.strip()
        if not text:
            continue
        # Within-paragraph single newlines become spaces — EPUB readers
        # typography expects flowing paragraphs, not hard wraps.
        text = text.replace("\n", " ")
        parts.append(f"<p>{html.escape(text)}</p>")
    return "\n".join(parts)


def _chapter_body_for(ch: dict) -> str | None:
    """Pick the canonical English for a chapter row, matching the existing
    download convention. Returns None for chapters that have no usable body
    (status != 'done' or both refined / translated empty)."""
    if ch.get("status") != "done":
        return None
    refined = ch.get("refined_text") or ""
    if ch.get("refinement_status") == "done" and refined:
        return refined
    translated = ch.get("translated_text") or ""
    return translated or None


def _chapter_title(ch: dict) -> str:
    return ch.get("title_en") or ch.get("title_zh") or f"Chapter {ch['chapter_num']}"


def _identifier_for(novel: dict) -> str:
    """Stable per-novel EPUB identifier. ebooklib requires a dc:identifier;
    we use a synthetic URN keyed on novel.id so re-downloads of the same
    novel keep the same identifier (Calibre treats matching IDs as the
    same book and overwrites cleanly)."""
    return f"urn:ln-translator:novel:{novel['id']}"


def build_epub(
    novel: dict,
    chapters: Iterable[dict],
    cover: tuple[bytes, str] | None = None,
) -> bytes:
    """Return EPUB 3 bytes for the novel. `novel` is the aiosqlite Row
    converted to a dict (or any mapping with the same keys); `chapters` is
    iterated in order. `cover` is an optional `(image_bytes, ext)` pair —
    when supplied, the cover lands in the spine as the first item and on
    the OPF metadata as the canonical cover image.

    Raises ValueError when no chapter is exportable. The download route
    converts this to HTTP 400 — exporting a wholly untranslated novel
    isn't actionable from the EPUB downloader; the user should translate
    chapters first.
    """
    try:
        from ebooklib import epub  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover — pyproject guarantees presence
        raise RuntimeError(f"ebooklib not installed: {e}") from e

    book = epub.EpubBook()
    book.set_identifier(_identifier_for(novel))
    book.set_title(novel.get("title") or "Untitled")
    book.set_language("en")
    creator = (novel.get("author") or "").strip() or "Unknown"
    book.add_author(creator)
    synopsis = (novel.get("synopsis") or "").strip()
    if synopsis:
        # ebooklib stores the description in OPF metadata; readers display
        # it in the book-info dialog. Limit to 4kb so a runaway synopsis
        # can't bloat the OPF.
        book.add_metadata("DC", "description", synopsis[:4000])

    if cover is not None:
        cover_bytes, cover_ext = cover
        ext_clean = cover_ext.lstrip(".").lower()
        if ext_clean in {"jpg", "jpeg"}:
            cover_filename = "cover.jpg"
        elif ext_clean in {"png", "gif", "webp"}:
            cover_filename = f"cover.{ext_clean}"
        else:
            cover_filename = "cover.jpg"
        book.set_cover(cover_filename, cover_bytes)

    epub_chapters: list = []
    spine: list = ["nav"]
    if cover is not None:
        # ebooklib's set_cover wires the cover into the spine automatically
        # under the id "cover"; explicitly prepend it so nav follows the
        # cover in reading order.
        spine = ["cover", "nav", *spine[1:]]
    toc_entries: list = []

    for ch in chapters:
        body = _chapter_body_for(ch)
        if body is None:
            continue
        title = _chapter_title(ch)
        # ebooklib uniqueness: EpubHtml ids must not collide.
        file_id = f"chap_{ch['chapter_num']}"
        item = epub.EpubHtml(
            title=title,
            file_name=f"{file_id}.xhtml",
            lang="en",
        )
        item.content = (
            _CHAPTER_HTML_HEAD.format(title=html.escape(title))
            + f"<h1>{html.escape(title)}</h1>\n"
            + _paragraphs_to_html(body)
            + "\n"
            + _CHAPTER_HTML_TAIL
        )
        book.add_item(item)
        epub_chapters.append(item)
        spine.append(item)
        toc_entries.append(item)

    if not epub_chapters:
        raise ValueError("no translated chapters to export")

    book.toc = tuple(toc_entries)
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # ebooklib insists on writing to a real file path. Round-trip through
    # a temp file so the downloader can hold the bytes.
    fd, tmp_name = tempfile.mkstemp(prefix="ln-export-", suffix=".epub")
    os.close(fd)
    try:
        epub.write_epub(tmp_name, book, {})
        with open(tmp_name, "rb") as f:
            data = f.read()
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass

    return data


__all__ = ["build_epub"]
