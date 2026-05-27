"""Route-handler tests for /api/opus-mt/ using FastAPI's TestClient.

Mocks the download_pair generator so we can drive the SSE / state machine
without hitting the network or actually unpacking a tar bundle.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.services import opus_mt_models


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_opus_mt_state():
    """Make sure each test starts with empty per-pair queues and locks."""
    opus_mt_models._download_locks.clear()
    opus_mt_models._translator_cache.clear()
    from backend.routes import opus_mt as opus_mt_routes
    opus_mt_routes._progress_queues.clear()
    opus_mt_routes._last_event.clear()
    yield
    opus_mt_models._download_locks.clear()
    opus_mt_routes._progress_queues.clear()
    opus_mt_routes._last_event.clear()


def test_list_pairs_returns_three_entries(client: TestClient):
    r = client.get("/api/opus-mt/pairs")
    assert r.status_code == 200
    pairs = r.json()
    assert {p["pair"] for p in pairs} == {"zh-en", "ja-en", "ko-en"}
    for p in pairs:
        assert "installed" in p
        assert "size_mb_expected" in p
        assert "source_language" in p


def test_start_download_404s_for_unknown_pair(client: TestClient):
    r = client.post("/api/opus-mt/pairs/xx-en/download")
    assert r.status_code == 404


def test_delete_pair_404s_for_unknown_pair(client: TestClient):
    r = client.delete("/api/opus-mt/pairs/xx-en")
    assert r.status_code == 404


def test_status_404s_for_unknown_pair(client: TestClient):
    r = client.get("/api/opus-mt/pairs/xx-en/status")
    assert r.status_code == 404


def test_start_download_short_circuits_when_installed(
    client: TestClient, tmp_path, monkeypatch,
):
    monkeypatch.setattr(opus_mt_models, "USER_DATA_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(
        opus_mt_models, "model_dir",
        lambda pair: tmp_path / "opus_mt" / pair,
        raising=True,
    )
    pair_dir = tmp_path / "opus_mt" / "zh-en"
    pair_dir.mkdir(parents=True)
    for fname in ("model.bin", "source.spm", "target.spm"):
        (pair_dir / fname).write_bytes(b"stub")

    r = client.post("/api/opus-mt/pairs/zh-en/download")
    assert r.status_code == 200
    assert r.json() == {"status": "done", "pair": "zh-en"}


def test_start_download_spawns_task_and_status_streams_done(
    client: TestClient, monkeypatch,
):
    """Mock download_pair to emit a few events and confirm the SSE stream
    surfaces them all the way to a 'done'."""

    async def _fake_download(pair: str, *, http_client=None) -> AsyncIterator:
        for ph in ("downloading", "extracting", "done"):
            yield opus_mt_models.ProgressEvent(
                pair=pair, phase=ph, bytes_done=100, bytes_total=100,
            )
            await asyncio.sleep(0)

    monkeypatch.setattr(opus_mt_models, "download_pair", _fake_download)

    r = client.post("/api/opus-mt/pairs/zh-en/download")
    assert r.status_code == 200
    assert r.json()["status"] in {"started", "in_progress"}

    # Drain the SSE stream to completion. iter_lines yields one line per
    # SSE delimiter newline, including the empty line between events, so we
    # just collect everything until the server closes the connection (which
    # the route does on the terminal 'done' or 'error' event).
    with client.stream("GET", "/api/opus-mt/pairs/zh-en/status") as resp:
        assert resp.status_code == 200
        blob = "".join(resp.iter_text())
    assert "downloading" in blob
    assert "extracting" in blob
    assert "done" in blob


def test_delete_pair_returns_removed_state(client: TestClient, tmp_path, monkeypatch):
    monkeypatch.setattr(opus_mt_models, "USER_DATA_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(
        opus_mt_models, "model_dir",
        lambda pair: tmp_path / "opus_mt" / pair,
        raising=True,
    )
    # No model installed → removed=False.
    r = client.delete("/api/opus-mt/pairs/zh-en")
    assert r.status_code == 200
    assert r.json() == {"pair": "zh-en", "removed": False}

    # Install, then delete.
    pair_dir = tmp_path / "opus_mt" / "zh-en"
    pair_dir.mkdir(parents=True)
    for fname in ("model.bin", "source.spm", "target.spm"):
        (pair_dir / fname).write_bytes(b"stub")
    r2 = client.delete("/api/opus-mt/pairs/zh-en")
    assert r2.status_code == 200
    assert r2.json() == {"pair": "zh-en", "removed": True}
    assert not pair_dir.exists()
