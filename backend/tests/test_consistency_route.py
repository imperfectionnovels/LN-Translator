"""HTTP-level test for GET /novels/{id}/chapters/{n}/consistency.

Service behavior (fuzzy/glossary detection, live rendering, status contract)
is covered by test_consistency.py; this pins the route reshaping into the
ConsistencyFindings Pydantic response and the error/empty-state contract:
404 only for a missing chapter, never a 500 for pending chapters.
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


def _seed() -> int:
    """Two chapters share an identical source paragraph rendered two ways, and
    a locked glossary term is dropped from chapter 2. Returns novel_id."""
    src = "他取出了一件灵宝。"
    h = _hash(src)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO novels (title, source_type) VALUES ('N', 'paste')")
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        rows = [(1, "He took out a Spirit Treasure."),
                (2, "He took out a Spiritual Treasure.")]
        for ch_num, tgt in rows:
            conn.execute(
                "INSERT INTO chapters "
                "(novel_id, chapter_num, original_text, translated_text, "
                " title_en, status) VALUES (?, ?, ?, ?, ?, 'done')",
                (novel_id, ch_num, src, tgt, f"Chapter {ch_num}"),
            )
            chapter_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO tm_segments "
                "(novel_id, chapter_id, paragraph_index, source_text, "
                " target_text, source_hash) VALUES (?, ?, 0, ?, ?, ?)",
                (novel_id, chapter_id, src, tgt, h),
            )
        # A locked term whose zh is in the source but absent from ch2's English.
        conn.execute(
            "INSERT INTO glossary_entries "
            "(novel_id, term_zh, term_en, category, locked, auto_detected) "
            "VALUES (?, '灵宝', 'Numinous Treasure', 'item', 1, 0)",
            (novel_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return novel_id


def test_consistency_route_returns_findings(client):
    novel_id = _seed()
    r = client.get(f"/api/novels/{novel_id}/chapters/2/consistency")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    # The fuzzy tier flags the divergent rendering of the shared source.
    assert len(data["matches"]) == 1
    m = data["matches"][0]
    assert m["paragraph_index"] == 0
    assert m["others"][0]["chapter_num"] == 1
    assert m["others"][0]["exact"] is True
    assert m["others"][0]["target_text"] == "He took out a Spirit Treasure."
    # The glossary tier flags the dropped locked term.
    assert any(f["term_zh"] == "灵宝" and f["expected_en"] == "Numinous Treasure"
               for f in data["glossary_flags"])


def test_consistency_route_404_for_missing_chapter(client):
    novel_id = _seed()
    r = client.get(f"/api/novels/{novel_id}/chapters/999/consistency")
    assert r.status_code == 404


def test_consistency_route_pending_chapter_is_not_500(client):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO novels (title, source_type) VALUES ('N', 'paste')")
    novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO chapters (novel_id, chapter_num, original_text, status) "
        "VALUES (?, 1, '他取出了一件灵宝。', 'pending')",
        (novel_id,),
    )
    conn.commit()
    conn.close()
    r = client.get(f"/api/novels/{novel_id}/chapters/1/consistency")
    assert r.status_code == 200
    assert r.json()["status"] == "not_translated"
