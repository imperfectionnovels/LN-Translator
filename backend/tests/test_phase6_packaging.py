"""Phase 6 tests: EXE-mode wiring.

Covers:
- _find_free_port skips a held port.
- USER_DATA_ROOT resolution: env override beats default; frozen-mode
  branch points at appdata.
- resolve_secret keyring/env fallback chain: keyring hit, env hit,
  both miss.
- store_secret / delete_secret pass through to keyring.
- /api/providers/{id}/set-secret writes via keyring (mocked).
- End-to-end wiring: app_entry._run_uvicorn in a background thread
  reaches /api/health; _signal_shutdown stops it cleanly. Catches
  breakage in the layer between main, _run_uvicorn, _signal_shutdown
  that unit tests of individual helpers can't see. (The actual EXE
  launch path is covered by scripts/smoke-exe.ps1.)
"""

from __future__ import annotations

import socket
import sys
from unittest.mock import MagicMock

import pytest

from backend.app_entry import (
    _CONSOLE_CLOSE_EVENTS,
    _CTRL_CLOSE_EVENT,
    _find_free_port,
    _install_windows_console_handler,
    _make_console_handler,
)
from backend.services import providers as providers_svc

# ---- port detection --------------------------------------------------------

def test_find_free_port_returns_something_bindable():
    port = _find_free_port()
    # The returned port must actually bind successfully right now.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))
        s.close()


def test_find_free_port_skips_busy_port():
    """Hold a socket on a specific port and verify the probe walks past it."""
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))  # OS picks
    held_port = held.getsockname()[1]
    try:
        # Start probing at held_port — should skip it and return a higher port.
        result = _find_free_port(start=held_port)
        assert result != held_port, (
            f"port detector returned the busy port {held_port} — should have walked past it"
        )
        assert result >= held_port + 1
    finally:
        held.close()


# ---- USER_DATA_ROOT resolution ---------------------------------------------

def test_user_data_root_env_override(monkeypatch, tmp_path):
    """LN_TRANSLATOR_DATA env var trumps the platform default."""
    custom = tmp_path / "my-overridden-data"
    monkeypatch.setenv("LN_TRANSLATOR_DATA", str(custom))
    # Re-import the helper to pick up the env var.
    from backend.config import _user_data_root
    assert _user_data_root() == custom


def test_user_data_root_default_windows(monkeypatch):
    """Windows path defaults to %APPDATA%/LN-Translator."""
    monkeypatch.delenv("LN_TRANSLATOR_DATA", raising=False)
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
    from backend.config import _user_data_root
    result = _user_data_root()
    assert "LN-Translator" in str(result)
    assert "AppData" in str(result) or "Roaming" in str(result)


# ---- resolve_secret fallback chain -----------------------------------------

def _provider_with_secret(ref: str) -> providers_svc.Provider:
    return providers_svc.Provider(
        id=1, name="t", provider_type="gemini",
        base_url=None, model_id="m", params={},
        secret_ref=ref, is_default=False,
        created_at="", updated_at="",
    )


def test_resolve_secret_keyring_hit_wins(monkeypatch):
    """When keyring has a value, env var is ignored."""
    fake_keyring = MagicMock()
    fake_keyring.get_password.return_value = "from-keyring"
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)
    monkeypatch.setenv("MY_TEST_KEY", "from-env")

    p = _provider_with_secret("MY_TEST_KEY")
    assert providers_svc.resolve_secret(p) == "from-keyring"
    fake_keyring.get_password.assert_called_once_with("LN-Translator", "MY_TEST_KEY")


def test_resolve_secret_falls_back_to_env_when_keyring_misses(monkeypatch):
    """Keyring returns None → resolve_secret tries env."""
    fake_keyring = MagicMock()
    fake_keyring.get_password.return_value = None
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)
    monkeypatch.setenv("MY_TEST_KEY", "from-env")

    p = _provider_with_secret("MY_TEST_KEY")
    assert providers_svc.resolve_secret(p) == "from-env"


def test_resolve_secret_returns_none_when_both_miss(monkeypatch):
    fake_keyring = MagicMock()
    fake_keyring.get_password.return_value = None
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)
    monkeypatch.delenv("MY_TEST_KEY", raising=False)

    p = _provider_with_secret("MY_TEST_KEY")
    assert providers_svc.resolve_secret(p) is None


def test_resolve_secret_keyring_failure_falls_to_env(monkeypatch):
    """keyring backend exceptions (no dbus on Linux, lock contention)
    must fall through silently — keyring is bonus, never required."""
    fake_keyring = MagicMock()
    fake_keyring.get_password.side_effect = RuntimeError("no backend available")
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)
    monkeypatch.setenv("MY_TEST_KEY", "fallback")

    p = _provider_with_secret("MY_TEST_KEY")
    assert providers_svc.resolve_secret(p) == "fallback"


def test_resolve_secret_no_ref_returns_none(monkeypatch):
    """Provider with secret_ref=None means no auth needed (SDK-based);
    resolve_secret must short-circuit to None without touching keyring."""
    fake_keyring = MagicMock()
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)

    p = providers_svc.Provider(
        id=1, name="claude", provider_type="claude_agent",
        base_url=None, model_id="m", params={},
        secret_ref=None, is_default=True,
        created_at="", updated_at="",
    )
    assert providers_svc.resolve_secret(p) is None
    fake_keyring.get_password.assert_not_called()


# ---- store_secret / delete_secret ------------------------------------------

def test_store_secret_passes_through_to_keyring(monkeypatch):
    fake_keyring = MagicMock()
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)
    assert providers_svc.store_secret("GEMINI_API_KEY", "sk-xyz") is True
    fake_keyring.set_password.assert_called_once_with(
        "LN-Translator", "GEMINI_API_KEY", "sk-xyz",
    )


def test_store_secret_returns_false_on_keyring_failure(monkeypatch):
    fake_keyring = MagicMock()
    fake_keyring.set_password.side_effect = RuntimeError("no backend")
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)
    assert providers_svc.store_secret("X", "v") is False


def test_store_secret_rejects_empty_inputs(monkeypatch):
    fake_keyring = MagicMock()
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)
    assert providers_svc.store_secret("", "v") is False
    assert providers_svc.store_secret("X", "") is False
    fake_keyring.set_password.assert_not_called()


def test_delete_secret_passes_through(monkeypatch):
    fake_keyring = MagicMock()
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)
    assert providers_svc.delete_secret("X") is True
    fake_keyring.delete_password.assert_called_once_with("LN-Translator", "X")


# ---- frozen-mode first-run flow --------------------------------------------

@pytest.mark.asyncio
async def test_ensure_default_provider_skips_seed_when_frozen(monkeypatch):
    """Regression: in frozen EXE mode, a fresh DB with no providers must
    stay empty so app_entry routes the first-run browser to /onboarding.
    Pre-fix, the seed produced a claude_agent provider from the default
    env var, leaving the UX dead-ended at the import page with a provider
    that couldn't auth.
    """
    from backend.db import init_db, open_conn
    # Wipe state.
    await init_db()
    async with open_conn() as conn:
        await conn.execute("DELETE FROM providers")
        await conn.commit()
    # Pretend we're frozen.
    monkeypatch.setattr("backend.services.providers.IS_FROZEN", True)

    result = await providers_svc.ensure_default_provider()
    assert result is None, "frozen mode must not seed a default provider"

    # Verify the table is still empty so _has_any_provider returns False.
    async with open_conn() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM providers")
        count = (await cur.fetchone())[0]
    assert count == 0


@pytest.mark.asyncio
async def test_ensure_default_provider_still_seeds_in_dev_mode(monkeypatch):
    """Dev mode (IS_FROZEN=False) still seeds from TRANSLATOR_BACKEND
    so users running from source don't lose the .env workflow."""
    from backend.db import init_db, open_conn
    await init_db()
    async with open_conn() as conn:
        await conn.execute("DELETE FROM providers")
        await conn.commit()
    monkeypatch.setattr("backend.services.providers.IS_FROZEN", False)
    monkeypatch.setattr("backend.services.providers.TRANSLATOR_BACKEND", "gemini")

    result = await providers_svc.ensure_default_provider()
    assert result is not None
    assert result.provider_type == "gemini"
    assert result.is_default is True


# ---- /api/providers/{id}/set-secret route ----------------------------------

@pytest.fixture
def quiet_app_p6(monkeypatch):
    """Same shape as quiet_app in test_refinement.py — stub the lifespan
    so TestClient doesn't probe a real provider or drain the queue."""
    async def _no_probe(default_provider):
        return None
    async def _no_drain():
        return None
    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)
    from backend.main import app
    return app


@pytest.mark.asyncio
async def test_set_secret_route_200_on_success(monkeypatch, quiet_app_p6):
    """POST /api/providers/{id}/set-secret with keyring available stores
    the value and returns 200."""
    from backend.db import init_db, open_conn

    await init_db()
    async with open_conn() as conn:
        await conn.execute("DELETE FROM providers")
        await conn.commit()
    p = await providers_svc.create_provider(
        name="gem", provider_type="gemini", model_id="m",
        secret_ref="MY_KEY",
    )

    fake_keyring = MagicMock()
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)

    from fastapi.testclient import TestClient
    with TestClient(quiet_app_p6) as client:
        resp = client.post(
            f"/api/providers/{p.id}/set-secret",
            json={"value": "sk-test-abc"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"ok": True, "stored_under": "MY_KEY"}
    fake_keyring.set_password.assert_called_once_with(
        "LN-Translator", "MY_KEY", "sk-test-abc",
    )


@pytest.mark.asyncio
async def test_set_secret_route_400_when_no_secret_ref(monkeypatch, quiet_app_p6):
    """Provider with no secret_ref (e.g. SDK-auth claude_agent) can't
    store anything — must 400 cleanly so the UI doesn't try."""
    from backend.db import init_db, open_conn
    await init_db()
    async with open_conn() as conn:
        await conn.execute("DELETE FROM providers")
        await conn.commit()
    p = await providers_svc.create_provider(
        name="claude", provider_type="claude_agent", model_id="claude-opus-4-7",
        secret_ref=None,  # SDK auth, no key needed.
    )

    from fastapi.testclient import TestClient
    with TestClient(quiet_app_p6) as client:
        resp = client.post(
            f"/api/providers/{p.id}/set-secret",
            json={"value": "ignored"},
        )
    assert resp.status_code == 400
    assert "secret_ref" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_set_secret_route_404_when_provider_missing(quiet_app_p6):
    from fastapi.testclient import TestClient
    with TestClient(quiet_app_p6) as client:
        resp = client.post(
            "/api/providers/99999/set-secret",
            json={"value": "anything"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_set_secret_route_503_when_keyring_unavailable(monkeypatch, quiet_app_p6):
    """When keyring fails (no backend installed, headless Linux without
    dbus), the route must 503 with a useful 'set the env var instead'
    hint rather than masquerading as success."""
    from backend.db import init_db, open_conn
    await init_db()
    async with open_conn() as conn:
        await conn.execute("DELETE FROM providers")
        await conn.commit()
    p = await providers_svc.create_provider(
        name="gem", provider_type="gemini", model_id="m",
        secret_ref="MY_KEY",
    )

    fake_keyring = MagicMock()
    fake_keyring.set_password.side_effect = RuntimeError("no backend available")
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)

    from fastapi.testclient import TestClient
    with TestClient(quiet_app_p6) as client:
        resp = client.post(
            f"/api/providers/{p.id}/set-secret",
            json={"value": "sk-xyz"},
        )
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "MY_KEY" in detail
    assert "env" in detail.lower()


@pytest.mark.asyncio
async def test_delete_secret_route_204(monkeypatch, quiet_app_p6):
    """DELETE /providers/{id}/secret removes the stored key. Idempotent —
    no error when nothing was stored."""
    from backend.db import init_db, open_conn
    await init_db()
    async with open_conn() as conn:
        await conn.execute("DELETE FROM providers")
        await conn.commit()
    p = await providers_svc.create_provider(
        name="gem", provider_type="gemini", model_id="m",
        secret_ref="MY_KEY",
    )

    fake_keyring = MagicMock()
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)

    from fastapi.testclient import TestClient
    with TestClient(quiet_app_p6) as client:
        resp = client.delete(f"/api/providers/{p.id}/secret")
    assert resp.status_code == 204
    fake_keyring.delete_password.assert_called_once_with("LN-Translator", "MY_KEY")


@pytest.mark.asyncio
async def test_delete_secret_route_404_when_provider_missing(quiet_app_p6):
    from fastapi.testclient import TestClient
    with TestClient(quiet_app_p6) as client:
        resp = client.delete("/api/providers/99999/secret")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_set_secret_route_rejects_empty_value(quiet_app_p6, monkeypatch):
    """Pydantic min_length=1 must reject an empty string before it
    reaches keyring."""
    from backend.db import init_db, open_conn
    await init_db()
    async with open_conn() as conn:
        await conn.execute("DELETE FROM providers")
        await conn.commit()
    p = await providers_svc.create_provider(
        name="gem", provider_type="gemini", model_id="m",
        secret_ref="MY_KEY",
    )

    from fastapi.testclient import TestClient
    with TestClient(quiet_app_p6) as client:
        resp = client.post(
            f"/api/providers/{p.id}/set-secret",
            json={"value": ""},
        )
    # Pydantic validation error — 422 Unprocessable Entity.
    assert resp.status_code == 422


# ---- end-to-end wiring -----------------------------------------------------

@pytest.mark.asyncio
async def test_app_entry_wiring_reaches_health_and_shuts_down(monkeypatch):
    """_run_uvicorn against backend.main:app in a background thread,
    /api/health returns 200, _signal_shutdown stops the server cleanly.

    Shutdown is via server.should_exit (not SIGINT) because signal.signal
    only fires on the main thread and uvicorn's run() loop is on a worker
    here. This matches what _signal_shutdown does in production.
    """
    import asyncio
    import threading
    import time

    import httpx

    from backend import app_entry

    async def _no_probe(_default):
        return None
    async def _no_drain():
        return None
    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)

    port = app_entry._find_free_port()
    app_entry._server_ref.clear()

    thread = threading.Thread(
        target=app_entry._run_uvicorn,
        args=(port,),
        daemon=True,
        name="uvicorn-wiring-test",
    )
    thread.start()
    try:
        body = None
        # Generous deadline + a catch on timeouts: on a slow CI runner the
        # background uvicorn thread can take several seconds to begin accepting
        # connections, and an individual connect can exceed the per-request
        # timeout (raising httpx.ConnectTimeout, a TimeoutException — NOT a
        # ConnectError). Both must be tolerated and retried, or the test is
        # flaky on anything slower than a fast dev box.
        deadline = time.monotonic() + 30.0
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            while time.monotonic() < deadline:
                try:
                    r = await client.get("/api/health", timeout=2.0)
                    if r.status_code == 200:
                        body = r.json()
                        break
                except (
                    httpx.ConnectError,
                    httpx.ReadError,
                    httpx.RemoteProtocolError,
                    httpx.TimeoutException,
                ):
                    pass
                await asyncio.sleep(0.1)

        assert body is not None, (
            f"server did not reach /api/health on port {port} within 30s"
        )
        assert body["ok"] is True
    finally:
        app_entry._signal_shutdown()
        thread.join(timeout=5.0)
        assert not thread.is_alive(), (
            "uvicorn thread did not exit within 5s of _signal_shutdown"
        )


# ---- Windows console-close handler (Section 8) -----------------------------

def test_make_console_handler_routes_close_event_to_callback():
    """CTRL_CLOSE_EVENT (and friends) invoke the on_close callback and
    return True so Windows knows the event was handled."""
    fired: list[int] = []
    handler = _make_console_handler(lambda code: fired.append(code))
    assert handler(_CTRL_CLOSE_EVENT) is True
    assert fired == [_CTRL_CLOSE_EVENT]


def test_make_console_handler_returns_false_for_ctrl_c():
    """CTRL_C and CTRL_BREAK must pass through (return False) so Python's
    signal module — which main() wires SIGINT into — sees them. Otherwise
    Ctrl+C in the console would silently hit BOTH paths or neither."""
    fired: list[int] = []
    handler = _make_console_handler(lambda code: fired.append(code))
    CTRL_C_EVENT = 0
    CTRL_BREAK_EVENT = 1
    assert handler(CTRL_C_EVENT) is False
    assert handler(CTRL_BREAK_EVENT) is False
    assert fired == [], "Ctrl+C / Ctrl+Break must not consume the on_close callback"


def test_make_console_handler_swallows_callback_exception():
    """The handler runs on a Windows-managed thread; an exception in the
    user callback would crash the process. The handler must swallow and
    still return True so Windows proceeds with its 5s + SIGKILL flow."""
    def _boom(_code):
        raise RuntimeError("simulated callback failure")
    handler = _make_console_handler(_boom)
    # Should not raise, should return True (event was 'handled').
    assert handler(_CTRL_CLOSE_EVENT) is True


def test_install_windows_console_handler_noop_on_non_windows(monkeypatch):
    """Non-Windows platforms get a clean False — no ctypes import, no
    crash. main() continues without the handler."""
    monkeypatch.setattr(sys, "platform", "linux")
    assert _install_windows_console_handler(lambda _c: None) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Win32-only API")
def test_install_windows_console_handler_succeeds_on_windows():
    """On Windows, the real Win32 call should succeed. Tests the ctypes
    plumbing + that SetConsoleCtrlHandler returns nonzero. We don't try
    to TRIGGER a close event from the test — that would terminate the
    test process — just verify the registration path works."""
    from backend import app_entry as ae
    # Use a no-op callback; we're testing registration, not firing.
    assert _install_windows_console_handler(lambda _c: None) is True
    # The module-level reference must be set so Python's GC doesn't
    # reclaim the ctypes callback (which would segfault the kernel call
    # on the next console event).
    assert ae._WINDOWS_CTRL_HANDLER_REF is not None


def test_close_event_constants_match_win32_documented_values():
    """Sanity check on the named constants matching the Windows API
    docs — if these drift, the handler routes the wrong events."""
    assert _CTRL_CLOSE_EVENT == 2  # CTRL_CLOSE_EVENT
    assert _CONSOLE_CLOSE_EVENTS == {2, 5, 6}  # CTRL_CLOSE / CTRL_LOGOFF / CTRL_SHUTDOWN
