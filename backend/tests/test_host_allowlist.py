"""H1: Host-header allowlist (DNS-rebinding / CSRF hardening).

The server binds 127.0.0.1 only, but a Host allowlist additionally defeats
browser-mediated DNS rebinding. conftest.py adds "testserver" (the TestClient
default Host) to LN_TRANSLATOR_ALLOWED_HOSTS so the rest of the suite passes.
"""

from __future__ import annotations

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
