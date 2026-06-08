"""HTTP-level tests for routes/global_glossary.py (mounted at /api).

This is the owning test for `backend.routes.global_glossary`: it imports the
module at top level (and asserts on its router shape + the inline request
model) and drives every endpoint end-to-end against a real temp DB. The global
glossary is pure CRUD over `global_glossary_entries` plus a per-novel usage
JOIN and an in-place text substitution across chapters, no translation/queue
work runs.

Coverage:
  * GET    /api/glossary/global, list (sorted)
  * POST   /api/glossary/global, create (201) + 409 dup
  * PATCH  /api/glossary/global/{id}, update + empty-body 400 + 404
  * DELETE /api/glossary/global/{id}, remove + double-delete 404
  * GET    /api/glossary/global/{id}/usage, per-novel counts + 404
  * POST   /api/glossary/global/{id}/apply-in-place, word-boundary substitution
  * POST   /api/glossary/{id}/promote-to-global, per-novel -> global + 409
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Direct import: this file is the owning test for the global glossary module.
from backend.routes import global_glossary

DB_PATH = Path(os.environ["DB_PATH"])


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


def _seed_novel_with_chapter(
    title: str,
    *,
    original_text: str = "原文",
    translated_text: str | None = None,
) -> tuple[int, int]:
    """Insert one novel + one done chapter. Returns (novel_id, chapter_id)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, 'paste')",
            (title,),
        )
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, original_text, status, translated_text) "
            "VALUES (?, 1, ?, 'done', ?)",
            (novel_id, original_text, translated_text),
        )
        chapter_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return novel_id, chapter_id
    finally:
        conn.close()


def _seed_per_novel_entry(
    novel_id: int, term_zh: str, term_en: str, category: str = "character"
) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO glossary_entries "
            "(novel_id, term_zh, term_en, category, locked, auto_detected) "
            "VALUES (?, ?, ?, ?, 1, 0)",
            (novel_id, term_zh, term_en, category),
        )
        entry_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return entry_id
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Router-shape assertion (keeps the coverage mapping honest beyond the import).
# --------------------------------------------------------------------------

def test_router_exposes_expected_global_paths():
    paths = {r.path for r in global_glossary.router.routes}
    assert "/glossary/global" in paths
    assert "/glossary/global/{entry_id}" in paths
    assert "/glossary/{entry_id}/promote-to-global" in paths
    # The inline request model enforces a non-empty old_en/new_en contract.
    fields = global_glossary.GlobalApplyInPlaceRequest.model_fields
    assert set(fields) == {"old_en", "new_en"}


# --------------------------------------------------------------------------
# CRUD endpoint tests.
# --------------------------------------------------------------------------

def test_create_and_list_global_entries_sorted(client):
    # Insert out of (category, term_zh) order; list must come back sorted.
    a = client.post(
        "/api/glossary/global",
        json={"term_zh": "灵气", "term_en": "Spiritual Energy", "category": "technique"},
    )
    b = client.post(
        "/api/glossary/global",
        json={"term_zh": "金丹", "term_en": "Golden Core", "category": "item"},
    )
    assert a.status_code == 201
    assert b.status_code == 201
    created = a.json()
    assert created["term_zh"] == "灵气"
    assert created["term_en"] == "Spiritual Energy"
    assert created["id"] > 0

    listed = client.get("/api/glossary/global")
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 2
    # Sorted by category, then term_zh: 'item' before 'technique'.
    assert [r["category"] for r in rows] == ["item", "technique"]


def test_create_global_duplicate_term_zh_409_with_existing(client):
    first = client.post(
        "/api/glossary/global",
        json={"term_zh": "道", "term_en": "Dao", "category": "other"},
    )
    assert first.status_code == 201
    dup = client.post(
        "/api/glossary/global",
        json={"term_zh": "道", "term_en": "The Way", "category": "other"},
    )
    assert dup.status_code == 409
    detail = dup.json()["detail"]
    # The 409 body carries the existing entry so the UI can offer "edit existing".
    assert detail["existing"]["term_en"] == "Dao"
    assert detail["existing"]["id"] == first.json()["id"]


def test_update_global_entry_and_empty_body_and_404(client):
    created = client.post(
        "/api/glossary/global",
        json={"term_zh": "剑", "term_en": "Sword", "category": "item"},
    ).json()
    entry_id = created["id"]

    updated = client.patch(
        f"/api/glossary/global/{entry_id}",
        json={"term_en": "Blade", "notes": "weapon"},
    )
    assert updated.status_code == 200
    assert updated.json()["term_en"] == "Blade"
    assert updated.json()["notes"] == "weapon"

    empty = client.patch(f"/api/glossary/global/{entry_id}", json={})
    assert empty.status_code == 400
    assert empty.json()["detail"] == "no fields to update"

    missing = client.patch(
        "/api/glossary/global/999999", json={"term_en": "Nope"}
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "global entry not found"


def test_delete_global_then_double_delete_404(client):
    created = client.post(
        "/api/glossary/global",
        json={"term_zh": "气", "term_en": "Qi", "category": "other"},
    ).json()
    entry_id = created["id"]

    first = client.delete(f"/api/glossary/global/{entry_id}")
    assert first.status_code == 200
    assert first.json() == {"ok": True}
    # Gone from the list.
    assert client.get("/api/glossary/global").json() == []
    # Second delete is a clean 404, not a silent success.
    second = client.delete(f"/api/glossary/global/{entry_id}")
    assert second.status_code == 404
    assert second.json()["detail"] == "global entry not found"


def test_usage_global_counts_chapters_per_novel_and_404(client):
    # Two novels whose original_text contains the term; one that doesn't.
    n1, _ = _seed_novel_with_chapter("Hits A", original_text="他练成了金丹境界")
    n2, _ = _seed_novel_with_chapter("Hits B", original_text="金丹再次出现金丹")
    _seed_novel_with_chapter("No Hit", original_text="毫无关联的内容")

    created = client.post(
        "/api/glossary/global",
        json={"term_zh": "金丹", "term_en": "Golden Core", "category": "item"},
    ).json()

    usage = client.get(f"/api/glossary/global/{created['id']}/usage")
    assert usage.status_code == 200
    rows = usage.json()
    # Only the two novels that contain the term, sorted by novel id ASC.
    by_id = {r["novel_id"]: r for r in rows}
    assert set(by_id) == {n1, n2}
    assert by_id[n1]["chapter_count"] == 1
    assert by_id[n1]["novel_title"] == "Hits A"

    missing = client.get("/api/glossary/global/999999/usage")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "global entry not found"


def test_apply_in_place_global_substitutes_across_chapters(client):
    # Two novels each with one translated chapter using the old rendering.
    _seed_novel_with_chapter(
        "Apply A", translated_text="The Golden Seat shimmered with the Golden Seat."
    )
    _seed_novel_with_chapter(
        "Apply B", translated_text="A single Golden Seat appeared."
    )
    created = client.post(
        "/api/glossary/global",
        json={"term_zh": "金丹", "term_en": "Golden Seat", "category": "item"},
    ).json()

    resp = client.post(
        f"/api/glossary/global/{created['id']}/apply-in-place",
        json={"old_en": "Golden Seat", "new_en": "Golden Core"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Both chapters were touched (one per novel).
    assert body["chapters_updated"] == 2
    assert body["rows_updated_translated"] == 2

    # Verify the substitution actually landed in the stored translated_text.
    conn = sqlite3.connect(DB_PATH)
    texts = [r[0] for r in conn.execute(
        "SELECT translated_text FROM chapters ORDER BY id"
    ).fetchall()]
    conn.close()
    assert all("Golden Seat" not in t for t in texts)
    assert any("Golden Core" in t for t in texts)


def test_apply_in_place_respects_word_boundary_and_case(client):
    """The substitution is `\\b`-anchored and case-sensitive: a plural
    ('Golden Seats') and a lowercase form ('golden seat') must survive, while
    the exact 'Golden Seat' token is replaced."""
    _seed_novel_with_chapter(
        "Boundary",
        translated_text="The Golden Seat, the Golden Seats, and a golden seat.",
    )
    created = client.post(
        "/api/glossary/global",
        json={"term_zh": "金丹", "term_en": "Golden Seat", "category": "item"},
    ).json()

    resp = client.post(
        f"/api/glossary/global/{created['id']}/apply-in-place",
        json={"old_en": "Golden Seat", "new_en": "Golden Core"},
    )
    assert resp.status_code == 200

    conn = sqlite3.connect(DB_PATH)
    text = conn.execute(
        "SELECT translated_text FROM chapters ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    conn.close()
    # Exact token replaced; plural and lowercase left untouched.
    assert text == "The Golden Core, the Golden Seats, and a golden seat."


def test_apply_in_place_global_404_for_unknown_entry(client):
    resp = client.post(
        "/api/glossary/global/999999/apply-in-place",
        json={"old_en": "X", "new_en": "Y"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "global entry not found"


def test_promote_per_novel_entry_to_global(client):
    novel_id, _ = _seed_novel_with_chapter("Promote Src")
    entry_id = _seed_per_novel_entry(novel_id, "筑基", "Foundation Establishment")

    resp = client.post(f"/api/glossary/{entry_id}/promote-to-global")
    assert resp.status_code == 201
    promoted = resp.json()
    assert promoted["term_zh"] == "筑基"
    assert promoted["term_en"] == "Foundation Establishment"

    # It now lives in the global table...
    globals_now = client.get("/api/glossary/global").json()
    assert any(g["term_zh"] == "筑基" for g in globals_now)
    # ...and the per-novel source row was deleted (atomic move).
    conn = sqlite3.connect(DB_PATH)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM glossary_entries WHERE id = ?", (entry_id,)
    ).fetchone()[0]
    conn.close()
    assert remaining == 0


def test_promote_conflicts_with_existing_global_409(client):
    novel_id, _ = _seed_novel_with_chapter("Promote Conflict")
    entry_id = _seed_per_novel_entry(novel_id, "元婴", "Nascent Soul")
    # A global already owns this term_zh.
    client.post(
        "/api/glossary/global",
        json={"term_zh": "元婴", "term_en": "Yuan Ying", "category": "character"},
    )

    resp = client.post(f"/api/glossary/{entry_id}/promote-to-global")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["existing"]["term_en"] == "Yuan Ying"
    # The per-novel row must survive a refused promote (no half-move).
    conn = sqlite3.connect(DB_PATH)
    survives = conn.execute(
        "SELECT COUNT(*) FROM glossary_entries WHERE id = ?", (entry_id,)
    ).fetchone()[0]
    conn.close()
    assert survives == 1


def test_promote_unknown_per_novel_entry_404(client):
    resp = client.post("/api/glossary/999999/promote-to-global")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "entry not found"
