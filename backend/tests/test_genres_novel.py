"""Tests for the per-novel genre tags surface (novels.genre = primary,
novel_genres = secondary tags).

Invariants exercised:
- list returns (primary, secondary[]) in insertion order
- add_secondary is idempotent on duplicate; rejects unknown keys (400);
  rejects when the key already matches the primary (409)
- remove_secondary rejects attempts to remove the primary (409)
- set_primary swaps transactionally: old primary becomes secondary, new
  primary leaves the novel_genres table
- unknown genre_key in any mutating endpoint → 400
- FK cascade: deleting the novel removes its novel_genres rows
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import _ADDITIVE_MIGRATIONS, SCHEMA
from backend.main import app

DB_PATH = Path(os.environ["DB_PATH"])


def _insert_novel(conn: sqlite3.Connection, title: str, genre: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO novels (title, source_type, genre) VALUES (?, ?, ?)",
        (title, "paste", genre),
    )
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def client():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    # The novel_genres table lives in _ADDITIVE_MIGRATIONS, not SCHEMA, so
    # apply migrations too — matches what init_db does on a real boot.
    for stmt in _ADDITIVE_MIGRATIONS:
        try:
            conn.executescript(stmt)
        except sqlite3.OperationalError:
            # ALTER ADD COLUMN on a column that already exists raises;
            # ignored at runtime by init_db's per-statement error swallow.
            pass
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    conn.close()
    return TestClient(app)


def test_list_empty_secondary_returns_primary_only(client: TestClient) -> None:
    conn = sqlite3.connect(DB_PATH)
    novel_id = _insert_novel(conn, "Novel A", "xianxia")
    conn.close()

    resp = client.get(f"/api/novels/{novel_id}/genres")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "primary": "xianxia",
        "secondary": [],
        "all_keys": ["xianxia"],
    }


def test_list_missing_novel_returns_404(client: TestClient) -> None:
    resp = client.get("/api/novels/9999/genres")
    assert resp.status_code == 404


def test_add_secondary_appends_and_returns_full_list(client: TestClient) -> None:
    conn = sqlite3.connect(DB_PATH)
    novel_id = _insert_novel(conn, "Novel A", "xianxia")
    conn.close()

    resp = client.post(
        f"/api/novels/{novel_id}/genres",
        json={"genre_key": "wuxia"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["secondary"] == ["wuxia"]
    assert resp.json()["all_keys"] == ["xianxia", "wuxia"]


def test_add_secondary_is_idempotent_on_duplicate(client: TestClient) -> None:
    conn = sqlite3.connect(DB_PATH)
    novel_id = _insert_novel(conn, "Novel A", "xianxia")
    conn.close()

    client.post(f"/api/novels/{novel_id}/genres", json={"genre_key": "wuxia"})
    resp = client.post(
        f"/api/novels/{novel_id}/genres",
        json={"genre_key": "wuxia"},
    )
    assert resp.status_code == 200
    assert resp.json()["secondary"] == ["wuxia"]


def test_add_secondary_unknown_key_returns_400(client: TestClient) -> None:
    conn = sqlite3.connect(DB_PATH)
    novel_id = _insert_novel(conn, "Novel A", "xianxia")
    conn.close()

    resp = client.post(
        f"/api/novels/{novel_id}/genres",
        json={"genre_key": "not-a-real-genre"},
    )
    assert resp.status_code == 400
    assert "unknown genre" in resp.json()["detail"]


def test_add_secondary_rejects_when_matches_primary(client: TestClient) -> None:
    conn = sqlite3.connect(DB_PATH)
    novel_id = _insert_novel(conn, "Novel A", "xianxia")
    conn.close()

    resp = client.post(
        f"/api/novels/{novel_id}/genres",
        json={"genre_key": "xianxia"},
    )
    assert resp.status_code == 409
    assert "primary genre" in resp.json()["detail"]


def test_remove_secondary_drops_the_row(client: TestClient) -> None:
    conn = sqlite3.connect(DB_PATH)
    novel_id = _insert_novel(conn, "Novel A", "xianxia")
    conn.close()

    client.post(f"/api/novels/{novel_id}/genres", json={"genre_key": "wuxia"})
    client.post(f"/api/novels/{novel_id}/genres", json={"genre_key": "isekai"})

    resp = client.delete(f"/api/novels/{novel_id}/genres/wuxia")
    assert resp.status_code == 200
    assert resp.json()["secondary"] == ["isekai"]


def test_remove_primary_rejects_with_409(client: TestClient) -> None:
    conn = sqlite3.connect(DB_PATH)
    novel_id = _insert_novel(conn, "Novel A", "xianxia")
    conn.close()

    resp = client.delete(f"/api/novels/{novel_id}/genres/xianxia")
    assert resp.status_code == 409
    assert "primary" in resp.json()["detail"]


def test_set_primary_swaps_with_old_primary(client: TestClient) -> None:
    """The transactional swap: old primary moves into novel_genres as a
    secondary, the new primary leaves novel_genres, novels.genre updates."""
    conn = sqlite3.connect(DB_PATH)
    novel_id = _insert_novel(conn, "Novel A", "xianxia")
    conn.close()
    client.post(f"/api/novels/{novel_id}/genres", json={"genre_key": "wuxia"})

    resp = client.put(f"/api/novels/{novel_id}/genres/wuxia/primary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["primary"] == "wuxia"
    assert body["secondary"] == ["xianxia"]
    assert body["all_keys"] == ["wuxia", "xianxia"]


def test_set_primary_to_brand_new_key_pushes_old_primary_to_secondary(
    client: TestClient,
) -> None:
    """Promoting a key that wasn't already a secondary still preserves the
    old primary by adding it to novel_genres."""
    conn = sqlite3.connect(DB_PATH)
    novel_id = _insert_novel(conn, "Novel A", "xianxia")
    conn.close()

    resp = client.put(f"/api/novels/{novel_id}/genres/wuxia/primary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["primary"] == "wuxia"
    assert body["secondary"] == ["xianxia"]


def test_set_primary_to_current_primary_is_noop(client: TestClient) -> None:
    conn = sqlite3.connect(DB_PATH)
    novel_id = _insert_novel(conn, "Novel A", "xianxia")
    conn.close()

    resp = client.put(f"/api/novels/{novel_id}/genres/xianxia/primary")
    assert resp.status_code == 200
    assert resp.json() == {
        "primary": "xianxia",
        "secondary": [],
        "all_keys": ["xianxia"],
    }


def test_set_primary_unknown_key_returns_400(client: TestClient) -> None:
    conn = sqlite3.connect(DB_PATH)
    novel_id = _insert_novel(conn, "Novel A", "xianxia")
    conn.close()

    resp = client.put(f"/api/novels/{novel_id}/genres/not-real/primary")
    assert resp.status_code == 400


def test_post_with_is_primary_true_delegates_to_set_primary(
    client: TestClient,
) -> None:
    """The POST endpoint's is_primary=True shortcut should be equivalent to
    calling PUT /{key}/primary directly."""
    conn = sqlite3.connect(DB_PATH)
    novel_id = _insert_novel(conn, "Novel A", "xianxia")
    conn.close()

    resp = client.post(
        f"/api/novels/{novel_id}/genres",
        json={"genre_key": "modern-romance", "is_primary": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["primary"] == "modern-romance"
    assert body["secondary"] == ["xianxia"]


def test_novel_purge_cascades_to_genre_rows(client: TestClient) -> None:
    """2026-05-25: DELETE soft-archives (cascades nothing); purge is the
    hard-delete path that fires CASCADE. Verify both halves."""
    conn = sqlite3.connect(DB_PATH)
    novel_id = _insert_novel(conn, "Novel A", "xianxia")
    conn.close()
    client.post(f"/api/novels/{novel_id}/genres", json={"genre_key": "wuxia"})

    # Soft-archive — novel_genres MUST persist.
    resp = client.delete(f"/api/novels/{novel_id}")
    assert resp.status_code == 200, resp.text
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.execute(
        "SELECT COUNT(*) FROM novel_genres WHERE novel_id = ?", (novel_id,),
    )
    assert cur.fetchone()[0] == 1, "soft-archive must not cascade"
    conn.close()

    # Hard purge — CASCADE fires.
    resp = client.delete(f"/api/novels/{novel_id}/purge")
    assert resp.status_code == 200, resp.text
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.execute(
        "SELECT COUNT(*) FROM novel_genres WHERE novel_id = ?", (novel_id,),
    )
    assert cur.fetchone()[0] == 0
    conn.close()


def test_primary_starts_null_when_novel_has_no_genre(client: TestClient) -> None:
    """Older novels without genre set surface primary=None; the UI uses
    this to show 'No genre yet' and offers the Add chip as a promote-to-
    primary affordance."""
    conn = sqlite3.connect(DB_PATH)
    novel_id = _insert_novel(conn, "Novel A", None)
    conn.close()

    resp = client.get(f"/api/novels/{novel_id}/genres")
    assert resp.status_code == 200
    assert resp.json() == {"primary": None, "secondary": [], "all_keys": []}
