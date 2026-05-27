"""Provider service.

A `Provider` row in the database describes one user-configured AI backend the
translator can route to. Per-novel selection (in `novels.translator_provider_id`
and `novels.refinement_provider_id`) picks which provider runs a given chapter;
NULL on the translator field falls back to the row flagged `is_default=1`,
NULL on the refinement field means refinement is OFF.

Secret handling: provider rows store only `secret_ref` (a name).
`resolve_secret` looks the name up in the OS keychain first (Credential Manager
/ Keychain / Secret Service), then falls back to `os.environ[secret_ref]` for
dev / headless contexts where keyring is unavailable.

The full set of supported provider types lives in
`backend/services/translator_catalog.py` — that file is the single source of
truth. `KNOWN_PROVIDER_TYPES` below is derived from it so the validation set,
the factory dispatch, and the UI dropdown can never drift out of sync.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from backend.config import (
    CLAUDE_AGENT_TRANSLATOR_MODEL,
    CLAUDE_CLI_TRANSLATOR_MODEL,
    DEEPSEEK_TRANSLATOR_MODEL,
    GEMINI_TRANSLATOR_MODEL,
    IS_FROZEN,
    TRANSLATOR_BACKEND,
)
from backend.db import open_conn
from backend.services.translator_catalog import all_type_keys

logger = logging.getLogger(__name__)

# Known provider_type values the factory can dispatch on. Derived from the
# catalog so adding a new type is one entry in translator_catalog.py + one
# translator class in services/translators/.
KNOWN_PROVIDER_TYPES = all_type_keys()


@dataclass(frozen=True)
class Provider:
    id: int
    name: str
    provider_type: str
    base_url: str | None
    model_id: str
    params: dict[str, Any] = field(default_factory=dict)
    secret_ref: str | None = None
    is_default: bool = False
    # Stamped by routes/providers.py /test on success. NULL until tested.
    # Drives the settings card's "tested 2m ago" tag.
    last_tested_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Provider":
        raw_params = row["params_json"] or "{}"
        try:
            params = json.loads(raw_params)
            if not isinstance(params, dict):
                params = {}
        except (TypeError, ValueError):
            logger.warning(
                "provider %s has malformed params_json — using empty dict",
                row["name"],
            )
            params = {}
        # last_tested_at was added late via _ADDITIVE_MIGRATIONS, so older
        # snapshots produced by tests / migrations may not have the column.
        # row.keys() is the portable way to test; falling back to None keeps
        # the dataclass shape valid.
        keys = row.keys()
        return cls(
            id=row["id"],
            name=row["name"],
            provider_type=row["provider_type"],
            base_url=row["base_url"],
            model_id=row["model_id"],
            params=params,
            secret_ref=row["secret_ref"],
            is_default=bool(row["is_default"]),
            last_tested_at=(
                row["last_tested_at"]
                if "last_tested_at" in keys else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# Shared SELECT column list — keeps the four loaders in sync. last_tested_at
# was added late so it lives in _ADDITIVE_MIGRATIONS; from_row() tolerates its
# absence on older snapshots.
_PROVIDER_COLS = (
    "id, name, provider_type, base_url, model_id, params_json, "
    "secret_ref, is_default, last_tested_at, created_at, updated_at"
)


async def list_providers() -> list[Provider]:
    async with open_conn() as conn:
        cur = await conn.execute(
            f"SELECT {_PROVIDER_COLS} FROM providers "
            "ORDER BY is_default DESC, id ASC"
        )
        rows = await cur.fetchall()
        return [Provider.from_row(r) for r in rows]


async def load_provider(provider_id: int) -> Provider | None:
    async with open_conn() as conn:
        cur = await conn.execute(
            f"SELECT {_PROVIDER_COLS} FROM providers WHERE id = ?",
            (provider_id,),
        )
        row = await cur.fetchone()
        return Provider.from_row(row) if row else None


async def get_default_provider() -> Provider | None:
    async with open_conn() as conn:
        cur = await conn.execute(
            f"SELECT {_PROVIDER_COLS} FROM providers "
            "WHERE is_default = 1 LIMIT 1"
        )
        row = await cur.fetchone()
        return Provider.from_row(row) if row else None


async def create_provider(
    *,
    name: str,
    provider_type: str,
    model_id: str,
    base_url: str | None = None,
    params: dict[str, Any] | None = None,
    secret_ref: str | None = None,
    is_default: bool = False,
) -> Provider:
    """Create a provider. If this is the very first row in the table, force
    is_default=1 regardless of the caller's flag — the queue worker falls
    through to legacy env routing when no default exists, which is almost
    never what a first-time user wants. The empty-state UI copy
    ("the first provider you add becomes the default") relies on this
    auto-promotion at the backend layer so it's correct even when the
    frontend forgets to check the box.
    """
    params_json = json.dumps(params or {}, ensure_ascii=False)
    async with open_conn() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM providers")
        existing_count = (await cur.fetchone())["n"]
        force_default = existing_count == 0
        effective_default = is_default or force_default
        if effective_default:
            await conn.execute("UPDATE providers SET is_default = 0")
        cur = await conn.execute(
            "INSERT INTO providers (name, provider_type, base_url, model_id, "
            "params_json, secret_ref, is_default) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, provider_type, base_url, model_id, params_json, secret_ref,
             1 if effective_default else 0),
        )
        await conn.commit()
        new_id = cur.lastrowid
    provider = await load_provider(new_id)
    assert provider is not None
    return provider


async def update_provider(
    provider_id: int,
    updates: dict[str, Any],
) -> Provider | None:
    """Partial update. `updates` keys are column names; presence means
    "write this value", absence means "leave unchanged". Crucially this lets
    callers clear nullable columns by passing `{"base_url": None}` — the
    earlier `kwarg is not None` shape conflated "omitted" with "explicit
    null" and silently ignored clear requests.

    Use `set_default` for the `is_default` flag — it has cross-row semantics
    so it lives separately.
    """
    if not updates:
        return await load_provider(provider_id)

    # Whitelist of mutable columns. Anything else in `updates` is dropped.
    _MUTABLE = {
        "name", "provider_type", "model_id", "base_url", "secret_ref",
    }
    sets: list[str] = []
    args: list[Any] = []
    for col in _MUTABLE:
        if col in updates:
            sets.append(f"{col} = ?")
            args.append(updates[col])
    if "params" in updates:
        sets.append("params_json = ?")
        value = updates["params"]
        if value is None:
            args.append("{}")
        else:
            args.append(json.dumps(value, ensure_ascii=False))
    if not sets:
        return await load_provider(provider_id)
    sets.append("updated_at = datetime('now')")
    args.append(provider_id)
    async with open_conn() as conn:
        await conn.execute(
            f"UPDATE providers SET {', '.join(sets)} WHERE id = ?", args,
        )
        await conn.commit()
    return await load_provider(provider_id)


async def delete_provider(provider_id: int) -> bool:
    """Delete a provider. If the deleted row was the default and other
    providers exist, promote the next-oldest one in the same transaction
    so the system is never left without a default during a live session
    (the queue worker would otherwise fall through to legacy env routing
    until the next server boot).
    """
    async with open_conn() as conn:
        await conn.execute("BEGIN")
        try:
            cur = await conn.execute(
                "SELECT is_default FROM providers WHERE id = ?",
                (provider_id,),
            )
            row = await cur.fetchone()
            if row is None:
                await conn.execute("ROLLBACK")
                return False
            was_default = bool(row["is_default"])
            await conn.execute(
                "DELETE FROM providers WHERE id = ?", (provider_id,),
            )
            if was_default:
                # Promote the oldest surviving row (lowest id) so the
                # system always has a default after this commit. If no
                # other rows exist, the system has no default — the queue
                # worker falls through to legacy env routing and the user
                # is expected to configure a new provider via the settings
                # UI. ensure_default_provider() will NOT seed a replacement
                # because we're not in the empty-on-boot path.
                cur = await conn.execute(
                    "SELECT id FROM providers ORDER BY id ASC LIMIT 1"
                )
                successor = await cur.fetchone()
                if successor is not None:
                    await conn.execute(
                        "UPDATE providers SET is_default = 1, "
                        "updated_at = datetime('now') WHERE id = ?",
                        (successor["id"],),
                    )
            await conn.commit()
            return True
        except Exception:
            await conn.execute("ROLLBACK")
            raise


async def set_default(provider_id: int) -> Provider | None:
    """Atomically mark `provider_id` as the default, clearing any other
    `is_default=1` row. The schema's unique index on `WHERE is_default = 1`
    would otherwise reject two-row updates in any order — wrap in a single
    transaction so the clear and the set are atomic."""
    async with open_conn() as conn:
        await conn.execute("BEGIN")
        try:
            await conn.execute("UPDATE providers SET is_default = 0")
            cur = await conn.execute(
                "UPDATE providers SET is_default = 1, updated_at = datetime('now') "
                "WHERE id = ?",
                (provider_id,),
            )
            if (cur.rowcount or 0) == 0:
                await conn.execute("ROLLBACK")
                return None
            await conn.commit()
        except Exception:
            await conn.execute("ROLLBACK")
            raise
    return await load_provider(provider_id)


_KEYRING_SERVICE = "LN-Translator"


def resolve_secret(provider: Provider) -> str | None:
    """Return the API key for `provider`, or None if no secret is configured.

    Lookup order:
      1. OS keychain via `keyring.get_password('LN-Translator', secret_ref)`.
         This is the canonical store for the frozen EXE — Windows
         Credential Manager / macOS Keychain / Linux Secret Service.
      2. Environment variable named `secret_ref`. Preserves the dev-mode
         `.env` workflow so existing users don't have to migrate.

    Keyring failures (no backend available, lock contention, missing
    library) drop through to the env-var path silently — keyring is a
    bonus, never a hard requirement. SDK-based providers (claude_agent,
    claude_cli) typically have no secret_ref — they authenticate via the
    user's local Claude install.
    """
    if not provider.secret_ref:
        return None
    try:
        import keyring
        stored = keyring.get_password(_KEYRING_SERVICE, provider.secret_ref)
        if stored:
            return stored
    except Exception as e:
        # No keyring backend, locked, missing library — log once at debug
        # and fall through to env-var lookup. Common on headless Linux
        # without dbus.
        logger.debug(
            "keyring lookup failed for %r: %s; falling back to env",
            provider.secret_ref, e,
        )
    return os.environ.get(provider.secret_ref) or None


def store_secret(secret_ref: str, value: str) -> bool:
    """Write a secret to the OS keychain under `_KEYRING_SERVICE`.
    Returns True on success, False if keyring isn't available. The route
    layer surfaces False as a 503 with a hint to set the env var instead.
    """
    if not secret_ref or not value:
        return False
    try:
        import keyring
        keyring.set_password(_KEYRING_SERVICE, secret_ref, value)
        return True
    except Exception as e:
        logger.warning(
            "keyring write failed for %r: %s — secret not persisted",
            secret_ref, e,
        )
        return False


def delete_secret(secret_ref: str) -> bool:
    """Remove a secret from the OS keychain. Returns True on success,
    False if keyring isn't available or the entry didn't exist."""
    if not secret_ref:
        return False
    try:
        import keyring
        keyring.delete_password(_KEYRING_SERVICE, secret_ref)
        return True
    except Exception as e:
        logger.debug(
            "keyring delete failed for %r: %s", secret_ref, e,
        )
        return False


# ----- bootstrap seed -----

# Map legacy backend name → seed defaults used when no providers exist yet.
# Each entry describes how to spin up a Provider row that mirrors the existing
# env-var-driven behavior so an upgraded user doesn't lose their config.
_SEED_DEFAULTS: dict[str, dict[str, Any]] = {
    "claude_agent": {
        "name": "Claude Agent (local subscription)",
        "model_id_factory": lambda: CLAUDE_AGENT_TRANSLATOR_MODEL,
        "base_url": None,
        "secret_ref": None,
    },
    "claude_cli": {
        "name": "Claude CLI",
        "model_id_factory": lambda: CLAUDE_CLI_TRANSLATOR_MODEL,
        "base_url": None,
        "secret_ref": None,
    },
    "gemini": {
        "name": "Google Gemini",
        "model_id_factory": lambda: GEMINI_TRANSLATOR_MODEL,
        "base_url": None,
        "secret_ref": "GEMINI_API_KEY",
    },
    "deepseek": {
        "name": "DeepSeek",
        "model_id_factory": lambda: DEEPSEEK_TRANSLATOR_MODEL,
        "base_url": "https://api.deepseek.com",
        "secret_ref": "DEEPSEEK_API_KEY",
    },
}


async def ensure_default_provider() -> Provider | None:
    """First-startup seed: if the providers table is empty, create one row
    from the legacy `TRANSLATOR_BACKEND` env var and mark it default. After
    the seed, the providers table is the source of truth and this is a
    no-op on every subsequent boot.

    Returns the default provider (newly seeded or already present), or None
    if the legacy env var doesn't match a known backend hint.

    Frozen-EXE carve-out: we DO NOT auto-seed from the legacy env var.
    The default TRANSLATOR_BACKEND value is `claude_agent`, which depends
    on a separate Claude Code install — seeding it on a fresh EXE produces
    a provider that almost certainly can't authenticate, `_has_any_provider`
    returns True, and the first-run window lands on `/` instead of
    `/onboarding`. The user is then stuck at "looks like there's a
    provider but nothing works." Skipping the seed in frozen mode keeps
    the table empty so app_entry routes to the welcome wizard. Users
    running from source keep the .env-driven seeding behavior.
    """
    existing = await get_default_provider()
    if existing is not None:
        return existing
    any_provider = await list_providers()
    if any_provider:
        # Providers exist but none is default — promote the first one.
        return await set_default(any_provider[0].id)
    if IS_FROZEN:
        # Fresh-EXE first run. Stay empty so app_entry's
        # _has_any_provider() check routes the browser to /onboarding.
        logger.info(
            "frozen mode: providers table empty, skipping legacy env seed "
            "so first-run UI lands on /onboarding"
        )
        return None
    backend = TRANSLATOR_BACKEND
    if backend not in _SEED_DEFAULTS:
        logger.warning(
            "providers table empty and TRANSLATOR_BACKEND=%r is unknown — "
            "skipping seed. Configure providers via the settings UI.",
            backend,
        )
        return None
    spec = _SEED_DEFAULTS[backend]
    logger.info(
        "seeding default provider from legacy TRANSLATOR_BACKEND=%s", backend,
    )
    return await create_provider(
        name=spec["name"],
        provider_type=backend,
        model_id=spec["model_id_factory"](),
        base_url=spec["base_url"],
        secret_ref=spec["secret_ref"],
        is_default=True,
    )


async def stamp_last_tested(provider_id: int) -> None:
    """Write `datetime('now')` into providers.last_tested_at. Called from the
    /test route after the config check passes so the settings card can show
    a fresh "tested Xm ago" tag without re-probing on every page load."""
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE providers SET last_tested_at = datetime('now') "
            "WHERE id = ?",
            (provider_id,),
        )
        await conn.commit()


async def test_provider(provider: Provider) -> tuple[bool, str]:
    """Smoke-test a provider's CONFIG (no network call). Returns (ok, message).

    Deliberately config-only — the settings UI fires this on every "Test"
    button click, and a real round-trip to Gemini / DeepSeek would burn a
    paid token quota each time. Validates: provider_type is known, model_id
    is non-empty, secret (if required) resolves. Anything wrong with the
    network or model name surfaces on the first real translation instead.

    If you want a real round-trip in the future, ADD a separate code path
    (e.g. gated behind a `?deep=true` query param on the /test route) — do
    NOT promote this function. The cheap-by-default invariant is asserted by
    `test_test_provider_makes_no_network_call`.
    """
    from backend.services.translator_catalog import get_type
    entry = get_type(provider.provider_type)
    if entry is None:
        return False, f"Unknown provider_type {provider.provider_type!r}"
    if not provider.model_id:
        return False, "model_id is empty"
    # Subscription/local types authenticate out-of-band (Claude CLI login,
    # ChatGPT login via codex, local Ollama). Only api_key types need a
    # resolvable secret_ref. The catalog declares this per-type so a new
    # type with its own auth shape can plug in without editing this function.
    needs_secret = entry.auth == "api_key"
    if needs_secret:
        if not provider.secret_ref:
            return False, f"{provider.provider_type} requires secret_ref"
        if not resolve_secret(provider):
            return False, (
                f"secret_ref {provider.secret_ref!r} resolves to empty — "
                f"set the env var or keyring entry."
            )
    return True, "Configuration looks valid (config check only; no round-trip)."
