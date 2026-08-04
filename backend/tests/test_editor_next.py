"""CAT Phase 5: GET /api/novels/{id}/editor-next (the continue card feed).

Contracts:
  - a chapter "needs work" when it is untranslated (status != 'done') OR its
    segment store is missing OR any of its segments is non-confirmed;
  - search runs forward from `after`, then wraps to the beginning;
  - a fully confirmed chapter (done + store + all confirmed) is skipped;
  - next_chapter_num is None when every chapter is fully confirmed;
  - 404 when the novel does not exist.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DB_PATH = Path(os.environ["DB_PATH"])


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@pytest.fixture
def client(monkeypatch):
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


def _seed_novel(chapters: list[dict]) -> int:
    """chapters: dicts with chapter_num, status ('done'|'pending'), and
    optional segments: list of status strings, one row per entry."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for ch in chapters:
            conn.execute(
                "INSERT INTO chapters (novel_id, chapter_num, original_text, "
                "translated_text, status) VALUES (?, ?, ?, ?, ?)",
                (novel_id, ch["chapter_num"], "原文。", "Body.", ch["status"]),
            )
            chapter_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for i, seg_status in enumerate(ch.get("segments", [])):
                src = f"第{ch['chapter_num']}章第{i}段。"
                conn.execute(
                    "INSERT INTO chapter_segments (novel_id, chapter_id, "
                    "seg_index, source_text, source_hash, target_text, "
                    "machine_text, status, origin, aligned) "
                    "VALUES (?, ?, ?, ?, ?, 'Para.', 'Para.', ?, 'llm', 1)",
                    (novel_id, chapter_id, i, src, _hash(src), seg_status),
                )
        conn.commit()
        return novel_id
    finally:
        conn.close()


def _next(client, novel_id: int, after: int) -> int | None:
    resp = client.get(
        f"/api/novels/{novel_id}/editor-next", params={"after": after}
    )
    assert resp.status_code == 200
    return resp.json()["next_chapter_num"]


def test_next_prefers_first_unconfirmed_after(client):
    novel_id = _seed_novel([
        {"chapter_num": 1, "status": "done", "segments": ["confirmed"]},
        {"chapter_num": 2, "status": "done", "segments": ["confirmed", "machine"]},
        {"chapter_num": 3, "status": "done", "segments": ["edited"]},
    ])
    assert _next(client, novel_id, 1) == 2


def test_fully_confirmed_chapters_are_skipped(client):
    novel_id = _seed_novel([
        {"chapter_num": 1, "status": "done", "segments": ["confirmed"]},
        {"chapter_num": 2, "status": "done", "segments": ["confirmed", "confirmed"]},
        {"chapter_num": 3, "status": "done", "segments": ["machine"]},
    ])
    assert _next(client, novel_id, 1) == 3


def test_untranslated_and_storeless_chapters_need_work(client):
    novel_id = _seed_novel([
        {"chapter_num": 1, "status": "done", "segments": ["confirmed"]},
        # Done but no segment store yet: its segments would all be machine.
        {"chapter_num": 2, "status": "done"},
        {"chapter_num": 3, "status": "pending"},
    ])
    assert _next(client, novel_id, 1) == 2
    # An untranslated chapter also counts.
    assert _next(client, novel_id, 2) == 3


def test_wraps_to_the_beginning(client):
    novel_id = _seed_novel([
        {"chapter_num": 1, "status": "done", "segments": ["machine"]},
        {"chapter_num": 2, "status": "done", "segments": ["confirmed"]},
    ])
    assert _next(client, novel_id, 2) == 1


def test_none_when_everything_confirmed(client):
    novel_id = _seed_novel([
        {"chapter_num": 1, "status": "done", "segments": ["confirmed"]},
        {"chapter_num": 2, "status": "done", "segments": ["confirmed"]},
    ])
    assert _next(client, novel_id, 2) is None


def test_missing_novel_404s(client):
    resp = client.get("/api/novels/999/editor-next", params={"after": 1})
    assert resp.status_code == 404
