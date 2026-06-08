"""HTTP-level tests for routes/find_replace.py (project-wide find/replace).

This is the owning test for `backend.routes.find_replace`: the module is
imported at top level and referenced directly (route table) so the coverage
map credits these tests to it rather than reaching it only transitively
through the app.

The preview/commit token contract is the load-bearing behavior: /find returns
a frozen-snapshot token + hit counts, /replace commits by token (and refuses
with 409/410 on drift/expiry). Everything runs against a real temp DB seeded
via plain `sqlite3`; no translation/queue work is involved (find/replace is
pure text substitution over existing chapter columns). The token store is
reset per test so the process-local dict doesn't leak across cases.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Direct import: these tests own backend.routes.find_replace.
from backend.routes import find_replace as fr_route
from backend.services import find_replace as fr

DB_PATH = Path(os.environ["DB_PATH"])


@pytest.fixture
def client(monkeypatch):
    """TestClient with startup probe + queue drain stubbed. Also resets the
    find/replace in-process token store so a token minted in one test can't be
    committed in another."""
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
        fr._reset_token_store_for_tests()
        yield c
        fr._reset_token_store_for_tests()


def _seed_novel_with_chapters(payload, title="FR Novel") -> int:
    """Insert one novel + N done chapters. `payload` is a list of
    (chapter_num, translated_text, refined_text). Returns the novel id."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, 'paste')",
            (title,),
        )
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for chapter_num, translated, refined in payload:
            conn.execute(
                "INSERT INTO chapters "
                "(novel_id, chapter_num, original_text, translated_text, "
                "refined_text, status) VALUES (?, ?, '原文', ?, ?, 'done')",
                (novel_id, chapter_num, translated, refined),
            )
        conn.commit()
        return novel_id
    finally:
        conn.close()


# ---- Route table sanity --------------------------------------------------


def test_find_replace_router_exposes_expected_routes():
    paths = {(r.path, tuple(sorted(r.methods))) for r in fr_route.router.routes}
    assert ("/find", ("POST",)) in paths
    assert ("/replace", ("POST",)) in paths
    assert ("/novels/{novel_id}/fr-snapshots", ("GET",)) in paths
    assert ("/fr-snapshots/{snapshot_id}/restore", ("POST",)) in paths


# ---- /find preview shape -------------------------------------------------


def test_find_preview_counts_hits_and_issues_token(client):
    novel_id = _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked. Bai Xiaochun smiled.", None),
        (2, "The sect.", None),
        (3, "Bai Xiaochun spoke.", "Bai Xiaochun spoke (refined)."),
    ])
    resp = client.post(
        "/api/find",
        json={
            "find": "Bai Xiaochun",
            "replacement": "Bai Xiao Chun",
            "scope_kind": "novel",
            "scope_ids": [novel_id],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]  # non-empty frozen-snapshot token
    assert body["total_chapters"] == 2          # ch1 + ch3 match
    assert body["total_hits_translated"] == 3   # 2 in ch1 + 1 in ch3
    assert body["total_hits_refined"] == 1      # 1 in ch3 refined
    # Rows carry the per-chapter breakdown + a sample preview.
    nums = sorted(r["chapter_num"] for r in body["rows"])
    assert nums == [1, 3]
    assert all("novel_title" in r for r in body["rows"])


def test_find_empty_target_cols_rejected_400(client):
    """An explicitly empty target_cols list is a 400 from the handler guard."""
    novel_id = _seed_novel_with_chapters([(1, "Bai Xiaochun walked.", None)])
    resp = client.post(
        "/api/find",
        json={
            "find": "Bai Xiaochun",
            "replacement": "X",
            "scope_kind": "novel",
            "scope_ids": [novel_id],
            "target_cols": [],
        },
    )
    assert resp.status_code == 400
    assert "target_cols" in resp.json()["detail"]


def test_find_empty_find_string_rejected_422(client):
    """find has min_length=1, so an empty string fails Pydantic validation."""
    resp = client.post(
        "/api/find",
        json={"find": "", "replacement": "x", "scope_kind": "all"},
    )
    assert resp.status_code == 422
    assert any(err["loc"][-1] == "find" for err in resp.json()["detail"])


def test_find_invalid_regex_rejected_400(client):
    """An unclosed group surfaces from the engine as a 400, not a 500."""
    _seed_novel_with_chapters([(1, "Bai Xiaochun walked.", None)])
    resp = client.post(
        "/api/find",
        json={
            "find": "(unclosed",
            "replacement": "x",
            "scope_kind": "all",
            "use_regex": True,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]  # a human-readable reason


# ---- /find -> /replace commit happy path ---------------------------------


def test_preview_then_commit_applies_substitution(client):
    novel_id = _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
        (2, "Bai Xiaochun spoke.", "Bai Xiaochun spoke (refined)."),
    ])
    preview = client.post(
        "/api/find",
        json={
            "find": "Bai Xiaochun",
            "replacement": "Bai Xiao Chun",
            "scope_kind": "novel",
            "scope_ids": [novel_id],
        },
    ).json()
    token = preview["token"]

    commit = client.post("/api/replace", json={"token": token})
    assert commit.status_code == 200
    body = commit.json()
    assert body["chapters_updated"] == 2
    assert body["rows_updated_translated"] == 2
    assert body["rows_updated_refined"] == 1

    # The substitution actually landed in the DB.
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT translated_text FROM chapters WHERE novel_id = ? ORDER BY chapter_num",
        (novel_id,),
    ).fetchall()
    conn.close()
    assert rows[0][0] == "Bai Xiao Chun walked."
    assert rows[1][0] == "Bai Xiao Chun spoke."


# ---- /replace failure paths ----------------------------------------------


def test_commit_unknown_token_returns_410(client):
    resp = client.post("/api/replace", json={"token": "no-such-token"})
    assert resp.status_code == 410
    assert "expired" in resp.json()["detail"]


def test_commit_is_single_use_replay_410(client):
    novel_id = _seed_novel_with_chapters([(1, "Bai Xiaochun walked.", None)])
    token = client.post(
        "/api/find",
        json={
            "find": "Bai Xiaochun",
            "replacement": "Bai Xiao Chun",
            "scope_kind": "novel",
            "scope_ids": [novel_id],
        },
    ).json()["token"]

    first = client.post("/api/replace", json={"token": token})
    assert first.status_code == 200
    assert first.json()["chapters_updated"] == 1
    # Token is consumed; a replay is a clean 410.
    second = client.post("/api/replace", json={"token": token})
    assert second.status_code == 410


def test_commit_refuses_when_chapter_drifts_409(client):
    """The viability invariant: any chapter content change between preview and
    commit refuses the commit with a 409 carrying the drifted ids."""
    novel_id = _seed_novel_with_chapters([
        (1, "Bai Xiaochun walked.", None),
        (2, "Bai Xiaochun spoke.", None),
    ])
    token = client.post(
        "/api/find",
        json={
            "find": "Bai Xiaochun",
            "replacement": "Bai Xiao Chun",
            "scope_kind": "novel",
            "scope_ids": [novel_id],
        },
    ).json()["token"]

    # Simulate a concurrent edit to chapter 2 between preview and commit.
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapters SET translated_text = 'Bai Xiaochun spoke politely.' "
        "WHERE novel_id = ? AND chapter_num = 2",
        (novel_id,),
    )
    conn.commit()
    conn.close()

    resp = client.post("/api/replace", json={"token": token})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "drifted_chapter_ids" in detail
    assert len(detail["drifted_chapter_ids"]) == 1
    # Drift aborts the whole commit: chapter 1 stays untouched.
    conn = sqlite3.connect(DB_PATH)
    ch1 = conn.execute(
        "SELECT translated_text FROM chapters WHERE novel_id = ? AND chapter_num = 1",
        (novel_id,),
    ).fetchone()[0]
    conn.close()
    assert ch1 == "Bai Xiaochun walked."


# ---- fr-snapshots history -----------------------------------------------


def test_fr_snapshots_empty_for_fresh_novel(client):
    novel_id = _seed_novel_with_chapters([(1, "Bai Xiaochun walked.", None)])
    resp = client.get(f"/api/novels/{novel_id}/fr-snapshots")
    assert resp.status_code == 200
    assert resp.json() == []


def test_commit_records_a_restorable_snapshot(client):
    """A successful novel-scoped commit logs a snapshot the History tab can
    list and (once) restore."""
    novel_id = _seed_novel_with_chapters([(1, "Bai Xiaochun walked.", None)])
    token = client.post(
        "/api/find",
        json={
            "find": "Bai Xiaochun",
            "replacement": "Bai Xiao Chun",
            "scope_kind": "novel",
            "scope_ids": [novel_id],
        },
    ).json()["token"]
    assert client.post("/api/replace", json={"token": token}).status_code == 200

    snaps = client.get(f"/api/novels/{novel_id}/fr-snapshots").json()
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap["find_pattern"] == "Bai Xiaochun"
    assert snap["replace_pattern"] == "Bai Xiao Chun"
    assert snap["restored_at"] is None
    assert snap["chapters_changed"] == 1

    # Restoring replays the original text back onto the chapter.
    restore = client.post(f"/api/fr-snapshots/{snap['id']}/restore")
    assert restore.status_code == 200
    rbody = restore.json()
    assert rbody["snapshot_id"] == snap["id"]
    assert rbody["chapters_restored"] == 1

    conn = sqlite3.connect(DB_PATH)
    restored = conn.execute(
        "SELECT translated_text FROM chapters WHERE novel_id = ? AND chapter_num = 1",
        (novel_id,),
    ).fetchone()[0]
    conn.close()
    assert restored == "Bai Xiaochun walked."
