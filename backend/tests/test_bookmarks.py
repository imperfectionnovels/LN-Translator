"""HTTP-level tests for routes/bookmarks.py (Initiative 2).

Pins the three reader-facing bookmark endpoints end-to-end against a real
temp DB (no translation/queue work runs — bookmarks are pure CRUD over the
`bookmarks` table plus a chapter JOIN):

  * GET    /api/novels/{id}/bookmarks                         — list in reading order
  * POST   /api/novels/{id}/chapters/{n}/bookmarks            — create (201)
  * DELETE /api/bookmarks/{id}                                — remove (404 when absent)

A novel + two chapters are seeded directly into SQLite; bookmarks are then
created/listed/deleted through the HTTP surface so the chapter_id resolution,
note normalization, reading-order sort, and 404 paths are all exercised.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DB_PATH = Path(os.environ["DB_PATH"])


@pytest.fixture
def client(monkeypatch):
    """TestClient with the startup probe + queue drain stubbed so the
    lifespan never reaches for a real translator. Entering the context
    manager runs init_db() against the fresh temp DB."""
    if DB_PATH.exists():
        DB_PATH.unlink()

    async def _no_probe(default_provider):
        return None

    async def _no_drain():
        return None

    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)

    from backend.main import app

    with TestClient(app) as c:
        yield c


def _seed_novel_with_chapters() -> int:
    """Insert one novel with chapters 1 and 2. Returns novel_id. The
    explicit commit is required — sqlite3 deferred transactions roll back
    on close() without it."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('BM Novel', 'paste')"
        )
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.executemany(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, status) "
            "VALUES (?, ?, '原文', 'done')",
            [(novel_id, 1), (novel_id, 2)],
        )
        conn.commit()
        return novel_id
    finally:
        conn.close()


def test_create_bookmark_returns_201_with_denormalized_chapter_num(client):
    novel_id = _seed_novel_with_chapters()
    resp = client.post(
        f"/api/novels/{novel_id}/chapters/1/bookmarks",
        json={"paragraph_index": 3, "note": "  a good line  "},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["novel_id"] == novel_id
    assert body["chapter_num"] == 1            # denormalized from the JOIN
    assert body["paragraph_index"] == 3
    assert body["note"] == "a good line"       # whitespace stripped
    assert isinstance(body["chapter_id"], int)
    assert body["id"] > 0


def test_create_bookmark_chapter_level_blank_note_becomes_null(client):
    novel_id = _seed_novel_with_chapters()
    # No paragraph_index (chapter-level) and a whitespace-only note.
    resp = client.post(
        f"/api/novels/{novel_id}/chapters/2/bookmarks",
        json={"note": "   "},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["paragraph_index"] is None
    assert body["note"] is None                # "   ".strip() -> None


def test_create_bookmark_unknown_chapter_404(client):
    novel_id = _seed_novel_with_chapters()
    resp = client.post(
        f"/api/novels/{novel_id}/chapters/99/bookmarks",
        json={"paragraph_index": 0},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "chapter not found"


def test_list_bookmarks_in_reading_order(client):
    novel_id = _seed_novel_with_chapters()
    # Insert out of reading order: ch2 first, then ch1 para 5, then ch1 para 0.
    client.post(
        f"/api/novels/{novel_id}/chapters/2/bookmarks",
        json={"paragraph_index": 0},
    )
    client.post(
        f"/api/novels/{novel_id}/chapters/1/bookmarks",
        json={"paragraph_index": 5},
    )
    client.post(
        f"/api/novels/{novel_id}/chapters/1/bookmarks",
        json={"paragraph_index": 0},
    )
    resp = client.get(f"/api/novels/{novel_id}/bookmarks")
    assert resp.status_code == 200
    rows = resp.json()
    # Ordered by (chapter_num, paragraph_index, id): ch1/0, ch1/5, ch2/0.
    assert [(r["chapter_num"], r["paragraph_index"]) for r in rows] == [
        (1, 0),
        (1, 5),
        (2, 0),
    ]


def test_list_bookmarks_empty_for_novel_without_any(client):
    novel_id = _seed_novel_with_chapters()
    resp = client.get(f"/api/novels/{novel_id}/bookmarks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_delete_bookmark_then_double_delete_404(client):
    novel_id = _seed_novel_with_chapters()
    created = client.post(
        f"/api/novels/{novel_id}/chapters/1/bookmarks",
        json={"paragraph_index": 1},
    ).json()
    bookmark_id = created["id"]

    first = client.delete(f"/api/bookmarks/{bookmark_id}")
    assert first.status_code == 200
    assert first.json() == {"ok": True}

    # Gone from the list...
    assert client.get(f"/api/novels/{novel_id}/bookmarks").json() == []

    # ...and a second delete is a clean 404, not a silent success.
    second = client.delete(f"/api/bookmarks/{bookmark_id}")
    assert second.status_code == 404
    assert second.json()["detail"] == "bookmark not found"
