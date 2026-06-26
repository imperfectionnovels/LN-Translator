"""HTTP + service tests for the quality / consistency cockpit routes.

Covers the read-only endpoints added with the in-app cockpit:
  * GET /api/novels/{id}/quality?chapters=LO-HI
  * GET /api/novels/{id}/consistency
  * GET /api/novels/{id}/chapters/{n}/quality

The scorers themselves are covered by test_quality_metrics / test_consistency_eval;
this pins the route reshaping, the worst-chapters / worst-terms triage payloads,
the per-chapter badge fields, and the error contract (404 missing, 400 bad range).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DB_PATH = Path(os.environ["DB_PATH"])


@pytest.fixture(autouse=True)
def _clear_quality_cache():
    # The dashboard cache is a module global keyed by novel_id; tests reuse
    # novel_id=1 across DB resets, so a stale entry could leak between tests.
    # Production never swaps the DB under a running process, so this is a
    # test-only concern.
    from backend.services import quality_dashboard as qd

    qd._cache.clear()
    qd._locks.clear()
    yield
    qd._cache.clear()
    qd._locks.clear()


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
    """A novel where two locked terms drift in the third chapter (TCR < 1 so
    worst_terms is populated), plus a fixup_audit blob and an observation on
    chapter 1. Returns novel_id."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO novels (title, source_type) VALUES ('N', 'paste')")
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Each source carries both zh terms (3 checkable occurrences each).
        src = "他取出一件灵宝，体内金丹震动。"
        chapters = [
            (1, src, "He drew out a Spirit Treasure as his Golden Core trembled."),
            (2, src, "He drew out a Spirit Treasure as his Golden Core trembled."),
            (3, src, "He drew out a Spiritual Treasure as his Gold Pill trembled."),
        ]
        fixup = json.dumps({"rules": {"enforce_em_dash": 3}, "total": 3})
        for n, s, t in chapters:
            conn.execute(
                "INSERT INTO chapters "
                "(novel_id, chapter_num, original_text, translated_text, status, "
                "translated_at, fixup_audit) "
                "VALUES (?, ?, ?, ?, 'done', '2026-06-01T00:00:00Z', ?)",
                (novel_id, n, s, t, fixup if n == 1 else None),
            )
        # Locked glossary so the terms are checkable for TCR.
        for zh, en in [("灵宝", "Spirit Treasure"), ("金丹", "Golden Core")]:
            conn.execute(
                "INSERT INTO glossary_entries "
                "(novel_id, term_zh, term_en, category, locked, auto_detected) "
                "VALUES (?, ?, ?, 'item', 1, 0)",
                (novel_id, zh, en),
            )
        ch1_id = conn.execute(
            "SELECT id FROM chapters WHERE novel_id=? AND chapter_num=1", (novel_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO chapter_observations (chapter_id, kind, excerpt) "
            "VALUES (?, 'mt_texture', 'sample')",
            (ch1_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return novel_id


def test_novel_quality_scorecard(client):
    nid = _seed()
    r = client.get(f"/api/novels/{nid}/quality", params={"chapters": "1-3"})
    assert r.status_code == 200
    card = r.json()
    assert card["chapters_scored"] == 3
    assert card["schema_outdated"] is False
    assert "glossary_presence" in card["categories"]
    worst = card["worst_chapters"]
    assert worst and set(worst[0]) >= {
        "chapter_num", "title_en", "violations", "opportunities", "rate", "fixup_total",
    }
    # Chapter 1 carried the fixup_audit blob; it should surface in the churn harvest.
    assert card["fixup_churn"]["rule_counts"].get("enforce_em_dash") == 3


def test_novel_quality_empty_range_404(client):
    nid = _seed()
    r = client.get(f"/api/novels/{nid}/quality", params={"chapters": "900-999"})
    assert r.status_code == 404


def test_novel_quality_bad_range_400(client):
    nid = _seed()
    r = client.get(f"/api/novels/{nid}/quality", params={"chapters": "oops"})
    assert r.status_code == 400


def test_novel_consistency_worst_terms_carry_id(client):
    nid = _seed()
    r = client.get(f"/api/novels/{nid}/consistency")
    assert r.status_code == 200
    rep = r.json()
    assert 0.0 <= rep["tcr"]["overall_tcr"] <= 1.0
    worst = rep["tcr"]["worst_terms"]
    assert worst, "expected drifting terms to populate worst_terms"
    assert all("id" in w for w in worst), "worst_terms must carry the glossary id for deep-linking"


def test_chapter_quality_badge(client):
    nid = _seed()
    r = client.get(f"/api/novels/{nid}/chapters/1/quality")
    assert r.status_code == 200
    d = r.json()
    assert d["scored"] is True
    assert d["observer_hits"] == 1
    assert d["fixup_total"] == 3
    assert d["fixup_rules"].get("enforce_em_dash") == 3
    assert isinstance(d["categories"], list)


def test_chapter_quality_missing_404(client):
    nid = _seed()
    assert client.get(f"/api/novels/{nid}/chapters/99/quality").status_code == 404


def test_missing_novel_404(client):
    _seed()
    assert client.get("/api/novels/999/consistency").status_code == 404
    assert client.get("/api/novels/999/quality").status_code == 404


def _seed_refined() -> int:
    """A refined novel where the DRAFT renders a locked term consistently but
    the refined body the reader actually sees drifts it in the last chapter.
    The cockpit must score the refined body (COALESCE), so TCR must be < 1."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO novels (title, source_type) VALUES ('R', 'paste')")
        nid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        src = "他取出一件灵宝。"
        rows = [
            (1, "He drew out a Spirit Treasure.", "He drew out a Spirit Treasure."),
            (2, "He drew out a Spirit Treasure.", "He drew out a Spirit Treasure."),
            # Draft consistent, refined drifts the locked term:
            (3, "He drew out a Spirit Treasure.", "He drew out a Spiritual Treasure."),
        ]
        for n, draft, refined in rows:
            conn.execute(
                "INSERT INTO chapters (novel_id, chapter_num, original_text, "
                "translated_text, refined_text, status, translated_at, refined_at) "
                "VALUES (?, ?, ?, ?, ?, 'done', '2026-06-01T00:00:00Z', "
                "'2026-06-01T00:00:00Z')",
                (nid, n, src, draft, refined),
            )
        conn.execute(
            "INSERT INTO glossary_entries (novel_id, term_zh, term_en, category, "
            "locked, auto_detected) VALUES (?, '灵宝', 'Spirit Treasure', 'item', 1, 0)",
            (nid,),
        )
        conn.commit()
    finally:
        conn.close()
    return nid


def test_consistency_scores_refined_body_not_draft(client):
    # If the scan read translated_text (consistent across all 3), TCR would be
    # 1.0 and worst_terms empty. Scoring the refined body surfaces the ch3 drift.
    nid = _seed_refined()
    rep = client.get(f"/api/novels/{nid}/consistency").json()
    assert rep["tcr"]["overall_tcr"] < 1.0
    assert rep["tcr"]["worst_terms"], "refined-body drift must surface in worst_terms"


def test_version_token_busts_on_inline_edit(client):
    # An inline paragraph edit inserts a style_edits row without bumping
    # translated_at; the version token must still change so the cached
    # scorecard/consistency do not serve pre-edit data.
    import asyncio

    from backend.services import quality_dashboard as qd

    nid = _seed()
    before = asyncio.run(qd._version_token(nid))
    conn = sqlite3.connect(DB_PATH)
    try:
        ch1 = conn.execute(
            "SELECT id FROM chapters WHERE novel_id=? AND chapter_num=1", (nid,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO style_edits (novel_id, chapter_id, before_text, after_text) "
            "VALUES (?, ?, 'a', 'b')",
            (nid, ch1),
        )
        conn.commit()
    finally:
        conn.close()
    after = asyncio.run(qd._version_token(nid))
    assert before != after, "inline edit (style_edits insert) must bust the cache token"


def test_scorecard_reuses_consistency_scan(client, monkeypatch):
    # The quality page loads scorecard + consistency together. The full-novel
    # consistency scan must run ONCE (scorecard reuses the cached result), not
    # once per endpoint.
    from backend.services import quality_dashboard as qd

    nid = _seed()
    calls = {"n": 0}
    real = qd._consistency_report

    def _counting(novel_id, data):
        calls["n"] += 1
        return real(novel_id, data)

    monkeypatch.setattr(qd, "_consistency_report", _counting)
    assert client.get(
        f"/api/novels/{nid}/quality", params={"chapters": "1-3"}
    ).status_code == 200
    assert client.get(f"/api/novels/{nid}/consistency").status_code == 200
    assert calls["n"] == 1, "consistency scan must run once across both endpoints"
