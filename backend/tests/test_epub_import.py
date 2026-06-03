"""Initiative 7 — EPUB import via /api/translate/upload.

Builds minimal EPUB fixtures in-memory via ebooklib and posts them to the
upload endpoint. Verifies:
- chapters are parsed in spine order
- xhtml <h1> headings become chapter titles
- an embedded cover image lands on disk + in novels.cover_image_path
- a corrupted .epub yields a 400, not a 500
- the bulk endpoint rejects .epub explicitly
"""

import os
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import SCHEMA
from backend.main import app

DB_PATH = Path(os.environ["DB_PATH"])


@pytest.fixture
def client(monkeypatch, tmp_path):
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    # Cover writes hit USER_DATA_ROOT — re-point to a temp dir so the test
    # doesn't pollute real %APPDATA%.
    monkeypatch.setattr("backend.services.covers.USER_DATA_ROOT", tmp_path)
    monkeypatch.setattr(
        "backend.services.covers._COVERS_DIR", tmp_path / "covers"
    )

    async def _noop_run(novel_id, chapter_id):
        return

    monkeypatch.setattr("backend.services.queue._run_translate", _noop_run)
    return TestClient(app)


def _build_epub_bytes(
    title: str,
    author: str | None,
    chapters: list[tuple[str, list[str]]],
    cover: tuple[bytes, str] | None = None,
) -> bytes:
    """Build a minimal valid EPUB 3 archive in-memory and return its bytes.
    `chapters` is `[(chapter_title, [paragraph, paragraph, ...]), ...]`.
    `cover` is `(image_bytes, ext)`."""
    from ebooklib import epub  # imported lazily so test collection doesn't
    # fail on a system without ebooklib (defensive — pyproject + Block 1
    # guarantee presence)

    book = epub.EpubBook()
    book.set_identifier(f"urn:test:{title}")
    book.set_title(title)
    book.set_language("en")
    if author:
        book.add_author(author)
    if cover is not None:
        cover_bytes, ext = cover
        book.set_cover(f"cover.{ext}", cover_bytes)

    items: list = []
    for i, (ch_title, paragraphs) in enumerate(chapters, start=1):
        para_html = "\n".join(f"<p>{p}</p>" for p in paragraphs)
        body = (
            f"<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            f"<h1>{ch_title}</h1>{para_html}</body></html>"
        )
        item = epub.EpubHtml(
            title=ch_title, file_name=f"chap_{i}.xhtml", lang="en"
        )
        item.content = body
        book.add_item(item)
        items.append(item)

    book.toc = tuple(items)
    book.spine = ["nav", *items]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    import os
    import tempfile

    fd, tmp_name = tempfile.mkstemp(prefix="epub-fixture-", suffix=".epub")
    os.close(fd)
    try:
        epub.write_epub(tmp_name, book, {})
        with open(tmp_name, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


# A tiny valid PNG (1x1 red pixel) for cover-extraction tests.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x00\x03\x00\x01\x88\x00\xbf\xf0\xd6\x06\x00\x00\x00\x00IEND"
    b"\xaeB`\x82"
)


def test_epub_import_two_chapters_creates_novel(client: TestClient) -> None:
    # Each chapter body must exceed MIN_CHAPTER_CHARS (=200) or the parser
    # merges short chapters forward. Pad with prose so the parser splits.
    pad = "The protagonist walked through the courtyard. " * 6
    epub_bytes = _build_epub_bytes(
        title="The Test Novel",
        author="Test Author",
        chapters=[
            (
                "Chapter 1: The Beginning",
                ["First paragraph. " + pad, "Second paragraph. " + pad],
            ),
            (
                "Chapter 2: The Middle",
                ["Body of chapter two. " + pad],
            ),
        ],
    )
    r = client.post(
        "/api/translate/upload",
        data={"title": "Test Novel"},
        files={"file": ("test.epub", BytesIO(epub_bytes), "application/epub+zip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_type"] == "epub"
    assert body["detected_encoding"] == "epub"
    assert body["cover_extracted"] is False
    novel_id = body["novel_id"]

    # Confirm both chapters landed in spine order with their titles attached
    # as headings the parser detected.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = list(
        conn.execute(
            "SELECT chapter_num, title_zh, original_text FROM chapters "
            "WHERE novel_id = ? ORDER BY chapter_num",
            (novel_id,),
        )
    )
    conn.close()
    assert len(rows) == 2, rows
    # Titles preserved in title_zh (the parser strips heading from body)
    assert "Chapter 1" in (rows[0]["title_zh"] or "")
    assert "The Beginning" in (rows[0]["title_zh"] or "")
    # First-chapter body still includes the prose
    assert "First paragraph" in rows[0]["original_text"]
    assert "Body of chapter two" in rows[1]["original_text"]


def test_epub_import_extracts_cover(client: TestClient, tmp_path) -> None:
    pad = "Some prose to push the chapter over MIN_CHAPTER_CHARS. " * 8
    epub_bytes = _build_epub_bytes(
        title="Covered",
        author="Coverer",
        chapters=[("Chapter 1: Only One", ["The body. " + pad])],
        cover=(_TINY_PNG, "png"),
    )
    r = client.post(
        "/api/translate/upload",
        data={"title": "Covered Novel"},
        files={"file": ("c.epub", BytesIO(epub_bytes), "application/epub+zip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cover_extracted"] is True
    novel_id = body["novel_id"]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT cover_image_path FROM novels WHERE id = ?", (novel_id,)
    ).fetchone()
    conn.close()
    assert row["cover_image_path"], row["cover_image_path"]
    # Cover landed under the temp covers dir keyed on novel_id
    cover_disk = tmp_path / row["cover_image_path"]
    assert cover_disk.is_file()
    assert cover_disk.read_bytes() == _TINY_PNG


def test_epub_import_rejects_corrupt_archive(client: TestClient) -> None:
    # Not a valid ZIP — ebooklib's read_epub raises; the route folds it to 400.
    r = client.post(
        "/api/translate/upload",
        data={"title": "Bad"},
        files={"file": ("broken.epub", BytesIO(b"not actually an epub"), "application/epub+zip")},
    )
    assert r.status_code == 400, r.text
    assert "could not parse .epub" in r.json()["detail"]


def test_bulk_endpoint_rejects_epub(client: TestClient) -> None:
    epub_bytes = _build_epub_bytes(
        title="X",
        author=None,
        chapters=[("Chapter 1", ["body"])],
    )
    r = client.post(
        "/api/translate/bulk",
        data={"title": "Y"},
        files=[("files", ("a.epub", BytesIO(epub_bytes), "application/epub+zip"))],
    )
    assert r.status_code == 400, r.text
    assert ".txt" in r.json()["detail"].lower() or "epub" in r.json()["detail"].lower()


# ---- F07 (2026-05-25): spine-as-chapter structural path ---------------------

def test_epub_import_three_chapters_uses_spine_path(client: TestClient) -> None:
    """EPUB with ≥3 spine items goes through the structural extractor:
    each spine item becomes one chapter; the text-blob heading-regex
    fallback is not consulted. Heading text is taken from the spine
    item's first <h1>, not from `parse_chapters` heading-detection."""
    pad = "Some prose to push the chapter over MIN_CHAPTER_CHARS. " * 8
    epub_bytes = _build_epub_bytes(
        title="Spine Test",
        author="A",
        chapters=[
            ("Prologue", ["First chapter body. " + pad]),
            ("Middle", ["Second chapter body. " + pad]),
            ("Finale", ["Third chapter body. " + pad]),
        ],
    )
    r = client.post(
        "/api/translate/upload",
        data={"title": "Spine"},
        files={"file": ("spine.epub", BytesIO(epub_bytes), "application/epub+zip")},
    )
    assert r.status_code == 200, r.text
    novel_id = r.json()["novel_id"]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = list(
        conn.execute(
            "SELECT chapter_num, title_zh, original_text FROM chapters "
            "WHERE novel_id = ? ORDER BY chapter_num",
            (novel_id,),
        )
    )
    conn.close()
    # 3 spine items + the EPUB's `nav` is in the spine too, but nav has
    # no body content and gets dropped — should land 3 chapters.
    assert len(rows) == 3, [dict(r) for r in rows]
    titles = [r["title_zh"] for r in rows]
    assert "Prologue" in titles
    assert "Middle" in titles
    assert "Finale" in titles


def test_epub_spine_extractor_threshold_falls_back_under_three():
    """Under the spine-min-items threshold, the spine extractor returns
    None so the upload route uses the legacy text-blob path."""
    from io import BytesIO

    from backend.services.uploads import decode_epub

    pad = "Padding prose to clear MIN_CHAPTER_CHARS. " * 8
    epub_bytes = _build_epub_bytes(
        title="Single",
        author="A",
        chapters=[("Chapter 1: Only", ["The body. " + pad])],
    )

    class _DummyFile:
        # Match enough of UploadFile's surface for decode_epub to read it.
        size = len(epub_bytes)
        filename = "single.epub"
        _stream = BytesIO(epub_bytes)
        async def read(self, n: int) -> bytes:
            return self._stream.read(n)

    import asyncio
    decoded = asyncio.run(decode_epub(_DummyFile()))
    assert decoded.pre_parsed_chapters is None
    assert decoded.text  # text-blob is still populated for the fallback path
