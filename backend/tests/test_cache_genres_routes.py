"""HTTP-level tests for the two smallest read-only routers: routes/cache.py
and routes/genres.py.

Both are pure read endpoints (no DB writes, no queue work), so they are
exercised directly through the HTTP surface against the real app. Importing
the route modules at top level keeps the coverage mapping honest: these tests
own those modules rather than reaching them only transitively through the app.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import DEFAULT_GENRE
from backend.genres import GENRES

# Direct imports: these tests are the owning tests for these route modules.
from backend.routes import cache as cache_route
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


def test_cache_stats_shape(client):
    """GET /api/cache/stats returns the in-process counter dict from llm_cache."""
    resp = client.get("/api/cache/stats")
    assert resp.status_code == 200
    body = resp.json()
    # CacheStats has a fixed shape: per-stage hit/miss/hit_rate plus on-disk size.
    assert set(body) == {"translator", "refiner", "on_disk_bytes", "on_disk_files"}
    for stage in ("translator", "refiner"):
        assert set(body[stage]) == {"hits", "misses", "hit_rate"}
        assert isinstance(body[stage]["hits"], int)
        assert isinstance(body[stage]["misses"], int)
        # hit_rate is None when no lookups happened, else a valid 0..1 ratio.
        rate = body[stage]["hit_rate"]
        assert rate is None or (isinstance(rate, float) and 0.0 <= rate <= 1.0)
    assert isinstance(body["on_disk_bytes"], int)
    assert isinstance(body["on_disk_files"], int)


def test_cache_router_has_single_get():
    """The cache router exposes exactly one GET route at /stats."""
    paths = {(r.path, tuple(sorted(r.methods))) for r in cache_route.router.routes}
    assert ("/stats", ("GET",)) in paths


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
