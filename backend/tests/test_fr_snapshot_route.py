"""End-to-end test: commit_preview writes a snapshot, route lists it,
restore endpoint replays the pre-substitution body back onto the chapter.
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


@pytest.fixture
def client():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    for stmt in _ADDITIVE_MIGRATIONS:
        try:
            conn.executescript(stmt)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    return TestClient(app)


def _seed_novel_with_chapter(translated: str) -> tuple[int, int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
    )
    novel_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO chapters (novel_id, chapter_num, original_text, "
        "translated_text, status) VALUES (?, 1, '...', ?, 'done')",
        (novel_id, translated),
    )
    chapter_id = cur.lastrowid
    conn.commit()
    conn.close()
    return novel_id, chapter_id


def test_commit_records_snapshot_listed_by_route(client: TestClient) -> None:
    novel_id, chapter_id = _seed_novel_with_chapter("Sword sword sword.")
    pr = client.post(
        "/api/find",
        json={
            "find": "Sword",
            "replacement": "Blade",
            "scope_kind": "novel",
            "scope_ids": [novel_id],
            "target_cols": ["translated_text"],
            "use_regex": False,
            "case_sensitive": True,
            "word_boundary": False,
        },
    )
    assert pr.status_code == 200, pr.text
    token = pr.json()["token"]
    cr = client.post("/api/replace", json={"token": token})
    assert cr.status_code == 200, cr.text
    assert cr.json()["chapters_updated"] == 1

    sr = client.get(f"/api/novels/{novel_id}/fr-snapshots")
    assert sr.status_code == 200, sr.text
    snapshots = sr.json()
    assert len(snapshots) == 1
    assert snapshots[0]["find_pattern"] == "Sword"
    assert snapshots[0]["chapters_changed"] == 1
    assert snapshots[0]["restored_at"] is None


def test_restore_replays_body_and_marks_snapshot(client: TestClient) -> None:
    novel_id, chapter_id = _seed_novel_with_chapter("Sword arrived.")
    pr = client.post(
        "/api/find",
        json={
            "find": "Sword",
            "replacement": "Blade",
            "scope_kind": "novel",
            "scope_ids": [novel_id],
            "target_cols": ["translated_text"],
            "use_regex": False,
            "case_sensitive": True,
            "word_boundary": False,
        },
    )
    token = pr.json()["token"]
    client.post("/api/replace", json={"token": token})

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT translated_text FROM chapters WHERE id = ?", (chapter_id,),
    ).fetchone()
    conn.close()
    assert row[0] == "Blade arrived."

    snapshots = client.get(f"/api/novels/{novel_id}/fr-snapshots").json()
    snap_id = snapshots[0]["id"]
    rr = client.post(f"/api/fr-snapshots/{snap_id}/restore")
    assert rr.status_code == 200, rr.text
    assert rr.json()["chapters_restored"] == 1

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT translated_text FROM chapters WHERE id = ?", (chapter_id,),
    ).fetchone()
    conn.close()
    assert row[0] == "Sword arrived."

    snapshots = client.get(f"/api/novels/{novel_id}/fr-snapshots").json()
    assert snapshots[0]["restored_at"] is not None

    rr = client.post(f"/api/fr-snapshots/{snap_id}/restore")
    assert rr.status_code == 409
