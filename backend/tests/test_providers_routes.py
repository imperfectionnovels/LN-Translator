"""HTTP-level tests for routes/providers.py (provider CRUD + /test + secrets).

This file is the *owning* test for `backend/routes/providers.py`: it imports the
route module at top level and asserts on its router shape + pure helpers, so the
coverage tool reads the module as directly tested. The existing
`test_providers.py` exercises the `services/providers.py` layer (the dataclass,
CRUD service, set_default atomicity, config-only test_provider). These tests are
complementary, they drive the *route* layer end-to-end through the HTTP surface:

  * GET    /api/providers, list
  * GET    /api/providers/{id}, get (+ 404)
  * POST   /api/providers, create (201) + duplicate-name 400
                                                 + unknown-type 400
  * PATCH  /api/providers/{id}, update (+ 404)
  * DELETE /api/providers/{id}, delete (+ 404)
  * POST   /api/providers/{id}/set-default, flips the default flag
  * POST   /api/providers/{id}/test, config check (stubbed, no network)
  * POST   /api/providers/{id}/set-secret, happy / no-secret_ref 400 / 404 / 503
  * DELETE /api/providers/{id}/secret, happy / 404

Provider CRUD runs against the real temp DB. The /test endpoint and the
keyring-backed secret storage are stubbed at the service boundary so no real
API call or OS-credential-store access ever happens.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Direct import, this is the owning test for the route module. Referenced below
# in test_router_exposes_expected_routes and the pure-helper tests.
from backend.routes import providers as providers_route

DB_PATH = Path(os.environ["DB_PATH"])


@pytest.fixture
def client(monkeypatch):
    """TestClient with the startup probe + queue drain stubbed so the lifespan
    never reaches for a real translator. Entering the context manager runs
    init_db() against the fresh temp DB. The providers table is then truncated
    so each test starts from a known-empty provider set (startup seeding from
    TRANSLATOR_BACKEND can otherwise leave a row behind)."""
    if DB_PATH.exists():
        DB_PATH.unlink()

    async def _no_probe(default_provider):
        return None

    async def _no_drain():
        return None

    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)

    from backend.main import app

    with TestClient(app) as c:
        # Clear any provider seeded by ensure_default_provider on startup.
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("DELETE FROM providers")
            conn.commit()
        finally:
            conn.close()
        yield c


def _create(client, **overrides) -> dict:
    """POST a provider and return the created JSON body. Defaults to a valid
    gemini config; pass overrides to vary a field."""
    body = {
        "name": "p1",
        "provider_type": "gemini",
        "model_id": "gemini-3-pro-preview",
        "secret_ref": "SOME_KEY",
    }
    body.update(overrides)
    resp = client.post("/api/providers", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ----- route-module ownership: router shape + pure helpers -----

def test_router_exposes_expected_routes():
    """Assert the route module wires every documented endpoint. References
    `providers_route` so coverage maps this file onto the module."""
    routes = providers_route.router.routes
    pairs = {(r.path, tuple(sorted(r.methods))) for r in routes}
    assert ("", ("GET",)) in pairs            # list
    assert ("", ("POST",)) in pairs           # create
    assert ("/{provider_id}", ("GET",)) in pairs
    assert ("/{provider_id}", ("PATCH",)) in pairs
    assert ("/{provider_id}", ("DELETE",)) in pairs
    assert ("/{provider_id}/set-default", ("POST",)) in pairs
    assert ("/{provider_id}/test", ("POST",)) in pairs
    assert ("/{provider_id}/set-secret", ("POST",)) in pairs
    assert ("/{provider_id}/secret", ("DELETE",)) in pairs


def test_to_model_maps_service_dataclass_fields():
    """The route's `_to_model` adapter must copy every field 1:1 from the
    service dataclass into the API model. A drift here silently drops a field
    from every provider response."""
    svc_provider = providers_route.providers_svc.Provider(
        id=7,
        name="adapter-check",
        provider_type="deepseek",
        base_url="https://api.deepseek.com",
        model_id="deepseek-v4-pro",
        params={"temperature": 0.3},
        secret_ref="DEEPSEEK_API_KEY",
        is_default=True,
        last_tested_at="2026-06-08T00:00:00",
        created_at="2026-06-01T00:00:00",
        updated_at="2026-06-02T00:00:00",
    )
    model = providers_route._to_model(svc_provider)
    assert model.id == 7
    assert model.name == "adapter-check"
    assert model.provider_type == "deepseek"
    assert model.base_url == "https://api.deepseek.com"
    assert model.model_id == "deepseek-v4-pro"
    assert model.params == {"temperature": 0.3}
    assert model.secret_ref == "DEEPSEEK_API_KEY"
    assert model.is_default is True
    assert model.last_tested_at == "2026-06-08T00:00:00"


def test_bucket_iso_dates_length_and_ordering():
    """`_bucket_iso_dates` aligns sparse SQL aggregates to a fixed-length,
    oldest-first sparkline array. Pin the contract the route's stats handler
    depends on: exact length, ascending order, distinct days."""
    days = providers_route._bucket_iso_dates(5)
    assert len(days) == 5
    assert days == sorted(days)           # oldest first / ascending
    assert len(set(days)) == 5            # no duplicate buckets
    assert days[-1] == providers_route._bucket_iso_dates(1)[0]  # last == today


# ----- CRUD over the HTTP surface -----

def test_create_provider_returns_201_and_auto_default(client):
    body = _create(client, name="first")
    assert body["name"] == "first"
    assert body["provider_type"] == "gemini"
    assert body["model_id"] == "gemini-3-pro-preview"
    # First provider in an empty table is force-promoted to default.
    assert body["is_default"] is True
    assert body["id"] > 0
    # The secret value is never echoed back, only the ref name.
    assert body["secret_ref"] == "SOME_KEY"


def test_list_and_get_provider(client):
    a = _create(client, name="alpha")
    b = _create(client, name="beta")

    listed = client.get("/api/providers")
    assert listed.status_code == 200
    rows = listed.json()
    names = {r["name"] for r in rows}
    assert names == {"alpha", "beta"}
    # The default (alpha, the first created) sorts ahead of beta.
    assert rows[0]["id"] == a["id"]

    got = client.get(f"/api/providers/{b['id']}")
    assert got.status_code == 200
    assert got.json()["name"] == "beta"
    assert got.json()["id"] == b["id"]


def test_get_provider_404(client):
    resp = client.get("/api/providers/99999")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "provider not found"


def test_patch_provider_updates_fields(client):
    p = _create(client, name="orig", model_id="m-old")
    resp = client.patch(
        f"/api/providers/{p['id']}",
        json={"name": "renamed", "model_id": "m-new"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "renamed"
    assert body["model_id"] == "m-new"
    # Confirm it actually persisted (not just echoed).
    refetched = client.get(f"/api/providers/{p['id']}").json()
    assert refetched["name"] == "renamed"


def test_patch_provider_can_clear_nullable_field(client):
    """PATCH with an explicit null clears the column (key-presence semantics)."""
    p = _create(client, name="clearable", secret_ref="SOME_KEY")
    resp = client.patch(
        f"/api/providers/{p['id']}",
        json={"secret_ref": None},
    )
    assert resp.status_code == 200
    assert resp.json()["secret_ref"] is None
    assert client.get(f"/api/providers/{p['id']}").json()["secret_ref"] is None


def test_patch_provider_404(client):
    resp = client.patch("/api/providers/99999", json={"name": "ghost"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "provider not found"


def test_delete_provider_then_404_and_gone(client):
    _create(client, name="keep")
    b = _create(client, name="remove")

    first = client.delete(f"/api/providers/{b['id']}")
    assert first.status_code == 200
    assert first.json() == {"ok": True}

    # Gone from the listing; the survivor remains.
    remaining = {r["name"] for r in client.get("/api/providers").json()}
    assert remaining == {"keep"}

    # A second delete of the same id is a clean 404, not a silent success.
    second = client.delete(f"/api/providers/{b['id']}")
    assert second.status_code == 404
    assert second.json()["detail"] == "provider not found"


# ----- error paths in create -----

def test_create_duplicate_name_returns_400(client):
    """The service raises on the UNIQUE(name) constraint; the route wraps it
    into a 400 rather than a 500."""
    _create(client, name="dupe")
    resp = client.post(
        "/api/providers",
        json={"name": "dupe", "provider_type": "gemini", "model_id": "m"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert isinstance(detail, str) and detail  # non-empty error string
    # Only the original row exists; the duplicate was rejected.
    assert len(client.get("/api/providers").json()) == 1


def test_create_unknown_provider_type_returns_400(client):
    resp = client.post(
        "/api/providers",
        json={
            "name": "bad-type",
            "provider_type": "totally_made_up_backend",
            "model_id": "m",
        },
    )
    assert resp.status_code == 400
    assert "totally_made_up_backend" in resp.json()["detail"]
    # Nothing was inserted.
    assert client.get("/api/providers").json() == []


# ----- set-default flips the default flag -----

def test_set_default_flips_the_default(client):
    a = _create(client, name="a")  # auto-default
    b = _create(client, name="b")
    assert a["is_default"] is True
    assert b["is_default"] is False

    resp = client.post(f"/api/providers/{b['id']}/set-default")
    assert resp.status_code == 200
    assert resp.json()["is_default"] is True

    # The old default lost the flag; the new one holds it.
    assert client.get(f"/api/providers/{a['id']}").json()["is_default"] is False
    assert client.get(f"/api/providers/{b['id']}").json()["is_default"] is True


def test_set_default_404(client):
    resp = client.post("/api/providers/99999/set-default")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "provider not found"


# ----- /test endpoint: stubbed at the service boundary, no network -----

def test_test_provider_ok_stamps_last_tested(client, monkeypatch):
    """A passing config check returns ok=True and stamps last_tested_at.
    `test_provider` is stubbed so no client is constructed and no network
    call is made."""
    p = _create(client, name="testable")
    assert p["last_tested_at"] is None

    calls: list[int] = []

    async def _fake_test(provider):
        calls.append(provider.id)
        return True, "Configuration looks valid (stubbed)."

    monkeypatch.setattr(providers_route.providers_svc, "test_provider", _fake_test)

    resp = client.post(f"/api/providers/{p['id']}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "stubbed" in body["message"]
    assert calls == [p["id"]]
    # ok=True path stamps the column; refetch confirms it's now non-null.
    assert client.get(f"/api/providers/{p['id']}").json()["last_tested_at"] is not None


def test_test_provider_failure_does_not_stamp(client, monkeypatch):
    """A failing config check returns ok=False and must NOT stamp
    last_tested_at (the settings card should not show a stale 'tested' tag)."""
    p = _create(client, name="bad-config")

    async def _fake_test(provider):
        return False, "secret_ref resolves to empty"

    monkeypatch.setattr(providers_route.providers_svc, "test_provider", _fake_test)

    resp = client.post(f"/api/providers/{p['id']}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "empty" in body["message"]
    # Failure leaves last_tested_at untouched (still null).
    assert client.get(f"/api/providers/{p['id']}").json()["last_tested_at"] is None


def test_test_provider_404(client):
    resp = client.post("/api/providers/99999/test")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "provider not found"


# ----- set-secret / delete-secret: stub keyring, never touch the OS store -----

def test_set_secret_happy_path(client, monkeypatch):
    """A provider with a secret_ref stores the value via the (stubbed) keyring
    helper and returns the ref it was stored under. The raw value never leaves
    the request body."""
    p = _create(client, name="has-ref", secret_ref="MY_KEY")

    stored: dict[str, str] = {}

    def _fake_store(secret_ref, value):
        stored[secret_ref] = value
        return True

    monkeypatch.setattr(providers_route.providers_svc, "store_secret", _fake_store)

    resp = client.post(
        f"/api/providers/{p['id']}/set-secret",
        json={"value": "sk-super-secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["stored_under"] == "MY_KEY"
    # The stub captured the value under the provider's ref name.
    assert stored == {"MY_KEY": "sk-super-secret"}


def test_set_secret_no_secret_ref_returns_400(client, monkeypatch):
    """A provider without a secret_ref can't store a secret, 400, and the
    keyring helper is never invoked."""
    p = _create(client, name="no-ref", secret_ref=None)

    called = {"n": 0}

    def _fake_store(secret_ref, value):
        called["n"] += 1
        return True

    monkeypatch.setattr(providers_route.providers_svc, "store_secret", _fake_store)

    resp = client.post(
        f"/api/providers/{p['id']}/set-secret",
        json={"value": "sk-x"},
    )
    assert resp.status_code == 400
    assert "secret_ref" in resp.json()["detail"]
    assert called["n"] == 0  # short-circuited before touching the store


def test_set_secret_keyring_unavailable_returns_503(client, monkeypatch):
    """When store_secret returns False (no keyring backend), the route surfaces
    a 503 with the env-var fallback hint."""
    p = _create(client, name="no-keyring", secret_ref="MY_KEY")

    monkeypatch.setattr(
        providers_route.providers_svc, "store_secret", lambda ref, value: False
    )

    resp = client.post(
        f"/api/providers/{p['id']}/set-secret",
        json={"value": "sk-x"},
    )
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "MY_KEY" in detail
    assert "env var" in detail.lower()


def test_set_secret_404(client, monkeypatch):
    monkeypatch.setattr(
        providers_route.providers_svc, "store_secret", lambda ref, value: True
    )
    resp = client.post(
        "/api/providers/99999/set-secret",
        json={"value": "sk-x"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "provider not found"


def test_delete_secret_happy_path(client, monkeypatch):
    """Deleting a stored secret calls the (stubbed) keyring delete with the
    provider's ref and returns ok. Idempotent by contract."""
    p = _create(client, name="del-ref", secret_ref="MY_KEY")

    deleted: list[str] = []

    def _fake_delete(secret_ref):
        deleted.append(secret_ref)
        return True

    monkeypatch.setattr(providers_route.providers_svc, "delete_secret", _fake_delete)

    resp = client.delete(f"/api/providers/{p['id']}/secret")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert deleted == ["MY_KEY"]


def test_delete_secret_no_ref_is_noop_ok(client, monkeypatch):
    """A provider without a secret_ref still returns ok (idempotent), and the
    keyring delete is never invoked."""
    p = _create(client, name="del-no-ref", secret_ref=None)

    called = {"n": 0}

    def _fake_delete(secret_ref):
        called["n"] += 1
        return True

    monkeypatch.setattr(providers_route.providers_svc, "delete_secret", _fake_delete)

    resp = client.delete(f"/api/providers/{p['id']}/secret")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert called["n"] == 0


def test_delete_secret_404(client):
    resp = client.delete("/api/providers/99999/secret")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "provider not found"
