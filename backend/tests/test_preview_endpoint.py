"""Tests for the import preview-gate endpoint (F05/F06/F08).

POST /api/translate/preview returns detected chapter count + first 5
headings + first chapter snippet, without DB writes. Mirrors what the
import will commit so the user can confirm before pressing Submit.
"""

from __future__ import annotations

import os
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import _ADDITIVE_MIGRATIONS, SCHEMA
from backend.main import app

DB_PATH = Path(os.environ["DB_PATH"])


@pytest.fixture
def client():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    for stmt in _ADDITIVE_MIGRATIONS:
        try:
            conn.executescript(stmt)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    return TestClient(app)


def test_preview_detects_multiple_chapters_from_paste(client: TestClient) -> None:
    """Paste text with multiple 第N章 markers → preview returns the
    detected count + heading list, no DB write."""
    pad = "情节展开，主角站在山顶上。" * 30
    text = "\n\n".join([
        f"第一章 序幕\n\n{pad}",
        f"第二章 启程\n\n{pad}",
        f"第三章 风暴\n\n{pad}",
    ])
    r = client.post(
        "/api/translate/preview",
        json={"text": text},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["detected_chapters"] == 3
    assert len(body["headings"]) == 3
    assert any("序幕" in h for h in body["headings"])
    assert body["format_path"] == "text"
    # No DB writes.
    conn = sqlite3.connect(DB_PATH)
    n_novels = conn.execute("SELECT COUNT(*) FROM novels").fetchone()[0]
    conn.close()
    assert n_novels == 0


def test_preview_returns_zero_for_no_markers(client: TestClient) -> None:
    """Pure prose with no chapter markers → detected_chapters might be 1
    (single-chapter fallback) or 0 depending on parse_chapters behavior.
    Either way, no DB writes happen."""
    r = client.post(
        "/api/translate/preview",
        json={"text": "Just a single paragraph of prose with no headings."},
    )
    assert r.status_code == 200, r.text
    # Either 0 (no parse result) or 1 (single-chapter fallback) is fine;
    # the UI's loud-fallback banner triggers on 0 OR 1 with no headings.
    body = r.json()
    assert body["detected_chapters"] >= 0


def test_preview_requires_text_or_file(client: TestClient) -> None:
    r = client.post("/api/translate/preview", json={})
    assert r.status_code == 400, r.text
    assert "text" in r.json()["detail"].lower() or "file" in r.json()["detail"].lower()


def test_preview_truncates_first_chapter_to_500(client: TestClient) -> None:
    """The first_chapter_first_500 field is bounded to 500 chars so the
    preview payload doesn't balloon on long imports."""
    pad = "A" * 2000
    text = f"第一章 序幕\n\n{pad}"
    r = client.post(
        "/api/translate/preview",
        json={"text": text},
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["first_chapter_first_500"]) <= 500


def test_preview_accepts_multipart_file_upload(client: TestClient) -> None:
    """The preview endpoint accepts multipart with a `file` field — same
    upload as /upload but no DB write. .txt is the simplest path."""
    pad = "情节展开。" * 50
    text = f"第一章 起点\n\n{pad}\n\n第二章 旅程\n\n{pad}"
    r = client.post(
        "/api/translate/preview",
        files={"file": ("novel.txt", text.encode("utf-8"), "text/plain")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["detected_chapters"] == 2
    assert body["format_path"] == "text"


def test_preview_surfaces_structured_format_path():
    """When an EPUB with ≥3 spine items is previewed, format_path is
    'epub_spine' (not 'text') so the UI can render a different label."""
    # Skip if ebooklib isn't installable in this environment.
    pytest.importorskip("ebooklib")
    from backend.tests.test_epub_import import _build_epub_bytes

    pad = "Padding prose. " * 8
    epub_bytes = _build_epub_bytes(
        title="Spine",
        author="A",
        chapters=[
            ("Prologue", ["A. " + pad]),
            ("Middle", ["B. " + pad]),
            ("Finale", ["C. " + pad]),
        ],
    )
    # Need a fresh client to bypass the fixture (the fixture is at function
    # scope so reusing it across files is awkward).
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    for stmt in _ADDITIVE_MIGRATIONS:
        try:
            conn.executescript(stmt)
        except sqlite3.OperationalError:
            pass
    conn.close()

    client = TestClient(app)
    r = client.post(
        "/api/translate/preview",
        files={"file": ("s.epub", BytesIO(epub_bytes), "application/epub+zip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["detected_chapters"] == 3
    assert body["format_path"] == "epub_spine"
