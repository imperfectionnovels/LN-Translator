"""HTTP-level + helper tests for routes/glossary.py (per-novel glossary).

This is the owning test for `backend.routes.glossary`: the module is imported
at top level and referenced directly (route table + `_safe_filename` helper)
so the coverage map credits these tests to that module rather than reaching it
only transitively through the app.

Everything runs against a real temp DB seeded via plain `sqlite3`. The only
mock is the queue's `spawn_translate_worker` boundary on the
`/retranslate-affected` path, without that stub the endpoint would spawn a
real translator task. The DB-state assertions (translate_queued flags, queued
counts) still exercise the genuine endpoint behavior.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Direct import: these tests own backend.routes.glossary.
from backend.routes import glossary as glossary_route

DB_PATH = Path(os.environ["DB_PATH"])


@pytest.fixture
def client(monkeypatch):
    """TestClient with the startup probe + queue drain stubbed so the lifespan
    never reaches a real translator. Entering the context manager runs
    init_db() against the fresh temp DB."""
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


def _seed_novel(title: str = "Glossary Novel") -> int:
    """Insert one novel and return its id. The explicit commit is required, sqlite3 deferred transactions roll back on close() without it."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, 'paste')",
            (title,),
        )
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return novel_id
    finally:
        conn.close()


def _seed_chapter(novel_id: int, chapter_num: int, original_text: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, status) "
            "VALUES (?, ?, ?, 'done')",
            (novel_id, chapter_num, original_text),
        )
        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return cid
    finally:
        conn.close()


# ---- Pure helper: _safe_filename ----------------------------------------


def test_safe_filename_strips_unfriendly_chars_and_truncates():
    """_safe_filename collapses non-word runs (spaces, colons, slashes) to
    single underscores, caps length at 60, and falls back to 'glossary' on
    empty/blank input. `\\w` is Unicode-aware so CJK characters are kept."""
    # Spaces/colons/slashes become underscores; ASCII word chars + digits stay.
    assert glossary_route._safe_filename("My Novel: Vol/1") == "My_Novel_Vol_1"
    assert glossary_route._safe_filename("") == "glossary"
    assert glossary_route._safe_filename("   ") == "glossary"
    # A name that is purely punctuation collapses every non-word run to a
    # single underscore (the result is truthy, so no fallback).
    assert glossary_route._safe_filename("***") == "_"
    # 100 'a's stay as word chars but get truncated to the 60-char cap.
    long_name = glossary_route._safe_filename("a" * 100)
    assert len(long_name) == 60
    assert set(long_name) == {"a"}


def test_glossary_router_exposes_expected_routes():
    """Sanity-pin the route table so a renamed/dropped endpoint is caught."""
    paths = {r.path for r in glossary_route.router.routes}
    assert "/novels/{novel_id}/glossary" in paths
    assert "/novels/{novel_id}/glossary/export" in paths
    assert "/glossary/{entry_id}" in paths
    assert "/glossary/{entry_id}/affected-chapters" in paths


# ---- CRUD: create / list / patch (locks) / delete ------------------------


def test_create_then_list_glossary_entry(client):
    novel_id = _seed_novel()
    resp = client.post(
        f"/api/novels/{novel_id}/glossary",
        json={"term_zh": "白小纯", "term_en": "Bai Xiaochun", "category": "character"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["term_zh"] == "白小纯"
    assert body["term_en"] == "Bai Xiaochun"
    # A manual add is locked + not auto-detected.
    assert body["locked"] is True
    assert body["auto_detected"] is False

    listing = client.get(f"/api/novels/{novel_id}/glossary")
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    assert rows[0]["id"] == body["id"]
    assert rows[0]["term_en"] == "Bai Xiaochun"


def test_create_empty_term_en_rejected_as_422(client):
    """Pydantic min_length=1 rejects an empty term_en before the handler runs."""
    novel_id = _seed_novel()
    resp = client.post(
        f"/api/novels/{novel_id}/glossary",
        json={"term_zh": "灵气", "term_en": "", "category": "other"},
    )
    assert resp.status_code == 422
    # The validation error names the offending field.
    assert any(err["loc"][-1] == "term_en" for err in resp.json()["detail"])


def test_create_locked_duplicate_returns_409(client):
    """A second manual add of the same locked term_zh is a 409 conflict, not
    a silent overwrite."""
    novel_id = _seed_novel()
    first = client.post(
        f"/api/novels/{novel_id}/glossary",
        json={"term_zh": "金丹", "term_en": "Golden Core", "category": "technique"},
    )
    assert first.status_code == 201
    dup = client.post(
        f"/api/novels/{novel_id}/glossary",
        json={"term_zh": "金丹", "term_en": "Golden Seat", "category": "technique"},
    )
    assert dup.status_code == 409
    assert "locked" in dup.json()["detail"]
    # The original rendering survived the rejected overwrite.
    rows = client.get(f"/api/novels/{novel_id}/glossary").json()
    assert len(rows) == 1
    assert rows[0]["term_en"] == "Golden Core"


def _seed_unlocked_entry(novel_id: int, term_zh: str, term_en: str) -> int:
    """Insert an auto-detected, UNLOCKED glossary row (as auto-extraction would),
    so a PATCH can prove the implicit lock-on-edit rather than starting locked."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO glossary_entries "
            "(novel_id, term_zh, term_en, category, auto_detected, locked) "
            "VALUES (?, ?, ?, 'technique', 1, 0)",
            (novel_id, term_zh, term_en),
        )
        eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return eid
    finally:
        conn.close()


def test_patch_implicitly_locks_unlocked_entry(client):
    """A PATCH on an unlocked auto-detected entry updates the field AND flips
    locked 0->1 so later auto-detection can't clobber the user's edit."""
    novel_id = _seed_novel()
    entry_id = _seed_unlocked_entry(novel_id, "聚气", "Qi Gathering")

    # Precondition: it really starts unlocked.
    before = client.get(f"/api/novels/{novel_id}/glossary").json()
    seeded = next(e for e in before if e["id"] == entry_id)
    assert seeded["locked"] is False

    patched = client.patch(
        f"/api/glossary/{entry_id}",
        json={"term_en": "Qi Condensation", "notes": "renamed"},
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["term_en"] == "Qi Condensation"
    assert body["notes"] == "renamed"
    # The edit itself flipped the lock (the load-bearing behavior).
    assert body["locked"] is True


def test_patch_unknown_entry_404(client):
    resp = client.patch("/api/glossary/999999", json={"term_en": "Whatever"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "entry not found"


def test_delete_then_double_delete_404(client):
    novel_id = _seed_novel()
    created = client.post(
        f"/api/novels/{novel_id}/glossary",
        json={"term_zh": "丹田", "term_en": "Dantian", "category": "other"},
    ).json()
    entry_id = created["id"]

    first = client.delete(f"/api/glossary/{entry_id}")
    assert first.status_code == 200
    assert first.json() == {"ok": True}
    # Gone from the listing.
    assert client.get(f"/api/novels/{novel_id}/glossary").json() == []
    # A second delete is a clean 404, not a silent success.
    second = client.delete(f"/api/glossary/{entry_id}")
    assert second.status_code == 404
    assert second.json()["detail"] == "entry not found"


# ---- Export (CSV + Markdown + 404) --------------------------------------


def test_export_csv_contains_header_and_rows(client):
    novel_id = _seed_novel("Export Test")
    client.post(
        f"/api/novels/{novel_id}/glossary",
        json={"term_zh": "白小纯", "term_en": "Bai Xiaochun", "category": "character"},
    )
    resp = client.get(f"/api/novels/{novel_id}/glossary/export?format=csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "Export_Test_glossary.csv" in resp.headers["content-disposition"]
    text = resp.text
    assert "term_zh,term_en,category,locked,auto_detected,notes" in text
    assert "白小纯,Bai Xiaochun,character,1,0," in text


def test_export_markdown_groups_by_category(client):
    novel_id = _seed_novel("MD Novel")
    client.post(
        f"/api/novels/{novel_id}/glossary",
        json={"term_zh": "白小纯", "term_en": "Bai Xiaochun", "category": "character"},
    )
    resp = client.get(f"/api/novels/{novel_id}/glossary/export?format=md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    text = resp.text
    # Header uses a colon (no em-dash, per the project's no-em-dash rule).
    assert "# Glossary: MD Novel" in text
    assert "—" not in text and "–" not in text
    assert "## character" in text
    # Locked manual entry renders the check mark in the Locked column.
    assert "| 白小纯 | Bai Xiaochun | ✓ |" in text


def test_export_unknown_novel_404(client):
    resp = client.get("/api/novels/424242/glossary/export?format=csv")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "novel not found"


def test_export_rejects_unknown_format_422(client):
    """The `format` query param is pattern-constrained to csv|md."""
    novel_id = _seed_novel()
    resp = client.get(f"/api/novels/{novel_id}/glossary/export?format=pdf")
    assert resp.status_code == 422
    assert "format" in str(resp.json()["detail"])


# ---- affected-chapters + health -----------------------------------------


def test_affected_chapters_lists_chapters_containing_term(client):
    novel_id = _seed_novel()
    _seed_chapter(novel_id, 1, "白小纯走进大殿。")  # contains the term
    _seed_chapter(novel_id, 2, "宗门一片寂静。")    # does not
    entry = client.post(
        f"/api/novels/{novel_id}/glossary",
        json={"term_zh": "白小纯", "term_en": "Bai Xiaochun", "category": "character"},
    ).json()

    resp = client.get(f"/api/glossary/{entry['id']}/affected-chapters")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["chapter_num"] == 1
    assert rows[0]["translate_queued"] is False


def test_affected_chapters_unknown_entry_404(client):
    resp = client.get("/api/glossary/777777/affected-chapters")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "entry not found"


def test_glossary_health_flags_duplicate_en_and_unused(client):
    novel_id = _seed_novel()
    # One chapter mentions 白小纯 but neither of the duplicate-English terms.
    _seed_chapter(novel_id, 1, "白小纯走进大殿。")
    client.post(
        f"/api/novels/{novel_id}/glossary",
        json={"term_zh": "白小纯", "term_en": "Bai Xiaochun", "category": "character"},
    )
    # Two distinct Chinese terms share the same English -> duplicate_en, and
    # neither appears in any chapter's original_text -> unused.
    client.post(
        f"/api/novels/{novel_id}/glossary",
        json={"term_zh": "苍穹", "term_en": "Firmament", "category": "place"},
    )
    client.post(
        f"/api/novels/{novel_id}/glossary",
        json={"term_zh": "天穹", "term_en": "Firmament", "category": "place"},
    )

    resp = client.get(f"/api/novels/{novel_id}/glossary/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_entries"] == 3
    # The two "Firmament" entries collide on English.
    dup_en_terms = {d["term_en"] for d in body["duplicate_en"]}
    assert dup_en_terms == {"Firmament"}
    # 苍穹 + 天穹 are unused (absent from chapter text); 白小纯 is used.
    unused_zh = {u["term_zh"] for u in body["unused"]}
    assert unused_zh == {"苍穹", "天穹"}


# ---- bulk-delete / bulk-lock --------------------------------------------


def test_bulk_delete_scoped_to_novel(client):
    novel_id = _seed_novel("A")
    other_id = _seed_novel("B")
    e1 = client.post(
        f"/api/novels/{novel_id}/glossary",
        json={"term_zh": "甲", "term_en": "Alpha", "category": "other"},
    ).json()
    e2 = client.post(
        f"/api/novels/{novel_id}/glossary",
        json={"term_zh": "乙", "term_en": "Beta", "category": "other"},
    ).json()
    other = client.post(
        f"/api/novels/{other_id}/glossary",
        json={"term_zh": "丙", "term_en": "Gamma", "category": "other"},
    ).json()

    # Pass all three ids but scope to novel_id: the other-novel id is ignored.
    resp = client.post(
        "/api/glossary/bulk-delete",
        json={"novel_id": novel_id, "ids": [e1["id"], e2["id"], other["id"]]},
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    # The other novel's entry survived the scope guard.
    assert client.get(f"/api/novels/{novel_id}/glossary").json() == []
    surviving = client.get(f"/api/novels/{other_id}/glossary").json()
    assert [r["id"] for r in surviving] == [other["id"]]


def test_bulk_delete_empty_ids_is_noop(client):
    novel_id = _seed_novel()
    resp = client.post(
        "/api/glossary/bulk-delete", json={"novel_id": novel_id, "ids": []}
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 0}


def test_bulk_lock_toggles_locked_flag(client):
    novel_id = _seed_novel()
    # Seed an UNLOCKED auto-detected entry directly so bulk-lock has something
    # to flip (manual adds are already locked).
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO glossary_entries "
        "(novel_id, term_zh, term_en, category, auto_detected, locked) "
        "VALUES (?, '剑气', 'Sword Qi', 'technique', 1, 0)",
        (novel_id,),
    )
    eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    resp = client.post(
        "/api/glossary/bulk-lock",
        json={"novel_id": novel_id, "ids": [eid], "locked": True},
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] == 1
    # Confirm the flag actually flipped in the listing.
    rows = client.get(f"/api/novels/{novel_id}/glossary").json()
    assert rows[0]["id"] == eid
    assert rows[0]["locked"] is True


# ---- retranslate-affected (queue boundary stubbed) ----------------------


def test_retranslate_affected_queues_chapters(client, monkeypatch):
    """The endpoint flags affected chapters translate_queued=1 and reports
    them. The worker-spawn is stubbed so no real translation runs, we still
    assert the durable queue flag landed in the DB."""
    spawned: list[tuple[int, int]] = []

    def _capture_spawn(novel_id, chapter_id):
        spawned.append((novel_id, chapter_id))

    monkeypatch.setattr(
        glossary_route.queue_svc, "spawn_translate_worker", _capture_spawn
    )

    novel_id = _seed_novel()
    _seed_chapter(novel_id, 1, "白小纯走进大殿。")
    _seed_chapter(novel_id, 2, "白小纯微微一笑。")
    _seed_chapter(novel_id, 3, "无关章节。")
    entry = client.post(
        f"/api/novels/{novel_id}/glossary",
        json={"term_zh": "白小纯", "term_en": "Bai Xiaochun", "category": "character"},
    ).json()

    resp = client.post(f"/api/glossary/{entry['id']}/retranslate-affected")
    assert resp.status_code == 200
    body = resp.json()
    assert body["queued_count"] == 2
    assert sorted(body["chapter_nums"]) == [1, 2]
    assert body["skipped_in_flight"] == []
    # Two workers were spawned through the stubbed boundary.
    assert len(spawned) == 2

    # The durable queue flag actually landed for the two affected chapters.
    conn = sqlite3.connect(DB_PATH)
    flagged = conn.execute(
        "SELECT chapter_num FROM chapters WHERE novel_id = ? AND translate_queued = 1 "
        "ORDER BY chapter_num",
        (novel_id,),
    ).fetchall()
    conn.close()
    assert [r[0] for r in flagged] == [1, 2]


def test_retranslate_affected_unknown_entry_404(client):
    resp = client.post("/api/glossary/888888/retranslate-affected")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "entry not found"
