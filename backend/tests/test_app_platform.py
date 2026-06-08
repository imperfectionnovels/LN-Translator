"""Direct unit tests for backend.app_platform.

Covers the pure decision logic of the Windows platform shims without
touching real Win32 / ctypes machinery:
  - _install_no_window_subprocess_patch: no-op gate off-Windows / unfrozen,
    and the actual Popen.__init__ creationflags merge when forced "frozen".
  - _make_console_handler: the pure-Python console-event dispatcher, close/logoff/shutdown route to on_close and return True; Ctrl-C/Break
    return False; on_close exceptions are swallowed.
  - _install_windows_console_handler: returns False off-Windows.
  - The Win32 event-code constants / sets are wired correctly.

The CREATE_NO_WINDOW patch is only exercised by *forcing* sys.platform and
sys.frozen via monkeypatch, then restoring subprocess.Popen.__init__ so no
other test inherits the patched constructor.
"""

import subprocess
import sys

import backend.app_platform as platform


def test_console_event_constants_and_set():
    """The named Win32 constants match the documented DWORD values and the
    close-event set contains exactly close/logoff/shutdown."""
    assert platform._CTRL_CLOSE_EVENT == 2
    assert platform._CTRL_LOGOFF_EVENT == 5
    assert platform._CTRL_SHUTDOWN_EVENT == 6
    assert platform._CREATE_NO_WINDOW == 0x08000000
    assert platform._CONSOLE_CLOSE_EVENTS == frozenset({2, 5, 6})
    # Ctrl-C (0) and Ctrl-Break (1) are deliberately NOT in the close set.
    assert 0 not in platform._CONSOLE_CLOSE_EVENTS
    assert 1 not in platform._CONSOLE_CLOSE_EVENTS


def test_make_console_handler_routes_close_events():
    """Close / logoff / shutdown codes all invoke on_close and return True,
    forwarding the exact event code."""
    received = []
    handler = platform._make_console_handler(lambda code: received.append(code))

    assert handler(platform._CTRL_CLOSE_EVENT) is True
    assert handler(platform._CTRL_LOGOFF_EVENT) is True
    assert handler(platform._CTRL_SHUTDOWN_EVENT) is True
    # on_close fired once per close-class event, with the right codes.
    assert received == [2, 5, 6]


def test_make_console_handler_passes_ctrl_c_through():
    """Ctrl-C (0) and Ctrl-Break (1) return False so Python's own signal
    machinery sees them; on_close is NOT called for them."""
    received = []
    handler = platform._make_console_handler(lambda code: received.append(code))

    assert handler(0) is False  # CTRL_C_EVENT
    assert handler(1) is False  # CTRL_BREAK_EVENT
    assert handler(99) is False  # any unknown code
    # No close-class event fired -> on_close never invoked.
    assert received == []


def test_make_console_handler_swallows_on_close_exception(caplog):
    """An exception in on_close must not propagate (it runs on a Windows
    control-handler thread); the handler still returns True and logs."""
    import logging

    def boom(_code):
        raise ValueError("handler exploded")

    handler = platform._make_console_handler(boom)
    with caplog.at_level(logging.DEBUG, logger="backend.app_platform"):
        result = handler(platform._CTRL_CLOSE_EVENT)
    assert result is True  # event still reported as handled
    # The swallow path logged the failure with the event code.
    assert any("on_close" in r.getMessage() for r in caplog.records)
    # And it really did not raise out of the handler (we got here).
    assert isinstance(result, bool)


def test_install_no_window_patch_noop_off_windows(monkeypatch):
    """Off Windows the patch is a no-op: Popen.__init__ is left untouched
    and carries no _ln_no_window marker."""
    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    before = subprocess.Popen.__init__

    platform._install_no_window_subprocess_patch()

    assert subprocess.Popen.__init__ is before
    assert getattr(subprocess.Popen.__init__, "_ln_no_window", False) is False


def test_install_no_window_patch_noop_when_not_frozen(monkeypatch):
    """On Windows but a non-frozen (dev) run, the patch is still a no-op, a terminal already owns a console."""
    monkeypatch.setattr(sys, "platform", "win32", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    before = subprocess.Popen.__init__

    platform._install_no_window_subprocess_patch()

    assert subprocess.Popen.__init__ is before
    assert getattr(subprocess.Popen.__init__, "_ln_no_window", False) is False


def test_install_no_window_patch_merges_creationflags(monkeypatch):
    """Forced 'frozen Windows': the patch wraps Popen.__init__ so every
    construction OR-s in CREATE_NO_WINDOW. We capture the flags via a fake
    original __init__ and assert the merge for both the default-None and
    pre-set-flags cases. Idempotent (a second install does not re-wrap).

    The real subprocess.Popen.__init__ is restored in a finally so no other
    test inherits the patched constructor.
    """
    monkeypatch.setattr(sys, "platform", "win32", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    captured = {}

    def fake_init(self, *args, **kwargs):
        captured["creationflags"] = kwargs.get("creationflags")

    original = subprocess.Popen.__init__
    subprocess.Popen.__init__ = fake_init
    try:
        platform._install_no_window_subprocess_patch()
        patched = subprocess.Popen.__init__
        assert getattr(patched, "_ln_no_window", False) is True
        assert patched is not fake_init  # it was wrapped

        # Idempotent: a second install must not stack another wrapper.
        platform._install_no_window_subprocess_patch()
        assert subprocess.Popen.__init__ is patched

        sentinel = object()  # stand-in for a real Popen instance

        # Case 1: no creationflags supplied -> exactly CREATE_NO_WINDOW.
        patched(sentinel)
        assert captured["creationflags"] == platform._CREATE_NO_WINDOW

        # Case 2: existing flags -> OR-ed with CREATE_NO_WINDOW.
        patched(sentinel, creationflags=0x00000010)
        assert captured["creationflags"] == (0x00000010 | platform._CREATE_NO_WINDOW)
        assert captured["creationflags"] & platform._CREATE_NO_WINDOW
    finally:
        subprocess.Popen.__init__ = original
    # Confirm the restore really happened.
    assert subprocess.Popen.__init__ is original
    assert getattr(subprocess.Popen.__init__, "_ln_no_window", False) is False


def test_install_windows_console_handler_false_off_windows(monkeypatch):
    """Off Windows the registration short-circuits to False and never
    touches ctypes."""
    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    calls = []
    result = platform._install_windows_console_handler(lambda code: calls.append(code))
    assert result is False
    assert isinstance(result, bool)
    # on_close was never invoked during a failed registration.
    assert calls == []
