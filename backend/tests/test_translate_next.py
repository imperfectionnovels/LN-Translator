"""Tests for the translate-next (move-to-front) feature (audit 3.2).

Covers:
1. Fresh DB has queue_priority column with default 0.
2. POST translate-next on a queued pending chapter returns 200 + queue_priority 1;
   a second call on a different queued chapter returns 2 (most-recent wins front).
3. 409 when chapter exists but is not queued; 404 for nonexistent chapter.
4. Worker order: queue chapters 1, 2, 3, prioritize 3, drive workers, assert [3, 1, 2].
5. After a successful translate the processed chapter has queue_priority 0
   and translate_queued 0.
6. Cancel-all-waiting zeroes queue_priority on cancelled rows.
7. /api/novels/queue/all lists a prioritized waiting chapter before other waiting.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import SCHEMA, init_db, open_conn
from backend.main import app
from backend.services import queue as queue_svc

DB_PATH = Path(os.environ["DB_PATH"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _wipe_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()


def _sync_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_novel(title: str = "Test Novel") -> int:
    conn = _sync_conn()
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, 'paste')", (title,)
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
    queue_priority: int = 0,
) -> int:
    conn = _sync_conn()
    try:
        conn.execute(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, original_text, status, translate_queued, queue_priority) "
            "VALUES (?, ?, '第N章 content', ?, ?, ?)",
            (novel_id, chapter_num, status, translate_queued, queue_priority),
        )
        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return cid
    finally:
        conn.close()


def _fetch_chapter(chapter_id: int) -> sqlite3.Row:
    conn = _sync_conn()
    try:
        return conn.execute(
            "SELECT status, translate_queued, queue_priority FROM chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTTP-layer fixtures (TestClient, no real workers)
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    """TestClient with worker-spawn stubbed to no-ops."""
    _wipe_db()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    monkeypatch.setattr(queue_svc, "spawn_translate_worker", lambda nid, cid: None)

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

    monkeypatch.setattr(queue_svc, "queue_translations", _fake_queue_translations)

    yield TestClient(app)
    _wipe_db()


# ---------------------------------------------------------------------------
# Async-worker fixture (real asyncio, no real LLM)
# ---------------------------------------------------------------------------

async def _reset_db_async() -> None:
    async with open_conn() as conn:
        for table in ("chapters", "novels", "providers"):
            try:
                await conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        await conn.commit()


@pytest.fixture
async def fresh_db():
    await init_db()
    await _reset_db_async()
    yield
    await _reset_db_async()


async def _seed_provider() -> int:
    from backend.services import providers as providers_svc
    p = await providers_svc.create_provider(
        name="stub-translator",
        provider_type="gemini",
        model_id="stub",
        is_default=True,
    )
    return p.id


async def _make_novel_and_chapters(n: int) -> tuple[int, list[int]]:
    """Create a novel with N queued pending chapters. Returns (novel_id, [chapter_ids])."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('wn', 'paste')"
        )
        novel_id = cur.lastrowid
        chapter_ids = []
        for i in range(1, n + 1):
            cur = await conn.execute(
                "INSERT INTO chapters "
                "(novel_id, chapter_num, original_text, status, translate_queued) "
                "VALUES (?, ?, '原文 ch' || ?, 'pending', 1)",
                (novel_id, i, i),
            )
            chapter_ids.append(cur.lastrowid)
        await conn.commit()
    return novel_id, chapter_ids


# ---------------------------------------------------------------------------
# Test 1: schema -- queue_priority column default 0
# ---------------------------------------------------------------------------

def test_queue_priority_column_default_zero(client: TestClient) -> None:
    """Fresh DB must have queue_priority with default 0 (PRAGMA table_info)."""
    conn = _sync_conn()
    try:
        rows = conn.execute("PRAGMA table_info(chapters)").fetchall()
    finally:
        conn.close()
    col_info = {r["name"]: r for r in rows}
    assert "queue_priority" in col_info, "queue_priority column missing from chapters"
    assert col_info["queue_priority"]["dflt_value"] == "0", (
        f"expected default 0, got {col_info['queue_priority']['dflt_value']!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: translate-next returns 200, priority increments correctly
# ---------------------------------------------------------------------------

def test_translate_next_200_and_priority(client: TestClient) -> None:
    nid = _seed_novel()
    c1 = _seed_chapter(nid, 1, status="pending", translate_queued=1)
    c2 = _seed_chapter(nid, 2, status="pending", translate_queued=1)

    # Prioritize chapter 1 first
    resp = client.post(f"/api/novels/{nid}/chapters/1/translate-next")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    prio1 = body["queue_priority"]
    assert prio1 >= 1

    # Prioritize chapter 2 -- must get a higher priority (wins the front)
    resp2 = client.post(f"/api/novels/{nid}/chapters/2/translate-next")
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    prio2 = body2["queue_priority"]
    assert prio2 > prio1, (
        f"second translate-next should get higher priority; got {prio2} vs {prio1}"
    )

    row1 = _fetch_chapter(c1)
    row2 = _fetch_chapter(c2)
    assert row1["queue_priority"] == prio1
    assert row2["queue_priority"] == prio2


# ---------------------------------------------------------------------------
# Test 3: 409 not queued, 404 nonexistent
# ---------------------------------------------------------------------------

def test_translate_next_409_not_queued(client: TestClient) -> None:
    nid = _seed_novel()
    _seed_chapter(nid, 1, status="done", translate_queued=0)

    resp = client.post(f"/api/novels/{nid}/chapters/1/translate-next")
    assert resp.status_code == 409, resp.text
    assert "not waiting in the queue" in resp.json()["detail"]


def test_translate_next_404_no_chapter(client: TestClient) -> None:
    nid = _seed_novel()
    resp = client.post(f"/api/novels/{nid}/chapters/999/translate-next")
    assert resp.status_code == 404, resp.text


def test_translate_next_409_already_translating(client: TestClient) -> None:
    nid = _seed_novel()
    _seed_chapter(nid, 1, status="translating", translate_queued=1)

    resp = client.post(f"/api/novels/{nid}/chapters/1/translate-next")
    assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# Test 4: worker processing order respects queue_priority
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_order_respects_priority(monkeypatch, fresh_db) -> None:
    """Queue chapters 1, 2, 3; prioritize 3; assert processing order is 3, 1, 2."""
    await _seed_provider()
    novel_id, chapter_ids = await _make_novel_and_chapters(3)

    processed_order: list[int] = []

    async def _recording_translate(source, title, glossary, **kwargs):
        # Identify which chapter is being processed by looking up the
        # translating row. We return a minimal stub result.
        class _StubResult:
            title_en = "Title"
            translated_text = "translated body"
            degraded = False
            usage = None
            new_terms = []
            parse_error = None
            prompt_snapshot = None

        async with open_conn() as conn:
            cur = await conn.execute(
                "SELECT chapter_num FROM chapters WHERE status = 'translating' "
                "AND novel_id = ? ORDER BY chapter_num LIMIT 1",
                (novel_id,),
            )
            row = await cur.fetchone()
            if row:
                processed_order.append(row["chapter_num"])
        return _StubResult()

    monkeypatch.setattr("backend.services.queue.translate_chapter", _recording_translate)

    # Prioritize chapter 3 before spawning workers
    async with open_conn() as conn:
        prio = await queue_svc.prioritize_chapter(conn, novel_id, chapter_num=3)
    assert prio is not None, "prioritize_chapter returned None"

    # Spawn workers (one per queued chapter, as the real code does)
    for cid in chapter_ids:
        queue_svc._spawn_translate(novel_id, cid)

    # Wait until all three chapters finish
    deadline = asyncio.get_event_loop().time() + 10.0
    while asyncio.get_event_loop().time() < deadline:
        async with open_conn() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) AS n FROM chapters WHERE novel_id = ? AND status = 'done'",
                (novel_id,),
            )
            row = await cur.fetchone()
            if row["n"] == 3:
                break
        await asyncio.sleep(0.05)

    assert processed_order == [3, 1, 2], (
        f"expected order [3, 1, 2] but got {processed_order}"
    )


# ---------------------------------------------------------------------------
# Test 5: success commit zeroes queue_priority and translate_queued
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_success_commit_clears_priority(monkeypatch, fresh_db) -> None:
    """After a chapter translates, queue_priority and translate_queued are 0."""
    await _seed_provider()

    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('wn', 'paste')"
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, original_text, status, translate_queued, queue_priority) "
            "VALUES (?, 1, '原文', 'pending', 1, 5)",
            (novel_id,),
        )
        chapter_id = cur.lastrowid
        await conn.commit()

    class _StubResult:
        title_en = "Chapter 1"
        translated_text = "finished body"
        degraded = False
        usage = None
        new_terms = []
        parse_error = None
        prompt_snapshot = None

    async def _fast_translate(*a, **kw):
        return _StubResult()

    monkeypatch.setattr("backend.services.queue.translate_chapter", _fast_translate)

    queue_svc._spawn_translate(novel_id, chapter_id)

    deadline = asyncio.get_event_loop().time() + 10.0
    while asyncio.get_event_loop().time() < deadline:
        async with open_conn() as conn:
            cur = await conn.execute(
                "SELECT status FROM chapters WHERE id = ?", (chapter_id,)
            )
            row = await cur.fetchone()
            if row and row["status"] in ("done", "error"):
                break
        await asyncio.sleep(0.05)

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT status, translate_queued, queue_priority FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["status"] == "done", f"expected done, got {row['status']}"
    assert row["translate_queued"] == 0, "translate_queued should be 0 after success"
    assert row["queue_priority"] == 0, "queue_priority should be 0 after success"


# ---------------------------------------------------------------------------
# Test 6: cancel-all-waiting zeroes queue_priority
# ---------------------------------------------------------------------------

def test_cancel_all_zeroes_priority(client: TestClient) -> None:
    nid = _seed_novel()
    c1 = _seed_chapter(nid, 1, status="pending", translate_queued=1, queue_priority=7)
    c2 = _seed_chapter(nid, 2, status="pending", translate_queued=1, queue_priority=3)
    # In-flight chapter should NOT be touched by the cancel
    _seed_chapter(nid, 3, status="translating", translate_queued=1, queue_priority=9)

    resp = client.delete(f"/api/novels/{nid}/queue")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cancelled_translate"] == 2  # two waiting rows, not the in-flight one

    row1 = _fetch_chapter(c1)
    row2 = _fetch_chapter(c2)
    assert row1["translate_queued"] == 0
    assert row1["queue_priority"] == 0, "queue_priority should be zeroed on cancel"
    assert row2["translate_queued"] == 0
    assert row2["queue_priority"] == 0, "queue_priority should be zeroed on cancel"


def test_cancel_global_all_zeroes_priority(client: TestClient) -> None:
    nid = _seed_novel()
    c1 = _seed_chapter(nid, 1, status="pending", translate_queued=1, queue_priority=5)

    resp = client.delete("/api/novels/queue/all")
    assert resp.status_code == 200, resp.text

    row1 = _fetch_chapter(c1)
    assert row1["translate_queued"] == 0
    assert row1["queue_priority"] == 0, "global cancel should zero queue_priority"


# ---------------------------------------------------------------------------
# Test 7: /api/novels/queue/all lists prioritized chapter before others
# ---------------------------------------------------------------------------

def test_queue_all_order_reflects_priority(client: TestClient) -> None:
    """A translate-next prioritized chapter appears before others in queue/all."""
    nid = _seed_novel()
    _seed_chapter(nid, 1, status="pending", translate_queued=1, queue_priority=0)
    _seed_chapter(nid, 2, status="pending", translate_queued=1, queue_priority=0)
    _seed_chapter(nid, 3, status="pending", translate_queued=1, queue_priority=0)

    # Prioritize chapter 2
    resp = client.post(f"/api/novels/{nid}/chapters/2/translate-next")
    assert resp.status_code == 200, resp.text

    resp = client.get("/api/novels/queue/all")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    translate = data["translate"]
    # Filter to waiting (not in-flight)
    waiting = [t for t in translate if not t["in_flight"]]
    chapter_nums = [t["chapter_num"] for t in waiting]
    assert chapter_nums[0] == 2, (
        f"prioritized chapter 2 should be first in queue/all, got order: {chapter_nums}"
    )
