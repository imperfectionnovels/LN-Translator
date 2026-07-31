"""HTTP-level tests for the read-only genres router (routes/genres.py).

Pure read endpoint (no DB writes, no queue work), so it is exercised
directly through the HTTP surface against the real app. Importing the route
module at top level keeps the coverage mapping honest: these tests own the
module rather than reaching it only transitively through the app.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import DEFAULT_GENRE
from backend.genres import GENRES

# Direct import: these tests are the owning tests for this route module.
from backend.routes import genres as genres_route


@pytest.fixture
def client(monkeypatch):
    async def _no_probe(default_provider):
        return None

    async def _no_drain():
        return None

    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)

    from backend.main import app

    with TestClient(app) as c:
        yield c


def test_list_genres_matches_registry(client):
    """GET /api/genres returns the default genre and every registered genre."""
    resp = client.get("/api/genres")
    assert resp.status_code == 200
    body = resp.json()
    assert body["default"] == DEFAULT_GENRE
    returned_keys = {g["key"] for g in body["genres"]}
    assert returned_keys == set(GENRES.keys())
    # Each genre entry carries the three display fields the dropdown needs.
    for g in body["genres"]:
        assert set(g) == {"key", "name", "description"}
        assert g["name"]


def test_default_genre_is_registered():
    """The configured DEFAULT_GENRE must exist in the registry the route serves."""
    assert DEFAULT_GENRE in GENRES


def test_genres_router_exposes_single_get():
    """The genres router registers exactly one GET endpoint at the mount root."""
    routes = [(r.path, tuple(sorted(r.methods))) for r in genres_route.router.routes]
    assert ("", ("GET",)) in routes
    assert len(routes) == 1
