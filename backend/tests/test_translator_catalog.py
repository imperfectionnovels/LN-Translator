"""Catalog ↔ factory parity invariants.

The translator catalog (services/translator_catalog.py) and the factory
dispatch table (services/translators/factory._DISPATCH) are two halves of
the same contract. Drift between them = silent breakage (a Provider row
the form happily creates but the queue worker can't instantiate). These
tests are the canary.
"""

from __future__ import annotations

import importlib

from backend.services.translator_catalog import (
    CUSTOM_MODEL_SENTINEL,
    all_type_keys,
    all_types,
    get_type,
    to_api_payload,
)
from backend.services.translators.factory import _DISPATCH


def test_catalog_and_factory_have_same_keys():
    """Every catalog type must have a factory dispatch entry, and vice
    versa. New type added in one place → must be added in the other."""
    catalog_keys = set(all_type_keys())
    dispatch_keys = set(_DISPATCH.keys())
    missing_in_dispatch = catalog_keys - dispatch_keys
    extra_in_dispatch = dispatch_keys - catalog_keys
    assert not missing_in_dispatch, (
        f"Catalog types missing from factory dispatch: {missing_in_dispatch}"
    )
    assert not extra_in_dispatch, (
        f"Factory dispatch types missing from catalog: {extra_in_dispatch}"
    )


def test_catalog_has_no_duplicate_type_keys():
    keys = [t.type for t in all_types()]
    assert len(keys) == len(set(keys)), (
        f"Duplicate type keys in catalog: {keys}"
    )


def test_every_factory_target_class_is_importable():
    """The dispatch table refers to classes by module + name. They must
    actually exist so an instantiation attempt doesn't blow up with
    AttributeError after the form has already saved the provider."""
    for type_key, (module_path, class_name) in _DISPATCH.items():
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name, None)
        assert cls is not None, (
            f"factory dispatch for {type_key!r} points at "
            f"{module_path}.{class_name} but it isn't defined"
        )


def test_subscription_and_local_types_ship_an_auth_command():
    """Subscription / local-auth types are exactly the ones whose Add
    Provider dialog needs to tell the user "run this command in your
    terminal" — they have no API key field. Most local/subscription types
    therefore ship a shell auth_command. A type that handles its install
    flow entirely in the UI (or needs no install at all, e.g.
    google_translate_free) is allowed to skip auth_command as long as it
    ships an install_hint so the dialog still renders a meaningful row
    instead of a blank command box."""
    for entry in all_types():
        if entry.auth in ("subscription", "none"):
            has_command = bool(entry.auth_command)
            has_hint = bool(entry.install_hint)
            assert has_command or has_hint, (
                f"{entry.type!r} has auth={entry.auth!r} but no auth_command "
                "AND no install_hint — the Add Provider dialog has nothing to "
                "show the user. Add an auth_command (shell flow) or an "
                "install_hint (UI flow) to the catalog entry."
            )


def test_every_catalog_entry_has_a_usable_model_path():
    """Either a non-empty curated list OR `supports_custom_model=True`.
    Otherwise the form would have no way to fill in `model_id`.

    Today every entry has `supports_custom_model=True` (defensive design)
    so this is a permissive check — it locks in the invariant against a
    future change that flips one to False."""
    for entry in all_types():
        has_curated = bool(entry.models)
        has_escape_hatch = entry.supports_custom_model
        assert has_curated or has_escape_hatch, (
            f"Catalog entry {entry.type!r} has no curated models AND no "
            "custom-model escape hatch — the user could never pick a model"
        )


def test_get_type_round_trips():
    for entry in all_types():
        assert get_type(entry.type) is entry
    assert get_type("definitely_not_a_real_type") is None


def test_api_payload_has_stable_shape():
    """The Settings dialog and onboarding both consume this. Lock in the
    field names so a renamed field doesn't silently break the frontend."""
    payload = to_api_payload()
    assert isinstance(payload, list)
    assert payload, "catalog should ship with at least one provider type"
    required_fields = {
        "type", "display", "group", "auth",
        "base_url_default", "secret_ref_hint",
        "supports_custom_model", "install_hint",
        "auth_command",
        "models", "custom_model_sentinel",
    }
    for entry in payload:
        missing = required_fields - set(entry.keys())
        assert not missing, (
            f"API payload entry {entry.get('type')!r} missing fields: {missing}"
        )
        assert entry["custom_model_sentinel"] == CUSTOM_MODEL_SENTINEL
        for m in entry["models"]:
            assert "id" in m and "display" in m


def test_catalog_endpoint_returns_payload():
    """Hits the route end-to-end so a future router-prefix change doesn't
    silently break the form's catalog fetch."""
    from fastapi.testclient import TestClient

    from backend.main import app

    async def _no_probe(_default):
        return None
    async def _no_drain():
        return None
    import backend.main as main_mod
    import backend.services.queue as queue_mod
    main_mod._probe_backends = _no_probe  # type: ignore[assignment]
    queue_mod.drain_on_startup = _no_drain  # type: ignore[assignment]

    with TestClient(app) as client:
        resp = client.get("/api/providers/catalog")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    type_keys = {e["type"] for e in body}
    # Sanity: every legacy type plus at least one new type is present.
    for legacy in ("claude_agent", "claude_cli", "gemini", "deepseek"):
        assert legacy in type_keys, f"legacy type {legacy!r} missing from catalog"
    for new_one in ("openai", "anthropic_api", "xai", "ollama", "codex_cli"):
        assert new_one in type_keys, f"new type {new_one!r} missing from catalog"
