"""HTTP-level tests for routes/tm.py (the concordance endpoint).

The service-layer behavior (alignment, concordance search, inconsistency
detection) is covered by test_tm.py; this pins the route-layer reshaping
into the ConcordanceHit Pydantic response, which test_tm.py does not
exercise. (The old /tm/inconsistencies route tests died with that route,
2026-07-30.)

tm_segments rows are seeded directly (source_hash = sha256(source)[:16],
matching services/tm.py::_hash_source) so no alignment/translation runs.
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


def _seed_segments() -> int:
    """One novel, two chapters, one aligned segment each. Returns novel_id."""
    rows = [
        (1, "他大笑。", "He laughed loudly."),
        (2, "他叹了口气。", "He sighed."),
    ]
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('TM Novel', 'paste')"
        )
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for ch_num, src, tgt in rows:
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
                (novel_id, chapter_id, src, tgt, _hash(src)),
            )
        conn.commit()
        return novel_id
    finally:
        conn.close()


def test_concordance_route_returns_full_hit_shape(client):
    novel_id = _seed_segments()
    resp = client.get(
        f"/api/novels/{novel_id}/tm/concordance", params={"q": "laughed"}
    )
    assert resp.status_code == 200
    hits = resp.json()
    assert len(hits) == 1
    hit = hits[0]
    # ConcordanceHit shape is fully populated. `status` is the CAT Phase 5
    # additive provenance field: None on a legacy tm_segments hit.
    assert set(hit) == {
        "chapter_id", "chapter_num", "chapter_title_en",
        "paragraph_index", "source_text", "target_text", "matched_side",
        "status",
    }
    assert hit["chapter_num"] == 1
    assert hit["chapter_title_en"] == "Chapter 1"
    assert hit["paragraph_index"] == 0
    assert hit["source_text"] == "他大笑。"
    assert hit["target_text"] == "He laughed loudly."
    assert hit["matched_side"] == "target"
    assert hit["status"] is None


def _seed_segment_row(
    novel_id: int, chapter_num: int, seg_index: int,
    src: str, tgt: str, status: str = "confirmed",
) -> None:
    """Insert a chapter_segments row for an existing chapter (direct SQL is
    fine in a test; production code goes through services/segments.py)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        chapter_id = conn.execute(
            "SELECT id FROM chapters WHERE novel_id = ? AND chapter_num = ?",
            (novel_id, chapter_num),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO chapter_segments (novel_id, chapter_id, seg_index, "
            "source_text, source_hash, target_text, machine_text, status, "
            "origin, aligned) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'human', 1)",
            (novel_id, chapter_id, seg_index, src, _hash(src), tgt, tgt, status),
        )
        conn.commit()
    finally:
        conn.close()


def test_concordance_prefers_segments_with_status_and_tm_fallback(client):
    """CAT Phase 5: a segment-covered chapter serves its chapter_segments
    rows (status-carrying, current text) and its stale tm_segments rows are
    suppressed; a chapter with no segment rows still serves via the legacy
    tm fallback with status None."""
    novel_id = _seed_segments()
    # Chapter 1 gains a segment store whose text superseded the tm row:
    # the concordance must show the segment rendering, not the stale tm one.
    _seed_segment_row(
        novel_id, 1, 0, "他大笑。", "He laughed thunderously.",
        status="confirmed",
    )
    resp = client.get(
        f"/api/novels/{novel_id}/tm/concordance", params={"q": "laughed"}
    )
    assert resp.status_code == 200
    hits = resp.json()
    assert len(hits) == 1
    assert hits[0]["target_text"] == "He laughed thunderously."
    assert hits[0]["status"] == "confirmed"

    # Chapter 2 has no segment rows: the tm fallback still serves it.
    resp = client.get(
        f"/api/novels/{novel_id}/tm/concordance", params={"q": "sighed"}
    )
    hits = resp.json()
    assert len(hits) == 1
    assert hits[0]["chapter_num"] == 2
    assert hits[0]["status"] is None


def test_concordance_merges_in_reading_order(client):
    """Segment hits and legacy tm hits interleave sorted by chapter."""
    novel_id = _seed_segments()
    # Both chapters' targets match "He "; chapter 1 is segment-covered with
    # an edited row, chapter 2 stays legacy.
    _seed_segment_row(
        novel_id, 1, 0, "他大笑。", "He laughed loudly.", status="edited",
    )
    resp = client.get(
        f"/api/novels/{novel_id}/tm/concordance", params={"q": "He "}
    )
    hits = resp.json()
    assert [(h["chapter_num"], h["status"]) for h in hits] == [
        (1, "edited"), (2, None),
    ]


def test_concordance_route_side_filter_and_empty(client):
    novel_id = _seed_segments()
    # Source-side query on a source-only string.
    resp = client.get(
        f"/api/novels/{novel_id}/tm/concordance",
        params={"q": "叹了口气", "side": "source"},
    )
    assert resp.status_code == 200
    hits = resp.json()
    assert [h["matched_side"] for h in hits] == ["source"]
    assert hits[0]["chapter_num"] == 2

    # The same source-only string restricted to target matches nothing.
    resp = client.get(
        f"/api/novels/{novel_id}/tm/concordance",
        params={"q": "叹了口气", "side": "target"},
    )
    assert resp.status_code == 200
    assert resp.json() == []
