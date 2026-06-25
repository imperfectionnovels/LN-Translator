"""Tests for the in-app learn-from-edits loop (stage + commit + ground-truth).

Seeds a chapter with captured style_edits that exercise a glossary casing change
(Spiritual Power -> spiritual power) and the exclamation-density brief signal,
then drives the two endpoints: stage derives the proposal without writing, commit
applies only confirmed ids (brief -> custom_style_brief, casing -> glossary entry
recased + locked, save_ground_truth -> ground_truth_edits row), and a forged id is
ignored.
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


def _seed() -> tuple[int, int]:
    """A novel + one done chapter, a locked glossary term, and two captured
    style edits: one recases the term, one strips three exclamation marks.
    Returns (novel_id, entry_id)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO novels (title, source_type) VALUES ('N', 'paste')")
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status) VALUES (?, 1, '源', 'His spiritual power surged.', 'done')",
            (novel_id,),
        )
        chapter_id = conn.execute(
            "SELECT id FROM chapters WHERE novel_id=? AND chapter_num=1", (novel_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO glossary_entries (novel_id, term_zh, term_en, category, "
            "locked, auto_detected) VALUES (?, '灵力', 'Spiritual Power', 'other', 1, 0)",
            (novel_id,),
        )
        entry_id = conn.execute(
            "SELECT id FROM glossary_entries WHERE novel_id=?", (novel_id,)
        ).fetchone()[0]
        edits = [
            ("His Spiritual Power surged.", "His spiritual power surged."),
            ("Stop! Now! Go!", "Stop. Now. Go."),
        ]
        for before, after in edits:
            conn.execute(
                "INSERT INTO style_edits (novel_id, chapter_id, before_text, after_text) "
                "VALUES (?, ?, ?, ?)",
                (novel_id, chapter_id, before, after),
            )
        conn.commit()
    finally:
        conn.close()
    return novel_id, entry_id


def test_stage_derives_proposal_without_writing(client):
    nid, entry_id = _seed()
    r = client.post(f"/api/novels/{nid}/chapters/1/learn-edits")
    assert r.status_code == 200
    p = r.json()
    assert p["captured_edits"] == 2
    # Exclamation signal present.
    assert any("xclamation" in b["text"] for b in p["brief"])
    # Casing proposal for the seeded term.
    gc = p["glossary_casing"]
    assert len(gc) == 1
    assert gc[0]["entry_id"] == entry_id
    assert gc[0]["term_en"] == "Spiritual Power"
    assert gc[0]["proposed_en"] == "spiritual power"
    # Nothing written yet.
    conn = sqlite3.connect(DB_PATH)
    try:
        brief = conn.execute(
            "SELECT custom_style_brief FROM novels WHERE id=?", (nid,)
        ).fetchone()[0]
        term = conn.execute(
            "SELECT term_en FROM glossary_entries WHERE id=?", (entry_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert not brief
    assert term == "Spiritual Power"


def test_commit_applies_confirmed_subset(client):
    nid, entry_id = _seed()
    body = {
        "brief": ["brief-0"],
        "glossary_casing": [f"gloss-{entry_id}"],
        "save_ground_truth": True,
    }
    r = client.post(f"/api/novels/{nid}/chapters/1/learn-edits/commit", json=body)
    assert r.status_code == 200
    res = r.json()
    assert res["applied_brief"] == 1
    assert res["applied_glossary"] == 1
    assert res["ground_truth_saved"] is True

    conn = sqlite3.connect(DB_PATH)
    try:
        brief = conn.execute(
            "SELECT custom_style_brief FROM novels WHERE id=?", (nid,)
        ).fetchone()[0]
        term_en, notes, locked = conn.execute(
            "SELECT term_en, notes, locked FROM glossary_entries WHERE id=?", (entry_id,)
        ).fetchone()
        gt = conn.execute(
            "SELECT edited_text, source FROM ground_truth_edits WHERE novel_id=?", (nid,)
        ).fetchone()
    finally:
        conn.close()
    assert brief and "xclamation" in brief
    assert term_en == "spiritual power"      # recased
    assert "lowercase" in (notes or "").lower()  # down-caser backstop noted
    assert locked == 1                        # lock-on-edit
    assert gt[0] == "His spiritual power surged."
    assert gt[1] == "draft"


def test_commit_forged_id_is_ignored(client):
    nid, entry_id = _seed()
    body = {"brief": ["brief-99"], "glossary_casing": ["gloss-999999"]}
    r = client.post(f"/api/novels/{nid}/chapters/1/learn-edits/commit", json=body)
    assert r.status_code == 200
    res = r.json()
    assert res["applied_brief"] == 0
    assert res["applied_glossary"] == 0


def test_missing_chapter_404(client):
    nid, _ = _seed()
    assert client.post(f"/api/novels/{nid}/chapters/99/learn-edits").status_code == 404
    assert client.post(
        f"/api/novels/{nid}/chapters/99/learn-edits/commit", json={}
    ).status_code == 404
