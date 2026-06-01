"""HTTP-level tests for routes/observations.py (the QA dashboard endpoints).

The unit suite (test_phase3_observers.py) covers the detect_* observers and
the normalizer; this file pins the three reader-facing HTTP endpoints that
read/dismiss the persisted chapter_observations rows:

  * GET  /api/novels/{id}/observations                       — per-chapter
        undismissed counts + novel-wide total (ObservationsSummary).
  * GET  /api/novels/{id}/chapters/{n}/observations          — the ordered
        per-chapter list (only undismissed by default).
  * POST /api/observations/{id}/dismiss                      — soft-dismiss
        one row (stamps dismissed_at, drops it from the open counts).

Rows are seeded directly into SQLite so no translation/queue work runs — the
endpoints are pure reads over the table plus one UPDATE.
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
    """A TestClient whose startup probe + queue drain are stubbed so the
    lifespan doesn't reach for a real translator backend. Entering the
    context manager runs init_db(), creating a fresh schema in the temp DB."""
    if DB_PATH.exists():
        DB_PATH.unlink()

    async def _no_probe(default_provider):
        return None

    async def _no_drain():
        return None

    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)

    from backend.main import app

    with TestClient(app) as c:  # context manager fires the lifespan → init_db
        yield c


def _seed() -> tuple[int, int, list[int]]:
    """Insert one novel + one done chapter + three observations on it.

    Returns (novel_id, chapter_num, [observation_ids]). The explicit commit
    is required — sqlite3 defaults to deferred transactions, so close()
    without commit() rolls back."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('Obs Novel', 'paste')"
        )
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, original_text, translated_text, status) "
            "VALUES (?, 1, '原文', 'Body.', 'done')",
            (novel_id,),
        )
        chapter_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        obs_ids: list[int] = []
        seed_rows = [
            ("missing_glossary_term", "warn", "missing locked glossary term 'X'"),
            ("mt_texture", "warn", "mt-texture tics: couldn't help but"),
            ("double_possessive", "warn", "Double possessive on a name: Li's's"),
        ]
        for kind, severity, excerpt in seed_rows:
            conn.execute(
                "INSERT INTO chapter_observations "
                "(chapter_id, kind, severity, excerpt) VALUES (?, ?, ?, ?)",
                (chapter_id, kind, severity, excerpt),
            )
            obs_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
        return novel_id, 1, obs_ids
    finally:
        conn.close()


def test_novel_observations_summary(client: TestClient) -> None:
    """GET /api/novels/{id}/observations returns per-chapter counts keyed by
    chapter_num plus the novel-wide total."""
    novel_id, chapter_num, obs_ids = _seed()
    resp = client.get(f"/api/novels/{novel_id}/observations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_undismissed"] == 3
    # by_chapter is keyed by chapter_num. JSON object keys are strings.
    assert body["by_chapter"] == {str(chapter_num): 3}


def test_chapter_observations_ordered_list(client: TestClient) -> None:
    """GET /api/novels/{id}/chapters/{n}/observations returns the full
    per-chapter list, ordered by id (insertion order), undismissed only."""
    novel_id, chapter_num, obs_ids = _seed()
    resp = client.get(f"/api/novels/{novel_id}/chapters/{chapter_num}/observations")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [r["id"] for r in rows] == obs_ids  # stable insertion order
    # severity_tier is derived per-kind: missing_glossary_term is semantic,
    # the two stylistic observers are stylistic.
    tiers = {r["kind"]: r["severity_tier"] for r in rows}
    assert tiers["missing_glossary_term"] == "semantic"
    assert tiers["mt_texture"] == "stylistic"
    assert all(r["dismissed_at"] is None for r in rows)


def test_chapter_observations_unknown_chapter_404(client: TestClient) -> None:
    novel_id, _, _ = _seed()
    resp = client.get(f"/api/novels/{novel_id}/chapters/999/observations")
    assert resp.status_code == 404, resp.text


def test_dismiss_observation_drops_open_count(client: TestClient) -> None:
    """POST /api/observations/{id}/dismiss stamps dismissed_at, removes the
    row from the undismissed summary, and is reflected in the per-chapter
    list (which hides dismissed rows by default)."""
    novel_id, chapter_num, obs_ids = _seed()
    target = obs_ids[0]

    resp = client.post(f"/api/observations/{target}/dismiss")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}

    # The summary total drops by one.
    summary = client.get(f"/api/novels/{novel_id}/observations").json()
    assert summary["total_undismissed"] == 2
    assert summary["by_chapter"] == {str(chapter_num): 2}

    # The dismissed row no longer appears in the default (undismissed) list.
    listed = client.get(
        f"/api/novels/{novel_id}/chapters/{chapter_num}/observations"
    ).json()
    assert target not in [r["id"] for r in listed]
    assert len(listed) == 2

    # dismissed_at was actually written to the row.
    conn = sqlite3.connect(DB_PATH)
    try:
        stamped = conn.execute(
            "SELECT dismissed_at FROM chapter_observations WHERE id = ?",
            (target,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert stamped is not None


def test_dismiss_unknown_observation_404(client: TestClient) -> None:
    _seed()
    resp = client.post("/api/observations/999999/dismiss")
    assert resp.status_code == 404, resp.text
