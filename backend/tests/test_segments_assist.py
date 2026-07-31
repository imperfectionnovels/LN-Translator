"""CAT Phase 4: GET .../segments/{seg_index}/assist + machine_differs.

Pins the assist-rail contract: exact TM matches (same source_hash in other
chapters, full-text verified, confirmed > edited > machine, cap 5), fuzzy
matches (rapidfuzz over chapter_segments sources, 0.80 threshold, tiny
sources skipped, cap 5), machine_text passthrough, the 404 contract, and
the machine_differs flag on the segment list payload.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.services.segments import hash16

DB_PATH = Path(os.environ["DB_PATH"])

_SRC_PARAS = [
    "甲甲甲甲甲甲甲甲甲甲甲甲甲甲。",
    "乙乙乙乙乙乙乙乙乙乙乙乙乙乙。",
    "丙丙丙丙丙丙丙丙丙丙丙丙丙丙。",
]
_SRC = "\n\n".join(_SRC_PARAS)
_TGT = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph here."


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


def _seed_novel_and_chapter(
    conn: sqlite3.Connection, novel_id: int | None, chapter_num: int,
    original: str = _SRC, translated: str = _TGT,
) -> tuple[int, int]:
    if novel_id is None:
        conn.execute("INSERT INTO novels (title, source_type) VALUES ('N', 'paste')")
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO chapters (novel_id, chapter_num, title_zh, title_en, "
        "original_text, translated_text, status) "
        "VALUES (?, ?, '第一章', 'C', ?, ?, 'done')",
        (novel_id, chapter_num, original, translated),
    )
    chapter_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return novel_id, chapter_id


def _insert_segment(
    conn: sqlite3.Connection, novel_id: int, chapter_id: int, seg_index: int,
    source_text: str, target_text: str, status: str = "machine",
    source_hash: str | None = None, confirmed_at: str | None = None,
    machine_text: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO chapter_segments (novel_id, chapter_id, seg_index, "
        "source_text, source_hash, target_text, machine_text, status, "
        "origin, aligned, confirmed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'aligned_backfill', 1, ?)",
        (novel_id, chapter_id, seg_index, source_text,
         source_hash or hash16(source_text), target_text,
         machine_text if machine_text is not None else target_text,
         status, confirmed_at),
    )


def _setup(client) -> tuple[int, int]:
    """Seed chapter 1, build its store via the GET (the lazy backfill), and
    return (novel_id, other_chapter_id) where chapter 2 is an empty vessel
    for hand-inserted corpus rows."""
    conn = sqlite3.connect(DB_PATH)
    try:
        novel_id, _ = _seed_novel_and_chapter(conn, None, 1)
        _, ch2 = _seed_novel_and_chapter(
            conn, novel_id, 2,
            original="丁丁丁丁丁丁丁。", translated="Other chapter body.",
        )
        conn.commit()
    finally:
        conn.close()
    r = client.get(f"/api/novels/{novel_id}/chapters/1/segments")
    assert r.status_code == 200 and r.json()["segments_state"] == "ok"
    return novel_id, ch2


def test_assist_exact_ranking_and_cap(client):
    novel_id, ch2 = _setup(client)
    shared = _SRC_PARAS[0]
    conn = sqlite3.connect(DB_PATH)
    try:
        # 5 confirmed (only the newest 5 of 6 should survive the cap once
        # edited/machine rows are pushed out), plus edited + machine rows.
        for i in range(6):
            _insert_segment(
                conn, novel_id, ch2, 10 + i, shared,
                f"Confirmed rendering {i}.", status="confirmed",
                confirmed_at=f"2026-01-0{i + 1} 00:00:00",
            )
        _insert_segment(conn, novel_id, ch2, 20, shared,
                        "Edited rendering.", status="edited")
        _insert_segment(conn, novel_id, ch2, 21, shared,
                        "Machine rendering.", status="machine")
        # Same hash, DIFFERENT full source text: a forged collision that the
        # full-text check must exclude.
        _insert_segment(conn, novel_id, ch2, 22, "完全不同的原文在此。",
                        "WRONG.", status="confirmed",
                        source_hash=hash16(shared),
                        confirmed_at="2026-06-01 00:00:00")
        conn.commit()
    finally:
        conn.close()

    r = client.get(f"/api/novels/{novel_id}/chapters/1/segments/0/assist")
    assert r.status_code == 200
    data = r.json()
    exact = data["tm_exact"]
    assert len(exact) == 5  # cap
    assert all(e["status"] == "confirmed" for e in exact)
    # Most recently confirmed first; the forged collision is absent.
    assert exact[0]["target_text"] == "Confirmed rendering 5."
    assert all(e["target_text"] != "WRONG." for e in exact)


def test_assist_exact_status_order(client):
    novel_id, ch2 = _setup(client)
    shared = _SRC_PARAS[0]
    conn = sqlite3.connect(DB_PATH)
    try:
        _insert_segment(conn, novel_id, ch2, 10, shared,
                        "Machine rendering.", status="machine")
        _insert_segment(conn, novel_id, ch2, 11, shared,
                        "Edited rendering.", status="edited")
        _insert_segment(conn, novel_id, ch2, 12, shared,
                        "Confirmed rendering.", status="confirmed",
                        confirmed_at="2026-01-01 00:00:00")
        conn.commit()
    finally:
        conn.close()
    r = client.get(f"/api/novels/{novel_id}/chapters/1/segments/0/assist")
    statuses = [e["status"] for e in r.json()["tm_exact"]]
    assert statuses == ["confirmed", "edited", "machine"]


def test_assist_fuzzy_threshold_and_tiny_skip(client):
    novel_id, ch2 = _setup(client)
    conn = sqlite3.connect(DB_PATH)
    try:
        # Near-duplicate of source paragraph 0 (one char differs out of 14):
        # well above the 0.80 cutoff.
        near = "甲甲甲甲甲甲甲甲甲甲甲甲乙。"
        _insert_segment(conn, novel_id, ch2, 10, near,
                        "A near duplicate rendering.", status="confirmed",
                        confirmed_at="2026-01-01 00:00:00")
        # Unrelated same-length source: far below the cutoff.
        _insert_segment(conn, novel_id, ch2, 11, "戊戊戊戊戊戊戊戊戊戊戊戊戊戊。",
                        "Unrelated rendering.")
        # Tiny source: skipped even though it would score.
        _insert_segment(conn, novel_id, ch2, 12, "甲甲。", "Tiny rendering.")
        conn.commit()
    finally:
        conn.close()
    r = client.get(f"/api/novels/{novel_id}/chapters/1/segments/0/assist")
    fuzzy = r.json()["tm_fuzzy"]
    targets = [f["target_text"] for f in fuzzy]
    assert "A near duplicate rendering." in targets
    assert "Unrelated rendering." not in targets
    assert "Tiny rendering." not in targets
    hit = next(f for f in fuzzy if f["target_text"] == "A near duplicate rendering.")
    assert 0.8 <= hit["score"] <= 1.0
    assert hit["chapter_num"] == 2
    assert hit["source_text"] == "甲甲甲甲甲甲甲甲甲甲甲甲乙。"


def test_assist_empty_states_and_machine_text(client):
    novel_id, _ch2 = _setup(client)
    r = client.get(f"/api/novels/{novel_id}/chapters/1/segments/0/assist")
    assert r.status_code == 200
    data = r.json()
    assert data["tm_exact"] == []
    assert data["tm_fuzzy"] == []
    # The lazy backfill stores machine_text == target_text.
    assert data["machine_text"] == "First paragraph here."


def test_assist_404_on_missing_segment_or_chapter(client):
    novel_id, _ch2 = _setup(client)
    assert client.get(
        f"/api/novels/{novel_id}/chapters/1/segments/99/assist"
    ).status_code == 404
    assert client.get(
        f"/api/novels/{novel_id}/chapters/77/segments/0/assist"
    ).status_code == 404


def test_machine_differs_flag_in_list_payload(client):
    novel_id, _ch2 = _setup(client)
    r = client.get(f"/api/novels/{novel_id}/chapters/1/segments")
    seg = r.json()["segments"][0]
    assert seg["machine_differs"] is False

    patch = client.patch(
        f"/api/novels/{novel_id}/chapters/1/segments/0",
        json={
            "action": "save",
            "after_text": "A human rewrite of the first paragraph.",
            "chapter_rev": r.json()["chapter_rev"],
            "before_target_hash": seg["target_hash"],
        },
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["segment"]["machine_differs"] is True

    r2 = client.get(f"/api/novels/{novel_id}/chapters/1/segments")
    seg2 = r2.json()["segments"][0]
    assert seg2["status"] == "edited"
    assert seg2["machine_differs"] is True
    # The AI's stored rendering is served by the assist endpoint.
    assist = client.get(
        f"/api/novels/{novel_id}/chapters/1/segments/0/assist"
    ).json()
    assert assist["machine_text"] == "First paragraph here."
