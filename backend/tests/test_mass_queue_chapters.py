"""Tests for POST /api/novels/{id}/queue (mass-queue chapters for translation).

The endpoint is the bulk version of the per-chapter retranslate route, driven
from the reader TOC rail's Queue-chapters dialog. Critical invariants:

- 404 on unknown novel id (caller can't accidentally queue against /any/ id).
- Range mode requires from/to and rejects from > to.
- Pending chapters go through queue_translations (just flips the flag).
- Errored chapters go through reset_chapters_for_retranslate (status reset
  + flag) only when include_errors=true; otherwise they are counted as
  skipped_errors.
- Done / in-flight / already-queued chapters are skipped and counted.
- Range mode honors the chapter_num window.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import SCHEMA
from backend.main import app

DB_PATH = Path(os.environ["DB_PATH"])


def _wipe_db() -> None:
    """Reset the shared DB_PATH between tests. Drops the file outright so
    the SCHEMA + this test's seeds are the only state visible to the route.
    Mirrors what other route tests do (test_glossary_bulk_retranslate, etc.)."""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(DB_PATH) + suffix)
        if candidate.exists():
            candidate.unlink()


@pytest.fixture
def client(monkeypatch):
    _wipe_db()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    # Stub the worker-spawn paths so the route returns synchronously without
    # firing real translator backends. queue_translations is replaced with a
    # version that just flips the flag (no asyncio task spawning); the reset
    # path goes through spawn_translate_worker which we stub to a no-op.
    # Stubbing at the function level — not the lower-level _spawn — keeps
    # the test from leaking partially-opened aiosqlite connections (which
    # caused database-disk-image-malformed faults in cross-test runs).
    import backend.services.queue as queue_svc
    from backend.db import open_conn

    async def _fake_queue_translations(novel_id, chapter_ids):
        if not chapter_ids:
            return
        async with open_conn() as c:
            placeholders = ",".join("?" * len(chapter_ids))
            await c.execute(
                f"UPDATE chapters SET translate_queued = 1 "
                f"WHERE novel_id = ? AND id IN ({placeholders})",
                [novel_id, *chapter_ids],
            )
            await c.commit()

    monkeypatch.setattr(queue_svc, "spawn_translate_worker", lambda nid, cid: None)
    monkeypatch.setattr(queue_svc, "queue_translations", _fake_queue_translations)
    yield TestClient(app)
    # Belt and braces: wipe the DB on teardown too so a crashing test inside
    # this module can't leave shared state for the next file.
    _wipe_db()


def _seed_novel(title: str = "Test Novel") -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, 'paste')",
            (title,),
        )
        nid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return nid
    finally:
        conn.close()


def _seed_chapter(
    novel_id: int,
    chapter_num: int,
    *,
    status: str = "pending",
    translate_queued: int = 0,
) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, original_text, status, translate_queued) "
            "VALUES (?, ?, '第N章 ...', ?, ?)",
            (novel_id, chapter_num, status, translate_queued),
        )
        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return cid
    finally:
        conn.close()


def _fetch(chapter_id: int) -> tuple[str, int]:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT status, translate_queued FROM chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()
        return row[0], row[1]
    finally:
        conn.close()


def test_unknown_novel_404(client: TestClient) -> None:
    resp = client.post("/api/novels/999999/queue", json={})
    assert resp.status_code == 404, resp.text


def test_all_untranslated_queues_pending(client: TestClient) -> None:
    nid = _seed_novel()
    c1 = _seed_chapter(nid, 1, status="pending")
    c2 = _seed_chapter(nid, 2, status="pending")
    _seed_chapter(nid, 3, status="done")
    resp = client.post(f"/api/novels/{nid}/queue", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queued_count"] == 2
    assert body["skipped_done"] == 1
    assert body["skipped_in_flight"] == 0
    assert body["skipped_already_queued"] == 0
    # The two pending chapters now carry translate_queued=1, status untouched.
    assert _fetch(c1) == ("pending", 1)
    assert _fetch(c2) == ("pending", 1)


def test_skips_in_flight_and_already_queued(client: TestClient) -> None:
    nid = _seed_novel()
    c_pending = _seed_chapter(nid, 1, status="pending")
    _seed_chapter(nid, 2, status="translating")
    _seed_chapter(nid, 3, status="pending", translate_queued=1)
    resp = client.post(f"/api/novels/{nid}/queue", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queued_count"] == 1
    assert body["skipped_in_flight"] == 1
    assert body["skipped_already_queued"] == 1
    assert _fetch(c_pending) == ("pending", 1)


def test_include_errors_resets_and_queues(client: TestClient) -> None:
    nid = _seed_novel()
    c_err = _seed_chapter(nid, 1, status="error")
    resp = client.post(
        f"/api/novels/{nid}/queue",
        json={"mode": "all_untranslated", "include_errors": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queued_count"] == 1
    assert body["skipped_errors"] == 0
    # Reset path flips status back to pending + sets translate_queued=1.
    assert _fetch(c_err) == ("pending", 1)


def test_exclude_errors_counts_them_skipped(client: TestClient) -> None:
    nid = _seed_novel()
    c_err = _seed_chapter(nid, 1, status="error")
    resp = client.post(
        f"/api/novels/{nid}/queue",
        json={"mode": "all_untranslated", "include_errors": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queued_count"] == 0
    assert body["skipped_errors"] == 1
    # Errored row untouched.
    assert _fetch(c_err) == ("error", 0)


def test_range_mode_window(client: TestClient) -> None:
    nid = _seed_novel()
    c1 = _seed_chapter(nid, 1, status="pending")
    c2 = _seed_chapter(nid, 2, status="pending")
    c3 = _seed_chapter(nid, 3, status="pending")
    c4 = _seed_chapter(nid, 4, status="pending")
    resp = client.post(
        f"/api/novels/{nid}/queue",
        json={"mode": "range", "from_chapter": 2, "to_chapter": 3},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["queued_count"] == 2
    assert _fetch(c1) == ("pending", 0)
    assert _fetch(c2) == ("pending", 1)
    assert _fetch(c3) == ("pending", 1)
    assert _fetch(c4) == ("pending", 0)


def test_range_requires_from_and_to(client: TestClient) -> None:
    nid = _seed_novel()
    _seed_chapter(nid, 1, status="pending")
    resp = client.post(
        f"/api/novels/{nid}/queue",
        json={"mode": "range"},
    )
    assert resp.status_code == 400, resp.text


def test_range_rejects_inverted_bounds(client: TestClient) -> None:
    nid = _seed_novel()
    _seed_chapter(nid, 1, status="pending")
    resp = client.post(
        f"/api/novels/{nid}/queue",
        json={"mode": "range", "from_chapter": 5, "to_chapter": 2},
    )
    assert resp.status_code == 400, resp.text
