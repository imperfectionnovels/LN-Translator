"""Section 6.7: cache stats counter + /api/cache/stats endpoint."""

from __future__ import annotations

import pytest

from backend.models import TranslationResult
from backend.services import llm_cache


@pytest.fixture(autouse=True)
def fresh_stats():
    llm_cache.reset_stats()
    yield
    llm_cache.reset_stats()


def test_translator_miss_then_hit_increments_correctly(tmp_path, monkeypatch):
    """One miss followed by a hit on the same key produces 1/1 counts."""
    monkeypatch.setenv("LLM_CACHE_ROOT", str(tmp_path))
    key = "abc" * 21
    # First read on an empty cache → miss.
    assert llm_cache.load_translation(key) is None
    s1 = llm_cache.get_stats()
    assert s1["translator"]["misses"] == 1
    assert s1["translator"]["hits"] == 0

    # Store, then load → hit.
    llm_cache.store_translation(
        key,
        TranslationResult(title_en="t", translated_text="body", new_terms=[]),
    )
    loaded = llm_cache.load_translation(key)
    assert loaded is not None
    s2 = llm_cache.get_stats()
    assert s2["translator"]["misses"] == 1
    assert s2["translator"]["hits"] == 1
    assert s2["translator"]["hit_rate"] == pytest.approx(0.5)


def test_corrupt_cache_file_counted_as_miss(tmp_path, monkeypatch):
    """A cache file that fails to parse must count as a miss, not silently
    inflate the hit counter — otherwise corrupt entries would look like
    perfect cache health to the operator."""
    monkeypatch.setenv("LLM_CACHE_ROOT", str(tmp_path))
    key = "corrupt-key-1234"
    bad_path = tmp_path / "translator"
    bad_path.mkdir(parents=True, exist_ok=True)
    (bad_path / f"{key}.json").write_text("{not valid json", encoding="utf-8")
    assert llm_cache.load_translation(key) is None
    s = llm_cache.get_stats()
    assert s["translator"]["misses"] == 1
    assert s["translator"]["hits"] == 0


def test_stats_returns_none_hit_rate_when_no_calls():
    """Zero calls → hit_rate is None, not 0.0 (which the UI would
    misinterpret as "0% cache hit rate, something's broken")."""
    s = llm_cache.get_stats()
    assert s["translator"]["hit_rate"] is None
    assert s["refiner"]["hit_rate"] is None


def test_reset_stats_clears_counters(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CACHE_ROOT", str(tmp_path))
    llm_cache.load_translation("does-not-exist")
    assert llm_cache.get_stats()["translator"]["misses"] == 1
    llm_cache.reset_stats()
    assert llm_cache.get_stats()["translator"]["misses"] == 0


@pytest.mark.asyncio
async def test_cache_stats_endpoint(monkeypatch):
    """GET /api/cache/stats returns the snapshot shape the settings JS expects."""
    from fastapi.testclient import TestClient
    async def _no_probe(_default):
        return None
    async def _no_drain():
        return None
    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)

    from backend.main import app
    with TestClient(app) as client:
        r = client.get("/api/cache/stats")
    assert r.status_code == 200
    body = r.json()
    for top_key in ("translator", "refiner", "on_disk_bytes", "on_disk_files"):
        assert top_key in body, f"missing top-level key {top_key!r}"
    for inner in ("hits", "misses", "hit_rate"):
        assert inner in body["translator"]
        assert inner in body["refiner"]
