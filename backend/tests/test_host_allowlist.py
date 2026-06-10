"""H1: Host-header allowlist (DNS-rebinding / CSRF hardening).

The server binds 127.0.0.1 only, but a Host allowlist additionally defeats
browser-mediated DNS rebinding. conftest.py adds "testserver" (the TestClient
default Host) to LN_TRANSLATOR_ALLOWED_HOSTS so the rest of the suite passes.
"""

from __future__ import annotations

import contextlib
import importlib
import os

import pytest
from fastapi.testclient import TestClient

from backend.main import app


def test_foreign_host_rejected():
    client = TestClient(app)
    r = client.get("/api/health", headers={"host": "evil.example.com"})
    assert r.status_code == 400


def test_allowed_hosts_ok():
    client = TestClient(app)
    # Explicit loopback host with a port (what the EXE / WebView2 sends).
    assert client.get("/api/health", headers={"host": "127.0.0.1:8765"}).status_code == 200
    # Default TestClient host is "testserver", added to the allowlist by conftest.
    assert client.get("/api/health").status_code == 200


@pytest.mark.skipif(os.name != "nt", reason="Windows user-registry fallback")
def test_allowed_hosts_falls_back_to_user_registry():
    """A process inherits its launcher's environment snapshot, so an EXE
    relaunched from a stale shell loses a setx-style user variable. With the
    process env unset, ALLOWED_HOSTS must come from HKCU\\Environment (mocked
    here) instead of silently dropping to the localhost default."""
    import winreg

    import backend.config as config

    saved_env = os.environ.pop("LN_TRANSLATOR_ALLOWED_HOSTS", None)
    saved_open, saved_query = winreg.OpenKey, winreg.QueryValueEx
    try:
        winreg.OpenKey = lambda *a, **k: contextlib.nullcontext()
        winreg.QueryValueEx = (
            lambda key, name: ("127.0.0.1,phone.example.ts.net", 1)
        )
        cfg = importlib.reload(config)
        assert "phone.example.ts.net" in cfg.ALLOWED_HOSTS
        assert "127.0.0.1" in cfg.ALLOWED_HOSTS
    finally:
        winreg.OpenKey, winreg.QueryValueEx = saved_open, saved_query
        if saved_env is not None:
            os.environ["LN_TRANSLATOR_ALLOWED_HOSTS"] = saved_env
        importlib.reload(config)
