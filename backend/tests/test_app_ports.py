"""Direct unit tests for backend.app_ports.

Covers the pure decision logic of the frozen-desktop port helpers:
  - _port_is_free: bind probe against a deliberately-occupied socket.
  - _read_sentinel_port: well-formed / malformed / out-of-range / missing.
  - _write_port_sentinel + _remove_port_sentinel: round-trip + idempotent cleanup.
  - _find_free_port: sentinel reuse, walk-up past a busy port, explicit-start
    bypass of the sentinel, exhaustion -> RuntimeError.

No server is bound for any real length of time; we bind a single throwaway
socket to force one port "busy" and assert the probe skips it. backend.config
is imported lazily inside each target function, so we monkeypatch
backend.config.USER_DATA_ROOT to a tmp_path for the sentinel-file tests.
"""

import socket
from contextlib import closing

import pytest

import backend.app_ports as ports


def _occupy_port() -> tuple[socket.socket, int]:
    """Bind a listening socket on an ephemeral loopback port and return
    (socket, port). Caller must close the socket. While open, that port
    is genuinely unbindable, so _port_is_free must report False for it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def test_port_is_free_true_for_unbound_port():
    """A port we just released is bindable, so _port_is_free is True."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    # Socket is now closed -> port is free again.
    assert ports._port_is_free(free_port) is True
    # And the helper returns an actual bool, not a truthy object.
    assert isinstance(ports._port_is_free(free_port), bool)
    assert ports._PORT_PROBE_START == 8765


def test_port_is_free_false_when_occupied():
    """A port held by a live listening socket is not bindable."""
    sock, port = _occupy_port()
    try:
        assert ports._port_is_free(port) is False
        # Sanity: a different probe of the same port stays False while held.
        assert ports._port_is_free(port) is False
        assert isinstance(port, int)
    finally:
        sock.close()
    # After release the same port is free again.
    assert ports._port_is_free(port) is True


def test_read_sentinel_port_valid(monkeypatch, tmp_path):
    """A well-formed in-range sentinel file is parsed to its int value."""
    import backend.config as config

    monkeypatch.setattr(config, "USER_DATA_ROOT", tmp_path, raising=False)
    (tmp_path / ports._PORT_SENTINEL_NAME).write_text("  9123\n", encoding="utf-8")
    assert ports._read_sentinel_port() == 9123
    # Distinct value to prove it's reading the file, not a constant.
    (tmp_path / ports._PORT_SENTINEL_NAME).write_text("8765", encoding="utf-8")
    assert ports._read_sentinel_port() == 8765
    assert ports._PORT_SENTINEL_NAME == "port.txt"


def test_read_sentinel_port_missing_returns_none(monkeypatch, tmp_path):
    """No sentinel file at all -> None (caller falls back to the walk)."""
    import backend.config as config

    monkeypatch.setattr(config, "USER_DATA_ROOT", tmp_path, raising=False)
    assert not (tmp_path / ports._PORT_SENTINEL_NAME).exists()
    assert ports._read_sentinel_port() is None
    # Still None on a second read (no accidental file creation).
    assert ports._read_sentinel_port() is None
    assert not (tmp_path / ports._PORT_SENTINEL_NAME).exists()


def test_read_sentinel_port_malformed_and_out_of_range(monkeypatch, tmp_path):
    """Non-numeric, too-low, and too-high contents all return None."""
    import backend.config as config

    monkeypatch.setattr(config, "USER_DATA_ROOT", tmp_path, raising=False)
    sentinel = tmp_path / ports._PORT_SENTINEL_NAME

    sentinel.write_text("not-a-port", encoding="utf-8")
    assert ports._read_sentinel_port() is None

    sentinel.write_text("80", encoding="utf-8")  # below 1024
    assert ports._read_sentinel_port() is None

    sentinel.write_text("70000", encoding="utf-8")  # above 65535
    assert ports._read_sentinel_port() is None

    # Boundary values are accepted.
    sentinel.write_text("1024", encoding="utf-8")
    assert ports._read_sentinel_port() == 1024
    sentinel.write_text("65535", encoding="utf-8")
    assert ports._read_sentinel_port() == 65535


def test_write_and_remove_port_sentinel_roundtrip(monkeypatch, tmp_path):
    """_write_port_sentinel persists the port; _read_sentinel_port reads it
    back; _remove_port_sentinel deletes it idempotently."""
    import backend.config as config

    monkeypatch.setattr(config, "USER_DATA_ROOT", tmp_path, raising=False)

    written = ports._write_port_sentinel(8801)
    assert written is not None
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == "8801"
    # Round-trips through the reader.
    assert ports._read_sentinel_port() == 8801

    ports._remove_port_sentinel(written)
    assert not written.exists()
    # Idempotent: removing again (file gone) does not raise.
    ports._remove_port_sentinel(written)
    assert not written.exists()
    # Removing None is a no-op.
    assert ports._remove_port_sentinel(None) is None


def test_write_port_sentinel_creates_missing_parent(monkeypatch, tmp_path):
    """USER_DATA_ROOT that does not yet exist is created (mkdir parents)."""
    import backend.config as config

    nested = tmp_path / "deep" / "nested" / "data"
    monkeypatch.setattr(config, "USER_DATA_ROOT", nested, raising=False)
    assert not nested.exists()

    written = ports._write_port_sentinel(8888)
    assert written is not None
    assert nested.is_dir()
    assert written.read_text(encoding="utf-8") == "8888"


def test_find_free_port_explicit_start_bypasses_sentinel(monkeypatch, tmp_path):
    """Passing an explicit start != _PORT_PROBE_START must NOT consult the
    sentinel; it returns a free port at or above that start."""
    import backend.config as config

    monkeypatch.setattr(config, "USER_DATA_ROOT", tmp_path, raising=False)
    # Plant a sentinel that should be ignored because start is explicit.
    (tmp_path / ports._PORT_SENTINEL_NAME).write_text("9999", encoding="utf-8")

    # Reserve a free start port by binding then releasing it.
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        start = s.getsockname()[1]
    got = ports._find_free_port(start=start)
    assert got >= start
    assert got != 9999  # sentinel ignored
    assert ports._port_is_free(got) is True


def test_find_free_port_walks_past_busy_port(monkeypatch):
    """When the start port is occupied, the probe walks up to the next
    free port. We hold the start port busy with a live socket."""
    sock, busy = _occupy_port()
    try:
        got = ports._find_free_port(start=busy)
        assert got != busy
        assert got > busy
        assert ports._port_is_free(got) is True
    finally:
        sock.close()


def test_find_free_port_reuses_free_sentinel(monkeypatch, tmp_path):
    """Default-start path: a sentinel port that is currently free is
    returned verbatim, keeping the URL origin stable across launches."""
    import backend.config as config

    monkeypatch.setattr(config, "USER_DATA_ROOT", tmp_path, raising=False)
    # Find a genuinely-free port, write it as the sentinel.
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    (tmp_path / ports._PORT_SENTINEL_NAME).write_text(str(free_port), encoding="utf-8")

    got = ports._find_free_port()  # no explicit start -> sentinel honored
    assert got == free_port
    assert ports._port_is_free(got) is True


def test_find_free_port_ignores_busy_sentinel(monkeypatch, tmp_path):
    """Default-start path: a sentinel pointing at a busy port is skipped,
    and the walk from _PORT_PROBE_START supplies a free port instead."""
    import backend.config as config

    monkeypatch.setattr(config, "USER_DATA_ROOT", tmp_path, raising=False)
    sock, busy = _occupy_port()
    try:
        (tmp_path / ports._PORT_SENTINEL_NAME).write_text(str(busy), encoding="utf-8")
        got = ports._find_free_port()
        assert got != busy
        assert got >= ports._PORT_PROBE_START
        assert ports._port_is_free(got) is True
    finally:
        sock.close()


def test_find_free_port_exhaustion_raises(monkeypatch):
    """If every probed port reports busy, _find_free_port raises RuntimeError
    rather than scanning the whole port space. We force _port_is_free False."""
    monkeypatch.setattr(ports, "_port_is_free", lambda port: False)
    with pytest.raises(RuntimeError, match="could not find a free localhost port"):
        ports._find_free_port(start=20000)


def test_wait_for_health_times_out_false(monkeypatch):
    """_wait_for_health returns False when the endpoint never answers 200.
    We point it at a closed port and use a tiny timeout so the test is fast,
    and stub time.sleep so the poll loop doesn't actually wait."""
    monkeypatch.setattr(ports.time, "sleep", lambda *_a, **_k: None)
    # Pick a port that is almost certainly closed (nothing listening).
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    # Socket closed -> nothing answers; short timeout -> quick False.
    result = ports._wait_for_health(dead_port, timeout=0.05)
    assert result is False
    assert isinstance(result, bool)
