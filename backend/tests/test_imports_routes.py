"""HTTP-level tests for routes/imports.py (the resumable-import status feed).

The import endpoints drive the library card's in-progress / paused / resume
UX. They are thin reads + status flips over the `novels.import_status` column
and the per-chapter `import_source_url` / `import_fetched_at` markers:

  * GET  /api/imports/{id}/status, count totals, fetched, resumability
  * POST /api/imports/{id}/cancel, flip 'in_progress' → 'paused'
  * POST /api/imports/{id}/resume, re-fire the runner for a paused scrape

No real scrape / runner work runs here: rows are seeded directly into SQLite
to put a novel in each lifecycle state, and `import_runner.spawn_resume` is
stubbed so the /resume happy path never spawns a background fetch task.

Importing the route module at top level keeps the coverage mapping honest, this file is the owning test for routes/imports.py.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Direct import: this file owns routes/imports.py for coverage purposes.
from backend.routes import imports as imports_route

DB_PATH = Path(os.environ["DB_PATH"])


@pytest.fixture
def client(monkeypatch):
    """TestClient with the startup probe + queue drain + import drain stubbed
    so the lifespan never reaches for a real translator or fires a runner.
    Entering the context manager runs init_db() against the fresh temp DB."""
    if DB_PATH.exists():
        DB_PATH.unlink()

    async def _no_probe(default_provider):
        return None

    async def _no_drain():
        return None

    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)
    monkeypatch.setattr(
        "backend.services.import_runner.drain_imports_on_startup", _no_drain
    )

    from backend.main import app

    with TestClient(app) as c:
        yield c


def _insert_novel(import_status: str | None, title: str = "Imp Novel") -> int:
    """Insert a novel row with the given import_status. Returns novel_id."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type, source_url, import_status) "
            "VALUES (?, 'url', 'http://example.test/book', ?)",
            (title, import_status),
        )
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return novel_id
    finally:
        conn.close()


def _insert_chapter(
    novel_id: int,
    chapter_num: int,
    *,
    original_text: str = "",
    import_source_url: str | None = None,
    fetched: bool = False,
) -> None:
    """Insert one chapter row. A 'pending skeleton' is original_text='' with an
    import_source_url and import_fetched_at NULL; a 'filled' row has either
    non-empty text or a fetched timestamp."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, original_text, status, "
            " import_source_url, import_fetched_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (
                novel_id,
                chapter_num,
                original_text,
                import_source_url,
                "2026-01-01 00:00:00" if fetched else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _import_status_of(novel_id: int) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT import_status FROM novels WHERE id = ?", (novel_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_router_exposes_status_cancel_resume():
    """The router carries exactly the three import surfaces."""
    paths = {(r.path, tuple(sorted(r.methods - {"HEAD", "OPTIONS"})))
             for r in imports_route.router.routes}
    assert ("/{novel_id}/status", ("GET",)) in paths
    assert ("/{novel_id}/cancel", ("POST",)) in paths
    assert ("/{novel_id}/resume", ("POST",)) in paths


def test_status_in_progress_with_pending_and_fetched(client):
    """A scrape mid-fill: some skeletons fetched, some still pending. fetched
    counts filled rows; resumable is True while any URL-bearing row is unfetched."""
    novel_id = _insert_novel("in_progress", title="Mid Crawl")
    # Two filled (one via text, one via fetched timestamp) and one pending skeleton.
    _insert_chapter(novel_id, 1, original_text="第一章正文",
                    import_source_url="http://example.test/1", fetched=True)
    _insert_chapter(novel_id, 2, original_text="",
                    import_source_url="http://example.test/2", fetched=True)
    _insert_chapter(novel_id, 3, original_text="",
                    import_source_url="http://example.test/3", fetched=False)

    resp = client.get(f"/api/imports/{novel_id}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["novel_id"] == novel_id
    assert body["title"] == "Mid Crawl"
    assert body["status"] == "in_progress"
    assert body["total_chapters"] == 3
    # ch1 (text) + ch2 (fetched ts) are counted; ch3 is unfetched/empty.
    assert body["fetched_chapters"] == 2
    # ch3 still has a URL and no fetched_at → resumable.
    assert body["resumable"] is True


def test_status_done_no_pending_not_resumable(client):
    """A finished import: all chapters fetched, nothing pending → not resumable."""
    novel_id = _insert_novel("done", title="Complete")
    _insert_chapter(novel_id, 1, original_text="正文",
                    import_source_url="http://example.test/1", fetched=True)
    _insert_chapter(novel_id, 2, original_text="正文",
                    import_source_url="http://example.test/2", fetched=True)

    resp = client.get(f"/api/imports/{novel_id}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "done"
    assert body["total_chapters"] == 2
    assert body["fetched_chapters"] == 2
    assert body["resumable"] is False


def test_status_novel_with_no_chapters(client):
    """A novel row with zero chapters reports zero totals and not resumable, the empty-result edge for the COUNT/SUM aggregate (SUM over no rows = NULL)."""
    novel_id = _insert_novel("paused", title="Empty Shell")
    resp = client.get(f"/api/imports/{novel_id}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_chapters"] == 0
    assert body["fetched_chapters"] == 0
    assert body["resumable"] is False
    assert body["status"] == "paused"


def test_status_unknown_novel_404(client):
    resp = client.get("/api/imports/999999/status")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "novel not found"


def test_cancel_flips_in_progress_to_paused(client):
    """Cancel flips an in-progress novel to paused and reports flipped=True;
    the column is actually rewritten."""
    novel_id = _insert_novel("in_progress", title="Cancel Me")
    resp = client.post(f"/api/imports/{novel_id}/cancel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["novel_id"] == novel_id
    assert body["flipped"] is True
    assert body["status"] == "paused"
    # The DB row was actually updated.
    assert _import_status_of(novel_id) == "paused"


def test_cancel_noop_when_not_in_progress(client):
    """Cancelling a novel that isn't in_progress is a no-op: flipped=False and
    the stored status is left as-is."""
    novel_id = _insert_novel("done", title="Already Done")
    resp = client.post(f"/api/imports/{novel_id}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["flipped"] is False
    # Status was NOT clobbered to paused.
    assert _import_status_of(novel_id) == "done"


def test_cancel_unknown_novel_404(client):
    resp = client.post("/api/imports/424242/cancel")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "novel not found"


def test_resume_paused_recipe_import_spawns_runner(client, monkeypatch):
    """Resume on a paused novel with pending recipe-scrape chapters flips it
    back to in_progress, reports the pending count, and spawns the runner."""
    spawned: list[int] = []
    monkeypatch.setattr(
        "backend.services.import_runner.spawn_resume",
        lambda nid: spawned.append(nid),
    )

    novel_id = _insert_novel("paused", title="Resume Me")
    # One filled, two pending skeletons (URL set, not fetched).
    _insert_chapter(novel_id, 1, original_text="正文",
                    import_source_url="http://example.test/1", fetched=True)
    _insert_chapter(novel_id, 2, original_text="",
                    import_source_url="http://example.test/2", fetched=False)
    _insert_chapter(novel_id, 3, original_text="",
                    import_source_url="http://example.test/3", fetched=False)

    resp = client.post(f"/api/imports/{novel_id}/resume")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "in_progress"
    assert body["pending_chapters"] == 2
    # The status column was flipped back so the fill loop's check passes.
    assert _import_status_of(novel_id) == "in_progress"
    # The runner was spawned exactly once, for this novel.
    assert spawned == [novel_id]


def test_resume_rejects_non_paused_status_400(client, monkeypatch):
    """A novel that's already done can't be resumed: 400, no runner spawn, and
    the status is left untouched."""
    spawned: list[int] = []
    monkeypatch.setattr(
        "backend.services.import_runner.spawn_resume",
        lambda nid: spawned.append(nid),
    )
    novel_id = _insert_novel("done", title="Finished")
    _insert_chapter(novel_id, 1, original_text="正文",
                    import_source_url="http://example.test/1", fetched=True)

    resp = client.post(f"/api/imports/{novel_id}/resume")
    assert resp.status_code == 400
    assert "import_status" in resp.json()["detail"]
    assert spawned == []
    assert _import_status_of(novel_id) == "done"


def test_resume_paused_bulk_import_has_nothing_to_resume_400(client, monkeypatch):
    """A paused bulk/EPUB novel (no chapters carrying import_source_url) can't
    be resumed: 400 with the 'nothing to resume' message, no spawn, no flip."""
    spawned: list[int] = []
    monkeypatch.setattr(
        "backend.services.import_runner.spawn_resume",
        lambda nid: spawned.append(nid),
    )
    novel_id = _insert_novel("paused", title="Bulk Partial")
    # Bulk rows have text but NO import_source_url → not resumable.
    _insert_chapter(novel_id, 1, original_text="正文一", import_source_url=None)
    _insert_chapter(novel_id, 2, original_text="正文二", import_source_url=None)

    resp = client.post(f"/api/imports/{novel_id}/resume")
    assert resp.status_code == 400
    assert "Nothing to resume" in resp.json()["detail"]
    assert spawned == []
    # Status stays paused, the route only flips after the pending-count guard.
    assert _import_status_of(novel_id) == "paused"


def test_resume_unknown_novel_404(client):
    resp = client.post("/api/imports/777777/resume")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "novel not found"
