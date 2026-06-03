"""HTTP-level tests for routes/tm.py.

The service-layer behavior (alignment, concordance, find_inconsistencies) is
covered by test_tm.py; this pins the route-layer reshaping into the nested
Pydantic response (InconsistencyGroup -> InconsistencyRendering ->
ConcordanceChapterMeta), which test_tm.py does not exercise.

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


def _seed_inconsistency() -> int:
    """One novel, three chapters; the same source paragraph rendered two ways
    (twice as 'He laughed loudly.', once as 'He chuckled.'). Returns novel_id."""
    src = "他大笑。"
    h = _hash(src)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('TM Novel', 'paste')"
        )
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        rows = [(1, "He laughed loudly."), (2, "He laughed loudly."), (3, "He chuckled.")]
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
        conn.commit()
        return novel_id
    finally:
        conn.close()


def test_inconsistencies_route_reshapes_nested_groups(client):
    novel_id = _seed_inconsistency()
    resp = client.get(f"/api/novels/{novel_id}/tm/inconsistencies")
    assert resp.status_code == 200
    groups = resp.json()
    assert len(groups) == 1
    g = groups[0]
    assert g["source_text"] == "他大笑。"
    assert g["source_hash"] == _hash("他大笑。")
    assert g["total_occurrences"] == 3

    renderings = {r["target_text"]: r for r in g["renderings"]}
    assert set(renderings) == {"He laughed loudly.", "He chuckled."}
    # The majority rendering carries both chapters; the outlier carries one.
    majority = renderings["He laughed loudly."]["chapters"]
    outlier = renderings["He chuckled."]["chapters"]
    assert {c["chapter_num"] for c in majority} == {1, 2}
    assert {c["chapter_num"] for c in outlier} == {3}
    # ConcordanceChapterMeta shape is fully populated per chapter.
    sample = majority[0]
    assert set(sample) == {"chapter_id", "chapter_num", "title_en"}
    assert sample["title_en"] == f"Chapter {sample['chapter_num']}"


def test_inconsistencies_route_empty_when_consistent(client):
    src = "他大笑。"
    h = _hash(src)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO novels (title, source_type) VALUES ('N', 'paste')")
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for ch_num in (1, 2):
            conn.execute(
                "INSERT INTO chapters (novel_id, chapter_num, original_text, "
                "translated_text, status) VALUES (?, ?, ?, 'He laughed.', 'done')",
                (novel_id, ch_num, src),
            )
            cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO tm_segments (novel_id, chapter_id, paragraph_index, "
                "source_text, target_text, source_hash) VALUES (?, ?, 0, ?, 'He laughed.', ?)",
                (novel_id, cid, src, h),
            )
        conn.commit()
    finally:
        conn.close()
    resp = client.get(f"/api/novels/{novel_id}/tm/inconsistencies")
    assert resp.status_code == 200
    assert resp.json() == []
