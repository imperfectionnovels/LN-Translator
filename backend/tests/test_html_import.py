"""Initiative 7 — HTML import via /api/translate/upload.

Posts a raw .html fixture and verifies trafilatura extracts the narrative
body, drops nav / footer / script blocks, and the parsed chapters survive
the round-trip into the chapters table.
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

    monkeypatch.setattr("backend.services.covers.USER_DATA_ROOT", tmp_path)
    monkeypatch.setattr(
        "backend.services.covers._COVERS_DIR", tmp_path / "covers"
    )

    async def _noop_run(novel_id, chapter_id):
        return

    monkeypatch.setattr("backend.services.queue._run_translate", _noop_run)
    return TestClient(app)


_HTML_FIXTURE = """<!DOCTYPE html>
<html><head><title>Sample Chapter</title></head>
<body>
<nav><a href="/">home</a> | <a href="/toc">toc</a></nav>
<script>console.log('drop me');</script>
<article>
<h1>Chapter 1: The Open Road</h1>
<p>The road stretched ahead of him, a ribbon of dust under the harsh sun.
He had walked this path before, in dreams and in memory, but never in the
flesh until now.</p>
<p>His companion paused at the crest of the hill and looked back, as if
weighing whether to follow or to turn aside. Silence settled between them,
broken only by the distant cry of a hawk.</p>
<h1>Chapter 2: The Crossing</h1>
<p>By the time they reached the river the moon was full and the water ran
silver beneath it. He set down his pack and knelt at the bank, cupping
water in his hands and drinking before he spoke.</p>
<p>"We should rest here," he said. "The crossing will be hard, and we
shouldn't attempt it tired."</p>
</article>
<footer>(c) 2026 Some Website</footer>
</body></html>
"""


def test_html_import_via_upload(client: TestClient) -> None:
    r = client.post(
        "/api/translate/upload",
        data={"title": "HTML Test"},
        files={"file": ("sample.html", BytesIO(_HTML_FIXTURE.encode("utf-8")), "text/html")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_type"] == "html"
    assert body["detected_encoding"] == "html"
    novel_id = body["novel_id"]

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
    # Chapters short enough that the parser MIGHT merge — assert at least one
    # chapter exists and that the extracted prose made it in but the nav /
    # footer / script did not.
    assert rows, rows
    full = "\n".join(r["original_text"] or "" for r in rows)
    assert "ribbon of dust" in full
    assert "console.log" not in full
    assert "Some Website" not in full


def test_html_import_htm_extension_accepted(client: TestClient) -> None:
    """Bare ASCII .htm files (legacy DOS-style filenames) should work."""
    r = client.post(
        "/api/translate/upload",
        data={"title": "Legacy"},
        files={"file": ("legacy.htm", BytesIO(_HTML_FIXTURE.encode("utf-8")), "text/html")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["source_type"] == "html"


def test_html_import_unsupported_extension_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/translate/upload",
        data={"title": "Bad"},
        files={"file": ("evil.pdf", BytesIO(b"%PDF-1.4"), "application/pdf")},
    )
    assert r.status_code == 400, r.text
    assert "unsupported" in r.json()["detail"].lower()
