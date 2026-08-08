"""In-process llm_cache hit/miss counters (get_stats / reset_stats).

The counters feed the /api/diagnostics payload (Settings About card). The
old standalone /api/cache/stats route was removed 2026-07-30 (no UI caller);
the service-level counter behavior pinned here is still live."""

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


def test_store_refinement_preserves_crlf(tmp_path, monkeypatch):
    """CRLF line endings in refinement text must round-trip identically.
    On Windows, text-mode I/O without newline='' translates \r\n to \r\r\n
    on write, then back to \n\n on read, corrupting the stored text. This
    test verifies that store_refinement and load_refinement preserve the
    exact bytes including \r\n and \r\n\r\n."""
    monkeypatch.setenv("LLM_CACHE_ROOT", str(tmp_path))
    key = "crlf-test-key"

    # Test content with CRLF within a paragraph and between paragraphs
    content = "First line\r\nstill first para.\r\n\r\nSecond paragraph here."

    # Store and load
    llm_cache.store_refinement(key, content)
    loaded = llm_cache.load_refinement(key)

    # Must be byte-identical
    assert loaded == content
    assert loaded is not None
    assert "\r\r\n" not in loaded  # No CRLF doubling
    assert "\n\n" not in loaded  # No doubled newlines
    s = llm_cache.get_stats()
    assert s["refiner"]["hits"] == 1
    assert s["refiner"]["misses"] == 0
