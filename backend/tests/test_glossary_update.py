"""Tests for `PATCH /api/glossary/{entry_id}` term_en validation.

Regression: the route used to accept `{"term_en": ""}` (no Pydantic guard) and
`{"term_en": "   "}` (no service-layer strip-reject), leaving unusable
glossary rows. Creation already defended both. The fix mirrors that:
- Pydantic min_length=1 on GlossaryUpdate.term_en → 422 on outright empty
- update_entry post-strip raise ValueError → 400 on whitespace-only
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

    # No translator stubs needed — the glossary routes don't queue work.
    return TestClient(app)


def _seed_entry() -> tuple[int, int]:
    """Insert one novel + one glossary entry and return (novel_id, entry_id).
    The explicit commit is required — Python's sqlite3 module defaults to
    deferred transactions, so close() without commit() rolls back."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('T', 'paste')"
        )
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO glossary_entries "
            "(novel_id, term_zh, term_en, category, locked) "
            "VALUES (?, '天剑', 'Heaven Sword', 'item', 0)",
            (novel_id,),
        )
        entry_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return novel_id, entry_id
    finally:
        conn.close()


def test_patch_rejects_empty_string(client: TestClient) -> None:
    """Pydantic min_length=1 catches the outright-empty case at the API
    boundary — returns 422 (validation error), not 400 or 500."""
    _, entry_id = _seed_entry()
    resp = client.patch(
        f"/api/glossary/{entry_id}", json={"term_en": ""}
    )
    assert resp.status_code == 422, resp.text


def test_patch_rejects_whitespace_only(client: TestClient) -> None:
    """Pydantic accepts "   " (3 chars, min_length=1). The service layer
    catches it post-strip and raises ValueError → route maps to 400."""
    _, entry_id = _seed_entry()
    resp = client.patch(
        f"/api/glossary/{entry_id}", json={"term_en": "   "}
    )
    assert resp.status_code == 400, resp.text
    assert "term_en" in resp.json()["detail"]


def test_patch_accepts_valid_term(client: TestClient) -> None:
    """Sanity: a non-empty edit still works and persists."""
    _, entry_id = _seed_entry()
    resp = client.patch(
        f"/api/glossary/{entry_id}", json={"term_en": "Heaven Blade"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["term_en"] == "Heaven Blade"

    # And it's persisted to the row.
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT term_en, locked FROM glossary_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        assert row[0] == "Heaven Blade"
        # Implicit lock-on-edit — preserves the existing contract.
        assert row[1] == 1
    finally:
        conn.close()


def test_patch_omitting_term_en_does_not_validate_it(client: TestClient) -> None:
    """Other fields can still be updated without touching term_en. Pydantic's
    min_length only applies when the field is present."""
    _, entry_id = _seed_entry()
    resp = client.patch(
        f"/api/glossary/{entry_id}", json={"notes": "from chapter 3"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["term_en"] == "Heaven Sword"  # unchanged


def test_update_entry_stamps_updated_at(client: TestClient) -> None:
    """Design v2 Phase D invariant: every PATCH MUST advance
    glossary_entries.updated_at. The stale-glossary watermark in the
    reader / glossary UI depends on this column moving forward whenever
    the term's English text (or any other field) changes; a column that
    only carries the insert time would never make a term "stale" relative
    to chapters that translated against the previous rendering.

    The service writes `updated_at = datetime('now')` (SQLite second
    precision). Rather than sleep across the one-second boundary, we force
    the row's updated_at to an explicit past timestamp; the PATCH then
    writes a current `datetime('now')` that is strictly later, so the
    advance is observable without any wall-clock wait."""
    _, entry_id = _seed_entry()

    # Pin updated_at to a fixed, unambiguously-past value so the PATCH's
    # datetime('now') is guaranteed strictly later (no real-time sleep).
    PAST = "2000-01-01 00:00:00"
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE glossary_entries SET updated_at = ? WHERE id = ?",
            (PAST, entry_id),
        )
        conn.commit()
        before = conn.execute(
            "SELECT updated_at FROM glossary_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert before == PAST

    resp = client.patch(
        f"/api/glossary/{entry_id}", json={"term_en": "Heaven Blade"}
    )
    assert resp.status_code == 200, resp.text

    conn = sqlite3.connect(DB_PATH)
    try:
        after = conn.execute(
            "SELECT updated_at FROM glossary_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert after > before, (
        f"updated_at must advance on PATCH; was {before!r} → {after!r}. "
        "If this test fails, the stale-glossary watermark in the UI will "
        "never trigger — re-check services/glossary.py::update_entry."
    )
