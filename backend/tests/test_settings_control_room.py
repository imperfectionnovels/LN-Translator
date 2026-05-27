"""Tests for the settings control-room endpoints landed 2026-05-26.

Covers:
- providers.last_tested_at migration applies and `/test` stamps it on success.
- `/api/providers/{id}/stats` returns bucket arrays of the right length and
  aggregates over `chapters.translated_at` / `cost_usd` filtered by the
  novel's `translator_provider_id`.
- `/api/providers/{id}/routed-novels` returns novels routed through this
  provider with the role discriminator.
- `/api/providers/{id}/activity` joins the attempts log to chapters/novels.
- `/api/diagnostics` returns version + size fields without errors.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import SCHEMA
from backend.main import app

DB_PATH = Path(os.environ["DB_PATH"])


@pytest.fixture
def client():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return TestClient(app)


def _now_iso(offset_days: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=offset_days)
    return dt.isoformat(timespec="seconds").replace("+00:00", "")


def _seed_provider(name: str = "Test Provider", provider_type: str = "claude_agent") -> int:
    """Insert a provider. Only the first one is flagged default (the schema's
    partial unique index allows at most one default row)."""
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0]
    is_default = 1 if existing == 0 else 0
    cur = conn.execute(
        "INSERT INTO providers (name, provider_type, model_id, is_default) "
        "VALUES (?, ?, 'fake-model', ?)",
        (name, provider_type, is_default),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


def _seed_novel(provider_id: int, title: str = "Test Novel") -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO novels (title, source_type, translator_provider_id) "
        "VALUES (?, 'paste', ?)",
        (title, provider_id),
    )
    nid = cur.lastrowid
    conn.commit()
    conn.close()
    return nid


def _seed_chapter(novel_id: int, num: int, *, translated: bool = True,
                  cost: float = 0.05, status: str = "done",
                  days_ago: int = 0) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO chapters (novel_id, chapter_num, original_text, "
        "translated_text, status, translated_at, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (novel_id, num, f"原文 {num}",
         f"english {num}" if translated else None,
         status,
         _now_iso(days_ago) if translated else None,
         cost if translated else None),
    )
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    return cid


def _seed_attempt(chapter_id: int, provider_id: int, status: str = "ok",
                  days_ago: int = 0) -> None:
    conn = sqlite3.connect(DB_PATH)
    started = _now_iso(days_ago)
    finished = _now_iso(days_ago)  # zero duration is fine for these tests
    conn.execute(
        "INSERT INTO chapter_translation_attempts "
        "(chapter_id, provider_id, model_id, started_at, finished_at, status) "
        "VALUES (?, ?, 'fake-model', ?, ?, ?)",
        (chapter_id, provider_id, started, finished, status),
    )
    conn.commit()
    conn.close()


# ---------- last_tested_at migration + stamp ----------

def test_providers_table_has_last_tested_at(client: TestClient) -> None:
    conn = sqlite3.connect(DB_PATH)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(providers)")]
    conn.close()
    assert "last_tested_at" in cols


def test_test_endpoint_stamps_last_tested_at_on_success(client: TestClient) -> None:
    pid = _seed_provider()
    # Before: NULL.
    r = client.get(f"/api/providers/{pid}").json()
    assert r["last_tested_at"] is None

    # claude_agent has no secret requirement → test should pass.
    resp = client.post(f"/api/providers/{pid}/test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    after = client.get(f"/api/providers/{pid}").json()
    assert after["last_tested_at"] is not None


# ---------- /stats ----------

def test_stats_empty_provider_returns_zeros(client: TestClient) -> None:
    pid = _seed_provider()
    r = client.get(f"/api/providers/{pid}/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["chapters_translated_30d"] == 0
    assert body["spend_30d_usd"] == 0
    assert body["failure_rate_30d"] == 0
    assert len(body["chapters_translated_buckets"]) == 14
    assert len(body["spend_30d_buckets"]) == 14
    assert all(x == 0 for x in body["chapters_translated_buckets"])


def test_stats_aggregates_translated_chapters(client: TestClient) -> None:
    pid = _seed_provider()
    nid = _seed_novel(pid)
    _seed_chapter(nid, 1, cost=0.10, days_ago=0)
    _seed_chapter(nid, 2, cost=0.12, days_ago=1)
    _seed_chapter(nid, 3, cost=0.08, days_ago=5)
    # A chapter from BEFORE the window is excluded.
    _seed_chapter(nid, 4, cost=99.00, days_ago=400)

    body = client.get(f"/api/providers/{pid}/stats").json()
    assert body["chapters_translated_30d"] == 3
    assert body["spend_30d_usd"] == pytest.approx(0.30, rel=1e-3)


def test_stats_failure_rate_from_attempts(client: TestClient) -> None:
    pid = _seed_provider()
    nid = _seed_novel(pid)
    cid = _seed_chapter(nid, 1)
    _seed_attempt(cid, pid, status="ok")
    _seed_attempt(cid, pid, status="ok")
    _seed_attempt(cid, pid, status="error")
    body = client.get(f"/api/providers/{pid}/stats").json()
    assert body["attempts_30d"] == 3
    assert body["failure_count_30d"] == 1
    assert body["failure_rate_30d"] == pytest.approx(1 / 3, rel=1e-3)


# ---------- /routed-novels ----------

def test_routed_novels_lists_translator_and_refinement(client: TestClient) -> None:
    pid = _seed_provider()
    other = _seed_provider("Other", provider_type="claude_cli")
    n1 = _seed_novel(pid, "N1 translator")
    n2 = _seed_novel(other, "N2 other")  # not routed to pid

    # Mark n2 as refinement target of pid.
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE novels SET refinement_provider_id = ? WHERE id = ?",
        (pid, n2),
    )
    conn.commit()
    conn.close()

    body = client.get(f"/api/providers/{pid}/routed-novels").json()
    titles = [n["title"] for n in body["novels"]]
    roles = {n["title"]: n["role"] for n in body["novels"]}
    assert set(titles) == {"N1 translator", "N2 other"}
    assert roles["N1 translator"] == "translator"
    assert roles["N2 other"] == "refinement"
    assert body["total"] == 2


def test_routed_novels_excludes_archived(client: TestClient) -> None:
    pid = _seed_provider()
    _seed_novel(pid, "Active")
    archived = _seed_novel(pid, "Archived")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE novels SET deleted_at = datetime('now') WHERE id = ?",
        (archived,),
    )
    conn.commit()
    conn.close()

    body = client.get(f"/api/providers/{pid}/routed-novels").json()
    titles = [n["title"] for n in body["novels"]]
    assert titles == ["Active"]


# ---------- /activity ----------

def test_activity_returns_recent_attempts_with_status_bucket(client: TestClient) -> None:
    pid = _seed_provider()
    nid = _seed_novel(pid, "Novel A")
    c1 = _seed_chapter(nid, 1)
    c2 = _seed_chapter(nid, 2)
    _seed_attempt(c1, pid, status="ok", days_ago=0)
    _seed_attempt(c2, pid, status="parse_failed", days_ago=0)
    _seed_attempt(c2, pid, status="error", days_ago=0)

    body = client.get(f"/api/providers/{pid}/activity?limit=10").json()
    assert len(body["events"]) == 3
    buckets = {e["raw_status"]: e["status"] for e in body["events"]}
    assert buckets["ok"] == "ok"
    assert buckets["parse_failed"] == "warn"
    assert buckets["error"] == "err"


# ---------- /api/diagnostics ----------

def test_diagnostics_returns_shape(client: TestClient) -> None:
    r = client.get("/api/diagnostics")
    assert r.status_code == 200
    body = r.json()
    for key in ("version", "frozen", "python", "platform", "data_root",
                "db_path", "log_folder", "covers_dir", "db_bytes",
                "cache_bytes", "covers_bytes", "library_bytes", "telemetry"):
        assert key in body, f"diagnostics missing {key!r}"
    assert isinstance(body["db_bytes"], int)
    assert body["telemetry"] == "off"


def test_diagnostics_log_folder_path(client: TestClient) -> None:
    r = client.get("/api/diagnostics/log-folder")
    assert r.status_code == 200
    assert "path" in r.json()
