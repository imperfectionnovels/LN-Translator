"""Tests for POST /api/glossary/bulk-retranslate-affected (Design v2 Phase D).

The endpoint consolidates per-entry retranslate-affected so the Ledger view's
bulk action can ship a multi-term retranslate as one user gesture. Critical
invariants:

- Cross-novel batches are rejected at the route boundary (worker dispatch is
  per-novel; silently splitting would surface no error to the user).
- Missing entry ids 404 (early; never partially queue).
- Shared chapters across multiple terms are queued exactly once.
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


@pytest.fixture
def client(monkeypatch):
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    # The route spawns translate workers via queue_svc.spawn_translate_worker.
    # The default behaviour fires off background tasks that try to use real
    # translator backends — stub to a no-op so the route returns synchronously.
    import backend.services.queue as queue_svc
    monkeypatch.setattr(queue_svc, "spawn_translate_worker", lambda nid, cid: None)
    return TestClient(app)


def _seed(novel_title: str, term_zh: str, term_en: str) -> tuple[int, int]:
    """Insert one novel + one glossary entry. Returns (novel_id, entry_id)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, 'paste')",
            (novel_title,),
        )
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO glossary_entries "
            "(novel_id, term_zh, term_en, category, locked) "
            "VALUES (?, ?, ?, 'item', 1)",
            (novel_id, term_zh, term_en),
        )
        entry_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return novel_id, entry_id
    finally:
        conn.close()


def test_bulk_rejects_cross_novel(client: TestClient) -> None:
    _, eid_a = _seed("Novel A", "甲", "Alpha")
    _, eid_b = _seed("Novel B", "乙", "Beta")
    resp = client.post(
        "/api/glossary/bulk-retranslate-affected",
        json={"entry_ids": [eid_a, eid_b]},
    )
    assert resp.status_code == 400, resp.text
    assert "same novel" in resp.json()["detail"]


def test_bulk_404_on_missing_entry(client: TestClient) -> None:
    _, eid_a = _seed("Novel A", "甲", "Alpha")
    resp = client.post(
        "/api/glossary/bulk-retranslate-affected",
        json={"entry_ids": [eid_a, 999999]},
    )
    assert resp.status_code == 404, resp.text


def test_bulk_zero_affected_returns_clean_payload(client: TestClient) -> None:
    """No chapters in the novel → no chapters to retranslate. The endpoint
    should return cleanly with queued_count=0 instead of erroring."""
    _, eid_a = _seed("Novel A", "甲", "Alpha")
    resp = client.post(
        "/api/glossary/bulk-retranslate-affected",
        json={"entry_ids": [eid_a]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queued_count"] == 0
    assert body["per_entry"][0]["affected_count"] == 0


def test_bulk_dedupes_shared_chapters(client: TestClient) -> None:
    """Two terms that both appear in the same chapter must queue that
    chapter exactly once. Validates the union-set logic in the route."""
    novel_id, eid_a = _seed("Novel A", "甲", "Alpha")
    # Add a second entry to the same novel.
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO glossary_entries "
            "(novel_id, term_zh, term_en, category, locked) "
            "VALUES (?, '乙', 'Beta', 'item', 1)",
            (novel_id,),
        )
        eid_b = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Seed one chapter that contains both terms.
        conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, status) "
            "VALUES (?, 1, '第一章 甲与乙相遇。', 'done')",
            (novel_id,),
        )
        chapter_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    resp = client.post(
        "/api/glossary/bulk-retranslate-affected",
        json={"entry_ids": [eid_a, eid_b]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Both entries report 1 affected chapter; the union is also 1 (the same
    # chapter), so queued_count must equal 1 — NOT 2.
    assert body["per_entry"][0]["affected_count"] == 1
    assert body["per_entry"][1]["affected_count"] == 1
    assert body["queued_count"] == 1

    # And the chapter is actually queued in the DB.
    conn = sqlite3.connect(DB_PATH)
    try:
        flag = conn.execute(
            "SELECT translate_queued FROM chapters WHERE id = ?",
            (chapter_id,),
        ).fetchone()[0]
        assert flag == 1
    finally:
        conn.close()
