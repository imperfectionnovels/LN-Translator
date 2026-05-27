"""Regression tests for Bug #2: pywebview fallback must engage when
`webview.start()` fails, not just when the import fails.

On Windows, `import webview` succeeds even without the WebView2 Evergreen
Runtime — the failure is deferred until `webview.start()` is called.
Without this fix the EXE would silently exit on Win10 machines lacking
WebView2 instead of degrading to a browser tab.
"""

import threading
import types
from unittest.mock import MagicMock

import pytest

from backend.app_entry import _run_browser_fallback, _run_window


class _FakeWindowEvents:
    closing = MagicMock()
    # Make `events.closing += ...` work (returns self).
    def __iadd__(self, other):
        return self


class _FakeWindow:
    events = _FakeWindowEvents()


class _BadEventsContainer:
    """Pywebview-shaped object whose `closing` attribute can't be
    subscribed to via `+=`. Used to simulate Bug #6's failure mode."""
    class _Events:
        @property
        def closing(self):
            raise AttributeError("simulated pywebview API mismatch")
    events = _Events()


class _WindowWithBadEvents:
    events = _BadEventsContainer.events


def _fake_webview(start_raises: Exception | None):
    """Build a stand-in pywebview module. If start_raises is set,
    `start()` raises it; otherwise start() is a no-op."""
    mod = types.SimpleNamespace()

    def create_window(*a, **k):
        return _FakeWindow()

    def start(*a, **k):
        if start_raises is not None:
            raise start_raises

    mod.create_window = create_window
    mod.start = start
    return mod


def test_run_window_propagates_start_failure():
    """_run_window MUST raise when webview.start() raises — otherwise the
    fallback in _main_inner never engages. This was the live bug:
    swallowing the start() exception would have hidden the WebView2
    runtime-missing case."""
    fake = _fake_webview(RuntimeError("WebView2 runtime not found"))
    shutdown_event = threading.Event()
    with pytest.raises(RuntimeError, match="WebView2"):
        _run_window(fake, "http://127.0.0.1:9999/", shutdown_event)


def test_run_window_succeeds_when_start_returns_normally():
    """Happy path: start() returns, _run_window returns, no exception."""
    fake = _fake_webview(None)
    shutdown_event = threading.Event()
    # Should not raise; control returns once `start()` returns.
    _run_window(fake, "http://127.0.0.1:9999/", shutdown_event)


def test_run_window_logs_subscription_failure_with_exception_detail(caplog):
    """Bug #6 regression: when `window.events.closing += ...` fails, the
    warning that lands in startup.log must include the exception type and
    message so the maintainer can diagnose. The original implementation
    logged a bare 'could not subscribe' with no detail."""
    import logging

    fake = _fake_webview(None)
    fake.create_window = lambda *a, **k: _WindowWithBadEvents()
    shutdown_event = threading.Event()
    with caplog.at_level(logging.WARNING, logger="backend.app_entry"):
        _run_window(fake, "http://127.0.0.1:9999/", shutdown_event)
    matching = [r for r in caplog.records if "could not subscribe" in r.message]
    assert matching, "expected subscription warning to be logged"
    # The warning must mention the exception class and message so the user
    # has enough context to file a bug against pywebview.
    msg = matching[0].getMessage()
    assert "AttributeError" in msg, f"warning missing exception type: {msg!r}"
    assert "simulated pywebview API mismatch" in msg, (
        f"warning missing exception message: {msg!r}"
    )


def test_run_browser_fallback_exits_on_shutdown_event(monkeypatch):
    """_run_browser_fallback sleep-loops until shutdown_event fires.
    Verify it doesn't spin forever — set the event, then call, then
    confirm it returns quickly."""
    # No real browser open in the test.
    monkeypatch.setattr("backend.app_entry.webbrowser.open", lambda *a, **k: True)
    shutdown_event = threading.Event()
    shutdown_event.set()
    # Fake uvicorn thread; the loop's `is_alive()` check will short-circuit
    # because the shutdown_event is already set.
    uvicorn_thread = threading.Thread(target=lambda: None)
    uvicorn_thread.start()
    uvicorn_thread.join()
    _run_browser_fallback(
        "http://127.0.0.1:9999/",
        shutdown_event,
        uvicorn_thread,
        reason="test",
    )
