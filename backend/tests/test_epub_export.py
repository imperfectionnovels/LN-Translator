"""Initiative 7 — EPUB export via GET /api/novels/{id}/download?format=epub.

Inserts novel + chapter rows directly into the test DB (skipping the
translator), then hits the download endpoint and re-parses the response
bytes with ebooklib to assert structure / content / cover round-tripped.
"""

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import SCHEMA
from backend.main import app

DB_PATH = Path(os.environ["DB_PATH"])


# Tiny valid PNG (1x1 red pixel) — same constant the EPUB import test uses.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x00\x03\x00\x01\x88\x00\xbf\xf0\xd6\x06\x00\x00\x00\x00IEND"
    b"\xaeB`\x82"
)


@pytest.fixture
def client(monkeypatch, tmp_path):
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    monkeypatch.setattr("backend.services.covers.USER_DATA_ROOT", tmp_path)
    monkeypatch.setattr(
        "backend.services.covers._COVERS_DIR", tmp_path / "covers"
    )
    monkeypatch.setattr("backend.routes.novels.resolve_cover_path",
                        lambda stored: (tmp_path / stored).resolve() if stored else None)

    async def _noop_run(novel_id, chapter_id):
        return

    monkeypatch.setattr("backend.services.queue._run_translate", _noop_run)
    return TestClient(app)


def _insert_novel(
    title: str,
    *,
    author: str | None = None,
    synopsis: str | None = None,
    cover_image_path: str | None = None,
) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "INSERT INTO novels (title, source_type, author, synopsis, "
            "cover_image_path) VALUES (?, 'paste', ?, ?, ?)",
            (title, author, synopsis, cover_image_path),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _insert_chapter(
    novel_id: int,
    chapter_num: int,
    title_en: str,
    translated_text: str,
    *,
    refined_text: str | None = None,
    refinement_status: str = "none",
) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, title_zh, title_en, "
            "original_text, translated_text, refined_text, refinement_status, "
            "status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'done')",
            (
                novel_id,
                chapter_num,
                None,
                title_en,
                "original text placeholder",
                translated_text,
                refined_text,
                refinement_status,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _parse_epub(data: bytes):
    import tempfile

    from ebooklib import epub  # type: ignore[attr-defined]
    fd, name = tempfile.mkstemp(suffix=".epub")
    os.close(fd)
    try:
        with open(name, "wb") as f:
            f.write(data)
        return epub.read_epub(name)
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def test_epub_export_round_trip(client: TestClient) -> None:
    novel_id = _insert_novel(
        "The Round Trip", author="Author Name", synopsis="A test novel."
    )
    _insert_chapter(
        novel_id, 1, "Chapter 1: The First",
        "First chapter body.\n\nSecond paragraph.",
    )
    _insert_chapter(
        novel_id, 2, "Chapter 2: The Second",
        "Second chapter body.",
    )
    r = client.get(f"/api/novels/{novel_id}/download?format=epub")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/epub+zip")
    book = _parse_epub(r.content)

    # Title + creator round-tripped
    titles = book.get_metadata("DC", "title")
    creators = book.get_metadata("DC", "creator")
    assert any("Round Trip" in t[0] for t in titles), titles
    assert any("Author Name" in c[0] for c in creators), creators

    # Synopsis became dc:description
    descs = book.get_metadata("DC", "description")
    assert any("A test novel" in d[0] for d in descs), descs

    # Both chapters in the spine
    from ebooklib import ITEM_DOCUMENT  # type: ignore[attr-defined]
    docs = list(book.get_items_of_type(ITEM_DOCUMENT))
    chapter_docs = [d for d in docs if d.get_name().startswith("chap_")]
    assert len(chapter_docs) == 2, [d.get_id() for d in chapter_docs]

    body0 = chapter_docs[0].get_content().decode("utf-8")
    assert "Chapter 1: The First" in body0
    assert "First chapter body" in body0
    assert "Second paragraph" in body0


def test_epub_export_prefers_refined_text(client: TestClient) -> None:
    novel_id = _insert_novel("Refined")
    _insert_chapter(
        novel_id, 1, "Chapter 1: First",
        translated_text="Draft text.",
        refined_text="Refined polished text.",
        refinement_status="done",
    )
    r = client.get(f"/api/novels/{novel_id}/download?format=epub")
    assert r.status_code == 200
    book = _parse_epub(r.content)
    from ebooklib import ITEM_DOCUMENT  # type: ignore[attr-defined]
    docs = list(book.get_items_of_type(ITEM_DOCUMENT))
    bodies = "\n".join(d.get_content().decode("utf-8") for d in docs)
    assert "Refined polished text" in bodies
    assert "Draft text" not in bodies


def test_epub_export_omits_pending_chapters(client: TestClient) -> None:
    novel_id = _insert_novel("Mixed")
    _insert_chapter(novel_id, 1, "Done", "Translated body.")
    # Pending chapter — status='pending', no translated_text
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO chapters (novel_id, chapter_num, title_en, "
        "original_text, status) VALUES (?, 2, 'Pending', 'src', 'pending')",
        (novel_id,),
    )
    conn.commit()
    conn.close()

    r = client.get(f"/api/novels/{novel_id}/download?format=epub")
    assert r.status_code == 200
    book = _parse_epub(r.content)
    from ebooklib import ITEM_DOCUMENT  # type: ignore[attr-defined]
    docs = list(book.get_items_of_type(ITEM_DOCUMENT))
    chapter_docs = [d for d in docs if d.get_name().startswith("chap_")]
    assert len(chapter_docs) == 1
    body = chapter_docs[0].get_content().decode("utf-8")
    assert "Pending" not in body
    assert "Translated body" in body


def test_epub_export_no_chapters_returns_400(client: TestClient) -> None:
    novel_id = _insert_novel("Empty")
    r = client.get(f"/api/novels/{novel_id}/download?format=epub")
    assert r.status_code == 400
    assert "no translated chapters" in r.json()["detail"]


def test_epub_export_unknown_format_returns_400(client: TestClient) -> None:
    novel_id = _insert_novel("Unknown")
    r = client.get(f"/api/novels/{novel_id}/download?format=mobi")
    assert r.status_code == 400


def test_epub_export_embeds_cover(client: TestClient, tmp_path) -> None:
    covers_dir = tmp_path / "covers"
    covers_dir.mkdir()
    cover_disk = covers_dir / "1.png"
    cover_disk.write_bytes(_TINY_PNG)
    novel_id = _insert_novel("With Cover", cover_image_path="covers/1.png")
    _insert_chapter(novel_id, 1, "Chapter 1: One", "Body of chapter one.")

    r = client.get(f"/api/novels/{novel_id}/download?format=epub")
    assert r.status_code == 200
    book = _parse_epub(r.content)
    from ebooklib import ITEM_COVER, ITEM_IMAGE  # type: ignore[attr-defined]
    # ebooklib stores cover as either ITEM_COVER or as the
    # property=cover-image item under ITEM_IMAGE.
    images = list(book.get_items_of_type(ITEM_IMAGE)) + list(
        book.get_items_of_type(ITEM_COVER)
    )
    assert images, "no images / cover items in exported EPUB"
    cover_bytes = b""
    for item in images:
        if b"PNG" in (item.get_content() or b"")[:8] or b"\x89PNG" in (item.get_content() or b"")[:8]:
            cover_bytes = item.get_content()
            break
    assert cover_bytes == _TINY_PNG, "cover round-trip mismatch"
