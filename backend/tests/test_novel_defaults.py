"""Tests for novel-creation defaults from config_kv['novel_defaults'].

Precedence (per the redesign plan):
  request body field → config_kv default → NULL → runtime fallback

As of 2026-05-25 the whitelist only accepts `translator_provider_id` and
`refinement_provider_id`. `genre` is auto-set on scrape (or picked at
import); `source_language` is auto-detected from the chapter text. Both
are properties of the novel, not app-wide defaults. Stale blobs that
still carry those keys must be silently ignored (whitelist drops them).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.db import init_db, open_conn


@pytest.fixture
def app_with_stubs(monkeypatch):
    async def _no_probe(default_provider):
        return None

    async def _no_drain():
        return None

    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)

    from backend.main import app
    return app


async def _wipe():
    async with open_conn() as conn:
        for t in ("chapters", "novels", "providers"):
            try:
                await conn.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        await conn.execute("DELETE FROM config_kv WHERE key = ?", ("novel_defaults",))
        await conn.commit()


def _make_provider(client: TestClient, name: str) -> int:
    """Create a minimal provider; return its id."""
    resp = client.post(
        "/api/providers",
        json={
            "name": name,
            "provider_type": "claude_agent",
            "model_id": "claude-opus-4-7",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_no_config_means_null_columns(app_with_stubs):
    """When config_kv['novel_defaults'] is unset, a new novel inherits
    NULL on genre / translator / refinement. The runtime fallback then
    chooses the system defaults — no behavior change vs. pre-Phase-5."""
    await init_db()
    await _wipe()

    with TestClient(app_with_stubs) as client:
        resp = client.post(
            "/api/translate/paste",
            json={"title": "No Defaults", "text": "Chapter 1\n\nFoo."},
        )
        assert resp.status_code == 200, resp.text
        novel_id = resp.json()["novel_id"]

        novel = client.get(f"/api/novels/{novel_id}").json()

    assert novel["genre"] is None
    assert novel["translator_provider_id"] is None
    assert novel["refinement_provider_id"] is None
    # source_language defaults to 'zh' via the SCHEMA default — not NULL.
    assert novel["source_language"] == "zh"


@pytest.mark.asyncio
async def test_translator_default_lands_on_new_novel(app_with_stubs):
    """A defaults blob with translator_provider_id stamps the column on
    the next novel created. The remaining supported column carries
    through; non-supported keys are ignored."""
    await init_db()
    await _wipe()

    with TestClient(app_with_stubs) as client:
        provider_id = _make_provider(client, "Test Provider")

        client.put(
            "/api/config/novel_defaults",
            json={
                "value": json.dumps({
                    "translator_provider_id": provider_id,
                    # Extra unsupported keys must not crash and must not
                    # land on the row.
                    "bogus_field": "ignored",
                }),
            },
        )

        resp = client.post(
            "/api/translate/paste",
            json={"title": "With Default Translator", "text": "Chapter 1\n\nFoo."},
        )
        assert resp.status_code == 200, resp.text
        novel_id = resp.json()["novel_id"]

        novel = client.get(f"/api/novels/{novel_id}").json()

    assert novel["translator_provider_id"] == provider_id


@pytest.mark.asyncio
async def test_genre_and_source_language_in_blob_are_silently_dropped(
    app_with_stubs,
):
    """Stale blobs from before the 2026-05-25 split (when genre +
    source_language were defaultable) must NOT land those columns on new
    novels — they're per-novel properties now, not app-wide defaults."""
    await init_db()
    await _wipe()

    with TestClient(app_with_stubs) as client:
        client.put(
            "/api/config/novel_defaults",
            json={
                "value": json.dumps({
                    "genre": "wuxia",          # ignored — no longer accepted
                    "source_language": "ja",   # ignored — auto-detected
                }),
            },
        )

        resp = client.post(
            "/api/translate/paste",
            json={"title": "Stale Blob", "text": "Chapter 1\n\nFoo."},
        )
        assert resp.status_code == 200, resp.text
        novel = client.get(f"/api/novels/{resp.json()['novel_id']}").json()

    assert novel["genre"] is None
    # source_language ends up at the schema's NOT NULL DEFAULT 'zh' since
    # the auto-detect from commit 3 isn't wired yet at this commit's point.
    assert novel["source_language"] == "zh"


@pytest.mark.asyncio
async def test_existing_novels_not_backfilled(app_with_stubs):
    """Configuring defaults AFTER an existing novel exists must leave that
    novel's columns alone. Defaults govern future novels only."""
    await init_db()
    await _wipe()

    with TestClient(app_with_stubs) as client:
        provider_id = _make_provider(client, "Default Provider")

        first = client.post(
            "/api/translate/paste",
            json={"title": "Pre Defaults", "text": "Chapter 1\n\nFoo."},
        ).json()
        first_id = first["novel_id"]

        client.put(
            "/api/config/novel_defaults",
            json={"value": json.dumps({"translator_provider_id": provider_id})},
        )

        second = client.post(
            "/api/translate/paste",
            json={"title": "Post Defaults", "text": "Chapter 1\n\nFoo."},
        ).json()
        second_id = second["novel_id"]

        first_after = client.get(f"/api/novels/{first_id}").json()
        second_after = client.get(f"/api/novels/{second_id}").json()

    assert first_after["translator_provider_id"] is None  # NOT backfilled
    assert second_after["translator_provider_id"] == provider_id


@pytest.mark.asyncio
async def test_malformed_config_falls_back_to_null(app_with_stubs):
    """A defaults blob that isn't valid JSON (or isn't a dict) must be
    treated as 'no defaults' — no crash, no half-applied state."""
    await init_db()
    await _wipe()

    with TestClient(app_with_stubs) as client:
        client.put(
            "/api/config/novel_defaults",
            json={"value": "not valid json {[}"},
        )

        resp = client.post(
            "/api/translate/paste",
            json={"title": "Malformed Defaults", "text": "Chapter 1\n\nFoo."},
        )
        assert resp.status_code == 200, resp.text
        novel = client.get(f"/api/novels/{resp.json()['novel_id']}").json()

    assert novel["translator_provider_id"] is None


@pytest.mark.asyncio
async def test_empty_string_config_value_treated_as_null(app_with_stubs):
    """An empty-string value in the defaults blob must NOT land an empty
    string on the column. Empty values are skipped just like None."""
    await init_db()
    await _wipe()

    with TestClient(app_with_stubs) as client:
        client.put(
            "/api/config/novel_defaults",
            json={
                "value": json.dumps({
                    "translator_provider_id": "",   # empty
                    "refinement_provider_id": "",
                }),
            },
        )

        resp = client.post(
            "/api/translate/paste",
            json={"title": "Empty Defaults", "text": "Chapter 1\n\nFoo."},
        )
        novel = client.get(f"/api/novels/{resp.json()['novel_id']}").json()

    assert novel["translator_provider_id"] is None
    assert novel["refinement_provider_id"] is None
