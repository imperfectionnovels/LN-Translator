"""Tests for the simple key/value config endpoints (Design v2 Phase G).

The store is reserved for app-level state — first_run_complete is the
inaugural key — and exposes GET / PUT / DELETE. Invariants:
- GET on a missing key returns 404 (callers treat absence as "default").
- PUT is upsert; second write to the same key replaces.
- DELETE is idempotent; deleting a missing key is 204 not 404.
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
def client():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return TestClient(app)


def test_get_missing_key_returns_404(client: TestClient) -> None:
    resp = client.get("/api/config/first_run_complete")
    assert resp.status_code == 404, resp.text


def test_put_then_get_round_trip(client: TestClient) -> None:
    resp = client.put(
        "/api/config/first_run_complete",
        json={"value": "1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"key": "first_run_complete", "value": "1"}

    resp = client.get("/api/config/first_run_complete")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"key": "first_run_complete", "value": "1"}


def test_put_upserts_existing_key(client: TestClient) -> None:
    """Second PUT with a different value replaces, doesn't fail on
    uniqueness. The endpoint's ON CONFLICT DO UPDATE handles this."""
    client.put("/api/config/foo", json={"value": "bar"})
    client.put("/api/config/foo", json={"value": "baz"})
    resp = client.get("/api/config/foo")
    assert resp.json()["value"] == "baz"


def test_delete_is_idempotent(client: TestClient) -> None:
    """Deleting a key that doesn't exist returns 204, not 404. The
    settings UI's 'clear saved key' button needs this to never error."""
    resp = client.delete("/api/config/never_set_this_key")
    assert resp.status_code == 204, resp.text


def test_delete_removes_existing_key(client: TestClient) -> None:
    client.put("/api/config/foo", json={"value": "v"})
    resp = client.delete("/api/config/foo")
    assert resp.status_code == 204, resp.text

    resp = client.get("/api/config/foo")
    assert resp.status_code == 404
