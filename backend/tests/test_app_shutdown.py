"""Direct unit tests for backend.app_shutdown.

The shutdown bridge is a single mutable module-level dict (_server_ref)
plus _signal_shutdown(), which flips server.should_exit when a server is
registered. We assert:
  - the no-server case is a safe no-op (no exception),
  - registering a fake server and signalling sets should_exit,
  - _server_ref is the same mutable object every consumer mutates in place,
  - clearing the ref makes a later signal a no-op again.

Each test restores _server_ref to empty so ordering can't leak state.
"""

import backend.app_shutdown as shutdown


class _FakeServer:
    """Minimal stand-in for uvicorn.Server: only should_exit is read."""

    def __init__(self):
        self.should_exit = False


def _reset_ref():
    shutdown._server_ref.clear()


def test_signal_shutdown_noop_without_server():
    """With no server registered, _signal_shutdown is a quiet no-op."""
    _reset_ref()
    assert shutdown._server_ref.get("server") is None
    # Must not raise even though there is nothing to signal.
    assert shutdown._signal_shutdown() is None
    assert shutdown._server_ref.get("server") is None


def test_signal_shutdown_sets_should_exit():
    """A registered server has should_exit flipped True on signal."""
    _reset_ref()
    server = _FakeServer()
    assert server.should_exit is False
    shutdown._server_ref["server"] = server

    shutdown._signal_shutdown()

    assert server.should_exit is True
    # Idempotent: a second signal keeps it True, no error.
    shutdown._signal_shutdown()
    assert server.should_exit is True
    _reset_ref()


def test_server_ref_is_shared_mutable_object():
    """Re-importing the bridge yields the SAME dict object, so a mutation
    via one reference is visible through another (the whole reason it's a
    module-level dict rather than a rebindable value)."""
    _reset_ref()
    from backend.app_entry import _server_ref as ref_b  # re-exported
    from backend.app_shutdown import _server_ref as ref_a

    assert ref_a is shutdown._server_ref
    assert ref_b is shutdown._server_ref

    server = _FakeServer()
    ref_a["server"] = server
    # Visible through the other handle and the canonical module attr.
    assert ref_b["server"] is server
    assert shutdown._server_ref["server"] is server
    _reset_ref()


def test_clearing_ref_restores_noop_behavior():
    """After clear(), a previously-registered server is no longer signalled."""
    _reset_ref()
    first = _FakeServer()
    shutdown._server_ref["server"] = first
    shutdown._signal_shutdown()
    assert first.should_exit is True

    shutdown._server_ref.clear()
    # New server NOT registered -> signalling does nothing to it.
    second = _FakeServer()
    shutdown._signal_shutdown()
    assert second.should_exit is False
    assert shutdown._server_ref.get("server") is None


def test_signal_shutdown_via_entry_reexport_hits_same_server():
    """app_entry._signal_shutdown is the same callable bound to the same
    _server_ref, so signalling through it flips the registered server."""
    _reset_ref()
    from backend.app_entry import _signal_shutdown as entry_signal

    server = _FakeServer()
    shutdown._server_ref["server"] = server
    entry_signal()
    assert server.should_exit is True
    assert entry_signal is shutdown._signal_shutdown
    _reset_ref()
