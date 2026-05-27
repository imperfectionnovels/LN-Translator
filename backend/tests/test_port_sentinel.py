"""Regression test for Bug #7: the EXE writes a port sentinel so smokes
can find the right server.

Plus regression tests for the wireframes-redesign persistence fix: the
sentinel is now ALSO consulted on launch so the port (and therefore the
URL origin, and therefore WebView2's localStorage / cookies / cache)
stays stable across launches."""

import socket
import tempfile
from pathlib import Path
from unittest.mock import patch

from backend.app_entry import (
    _PORT_SENTINEL_NAME,
    _find_free_port,
    _read_sentinel_port,
    _remove_port_sentinel,
    _write_port_sentinel,
)


def test_write_port_sentinel_writes_to_user_data_root():
    tmp = Path(tempfile.mkdtemp(prefix="sentinel-write-"))
    with patch("backend.config.USER_DATA_ROOT", tmp):
        sentinel = _write_port_sentinel(8765)
    assert sentinel is not None
    assert sentinel == tmp / _PORT_SENTINEL_NAME
    assert sentinel.read_text(encoding="utf-8") == "8765"


def test_write_port_sentinel_overwrites_existing():
    """A previous run's sentinel must not survive — overwrite, don't append."""
    tmp = Path(tempfile.mkdtemp(prefix="sentinel-overwrite-"))
    (tmp / _PORT_SENTINEL_NAME).write_text("8765", encoding="utf-8")
    with patch("backend.config.USER_DATA_ROOT", tmp):
        sentinel = _write_port_sentinel(8769)
    assert sentinel.read_text(encoding="utf-8") == "8769"


def test_remove_port_sentinel_is_idempotent():
    """Cleanup must not raise if the file isn't there (already removed,
    write failed silently, etc.)."""
    tmp = Path(tempfile.mkdtemp(prefix="sentinel-remove-"))
    sentinel = tmp / _PORT_SENTINEL_NAME
    sentinel.write_text("8765", encoding="utf-8")
    _remove_port_sentinel(sentinel)
    assert not sentinel.exists()
    # Second call is a no-op, not a crash.
    _remove_port_sentinel(sentinel)
    # None argument is also a no-op.
    _remove_port_sentinel(None)


# ===== Wireframes-redesign persistence fix =====

def test_read_sentinel_port_returns_value(monkeypatch, tmp_path):
    """The reader pulls a well-formed port from USER_DATA_ROOT/port.txt."""
    (tmp_path / _PORT_SENTINEL_NAME).write_text("9123", encoding="utf-8")
    monkeypatch.setattr("backend.config.USER_DATA_ROOT", tmp_path)
    assert _read_sentinel_port() == 9123


def test_read_sentinel_port_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.config.USER_DATA_ROOT", tmp_path)
    assert _read_sentinel_port() is None


def test_read_sentinel_port_rejects_garbage(monkeypatch, tmp_path):
    (tmp_path / _PORT_SENTINEL_NAME).write_text("not-a-port", encoding="utf-8")
    monkeypatch.setattr("backend.config.USER_DATA_ROOT", tmp_path)
    assert _read_sentinel_port() is None


def test_read_sentinel_port_rejects_out_of_range(monkeypatch, tmp_path):
    """Ports below 1024 (privileged) or above 65535 (invalid) get rejected
    so a corrupted file can't redirect the EXE somewhere weird."""
    for bad in ("0", "80", "65536", "-1"):
        (tmp_path / _PORT_SENTINEL_NAME).write_text(bad, encoding="utf-8")
        monkeypatch.setattr("backend.config.USER_DATA_ROOT", tmp_path)
        assert _read_sentinel_port() is None, f"should reject {bad!r}"


def test_find_free_port_reuses_sentinel_when_free(monkeypatch, tmp_path):
    """Production path: no explicit start, sentinel exists and port is
    free → reuse it. This is what makes the URL origin stable across
    launches so WebView2 localStorage persists."""
    # Pick a high port that's almost certainly free.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    # Now write that port to the sentinel. (probe is closed → port free.)
    (tmp_path / _PORT_SENTINEL_NAME).write_text(str(free_port), encoding="utf-8")
    monkeypatch.setattr("backend.config.USER_DATA_ROOT", tmp_path)

    assert _find_free_port() == free_port


def test_find_free_port_skips_sentinel_when_held(monkeypatch, tmp_path):
    """Sentinel pointing at a busy port → fall through to the walk
    starting at the default. Prevents the sentinel from making the EXE
    fail to launch when a stale instance is still bound."""
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held_port = held.getsockname()[1]
    try:
        (tmp_path / _PORT_SENTINEL_NAME).write_text(str(held_port), encoding="utf-8")
        monkeypatch.setattr("backend.config.USER_DATA_ROOT", tmp_path)
        result = _find_free_port()
        # Did NOT reuse the held sentinel.
        assert result != held_port
    finally:
        held.close()


def test_find_free_port_ignores_sentinel_when_start_passed(monkeypatch, tmp_path):
    """Explicit start= bypasses the sentinel — callers asking for a
    specific port range should get the walk-from-start contract."""
    # Sentinel says 9001, but caller explicitly wants 9500+.
    (tmp_path / _PORT_SENTINEL_NAME).write_text("9001", encoding="utf-8")
    monkeypatch.setattr("backend.config.USER_DATA_ROOT", tmp_path)
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held_port = held.getsockname()[1]
    try:
        result = _find_free_port(start=held_port)
        # Must walk from held_port, NOT jump to the sentinel.
        assert result >= held_port + 1
        assert result != 9001  # sentinel ignored
    finally:
        held.close()
