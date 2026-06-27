"""Direct unit tests for backend.app_ui.

Covers the UI-surface helpers' decision logic without ever opening a real
pywebview window or launching a real browser:
  - _resolve_webview_storage_path: creates a stable on-disk dir under a
    tmp USER_DATA_ROOT and returns its string path.
  - _install_startup_log_handler: attaches one RotatingFileHandler, is
    idempotent, and returns the log path.
  - _try_import_pywebview: returns the module on success, None on failure
    (we stub the import to raise).
  - _run_window: wires window.events.closing into the shutdown funnel and
    passes the persistence kwargs to a *fake* webview module; we assert the
    captured create_window / start args and the closing-callback funnel.
  - _run_browser_fallback: opens the (stubbed) URL and exits promptly once
    the shutdown event is set.

webview.create_window / webview.start / webbrowser.open are all replaced
with record-only fakes; value assertions dominate.
"""

import logging
import threading
import types

import backend.app_ui as ui


# --------------------------------------------------------------------------
# _resolve_webview_storage_path
# --------------------------------------------------------------------------
def test_resolve_webview_storage_path_creates_dir(monkeypatch, tmp_path):
    """Returns a string path under USER_DATA_ROOT/webview-data and creates
    the directory if missing."""
    import backend.config as config

    monkeypatch.setattr(config, "USER_DATA_ROOT", tmp_path, raising=False)
    expected = tmp_path / "webview-data"
    assert not expected.exists()

    result = ui._resolve_webview_storage_path()

    assert result == str(expected)
    assert expected.is_dir()
    # Idempotent: second call returns the same path, dir still present.
    assert ui._resolve_webview_storage_path() == str(expected)
    assert expected.is_dir()


# --------------------------------------------------------------------------
# _install_startup_log_handler
# --------------------------------------------------------------------------
def test_install_startup_log_handler_attaches_once(monkeypatch, tmp_path):
    """Creates logs/startup.log, attaches exactly one marked
    RotatingFileHandler, and is idempotent across repeated calls."""
    import logging.handlers

    import backend.config as config

    monkeypatch.setattr(config, "USER_DATA_ROOT", tmp_path, raising=False)
    root = logging.getLogger()
    # Snapshot any pre-existing marked handlers so we can clean up precisely.
    preexisting = [
        h for h in root.handlers
        if getattr(h, "_ln_translator_startup_log", False)
    ]
    for h in preexisting:
        root.removeHandler(h)

    try:
        log_path = ui._install_startup_log_handler()
        assert log_path == tmp_path / "logs" / "startup.log"
        assert (tmp_path / "logs").is_dir()

        marked = [
            h for h in root.handlers
            if getattr(h, "_ln_translator_startup_log", False)
        ]
        assert len(marked) == 1
        assert isinstance(marked[0], logging.handlers.RotatingFileHandler)
        assert marked[0].level == logging.INFO

        # Idempotent: a second call returns the same path and does NOT add
        # a duplicate handler.
        again = ui._install_startup_log_handler()
        assert again == log_path
        marked_after = [
            h for h in root.handlers
            if getattr(h, "_ln_translator_startup_log", False)
        ]
        assert len(marked_after) == 1
    finally:
        for h in list(root.handlers):
            if getattr(h, "_ln_translator_startup_log", False):
                root.removeHandler(h)
                h.close()


def test_install_startup_log_handler_returns_none_on_bad_root(monkeypatch):
    """If USER_DATA_ROOT resolution / mkdir blows up, the helper returns
    None instead of raising, logging degrades to stderr-only."""
    import backend.config as config

    class _Boom:
        def __truediv__(self, _other):
            raise OSError("simulated unwritable data root")

    monkeypatch.setattr(config, "USER_DATA_ROOT", _Boom(), raising=False)
    result = ui._install_startup_log_handler()
    assert result is None


# --------------------------------------------------------------------------
# _try_import_pywebview
# --------------------------------------------------------------------------
def test_try_import_pywebview_returns_none_on_failure(monkeypatch, caplog):
    """When `import webview` raises, the helper logs a warning and returns
    None so the caller can fall back to a browser tab."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "webview":
            raise ImportError("no webview here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with caplog.at_level(logging.WARNING, logger="backend.app_ui"):
        result = ui._try_import_pywebview()
    assert result is None
    assert any("pywebview unavailable" in r.getMessage() for r in caplog.records)


def test_try_import_pywebview_returns_module_on_success(monkeypatch):
    """When `import webview` succeeds, the helper returns that module
    object verbatim."""
    import builtins
    import sys

    fake_mod = types.ModuleType("webview")
    fake_mod.MARKER = "fake-webview"
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "webview":
            return fake_mod
        return real_import(name, *args, **kwargs)

    monkeypatch.setitem(sys.modules, "webview", fake_mod)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = ui._try_import_pywebview()
    assert result is fake_mod
    assert result.MARKER == "fake-webview"


# --------------------------------------------------------------------------
# _run_window  (fake pywebview module, no real window)
# --------------------------------------------------------------------------
class _FakeEvents:
    def __init__(self):
        self.subscribers = []

    @property
    def closing(self):
        return self

    @closing.setter
    def closing(self, value):
        # `events.closing += cb` desugars to set(closing.__iadd__(cb)).
        pass

    def __iadd__(self, cb):
        self.subscribers.append(cb)
        return self


class _FakeWindow:
    def __init__(self):
        self.events = _FakeEvents()


class _FakeWebview:
    def __init__(self):
        self.create_window_calls = []
        self.start_calls = []
        self.window = _FakeWindow()
        # Mirror pywebview's default: downloads off until the app enables them.
        self.settings = {"ALLOW_DOWNLOADS": False}

    def create_window(self, title, **kwargs):
        self.create_window_calls.append((title, kwargs))
        return self.window

    def start(self, **kwargs):
        self.start_calls.append(kwargs)


def test_run_window_passes_persistence_kwargs(monkeypatch, tmp_path):
    """_run_window must create the window with text_select=True /
    confirm_close=False and start it with private_mode=False plus a
    storage_path, the persistence-critical settings."""
    import backend.config as config

    monkeypatch.setattr(config, "USER_DATA_ROOT", tmp_path, raising=False)
    fake = _FakeWebview()
    shutdown_event = threading.Event()

    ui._run_window(fake, "http://127.0.0.1:8765/onboarding", shutdown_event)

    # create_window: title + the selection/close kwargs.
    assert len(fake.create_window_calls) == 1
    title, kwargs = fake.create_window_calls[0]
    assert title == "LN-Translator"
    assert kwargs["url"] == "http://127.0.0.1:8765/onboarding"
    assert kwargs["text_select"] is True
    assert kwargs["confirm_close"] is False
    assert kwargs["resizable"] is True

    # start: persistence kwargs.
    assert len(fake.start_calls) == 1
    start_kwargs = fake.start_calls[0]
    assert start_kwargs["private_mode"] is False
    assert start_kwargs["debug"] is False
    assert start_kwargs["storage_path"] == str(tmp_path / "webview-data")


def test_run_window_enables_downloads(monkeypatch, tmp_path):
    """pywebview defaults ALLOW_DOWNLOADS=False and the WebView2 backend
    cancels every download when it is false, which silently broke the in-app
    .txt/.md/.epub and glossary exports. _run_window must flip it on."""
    import backend.config as config

    monkeypatch.setattr(config, "USER_DATA_ROOT", tmp_path, raising=False)
    fake = _FakeWebview()
    assert fake.settings["ALLOW_DOWNLOADS"] is False  # pywebview default

    ui._run_window(fake, "http://127.0.0.1:8765/", threading.Event())

    assert fake.settings["ALLOW_DOWNLOADS"] is True


def test_run_window_survives_missing_settings(monkeypatch, tmp_path):
    """A pywebview build without a `settings` mapping must not crash launch:
    the download-enable is best-effort, the window still opens."""
    import backend.config as config

    monkeypatch.setattr(config, "USER_DATA_ROOT", tmp_path, raising=False)
    fake = _FakeWebview()
    del fake.settings  # simulate an older/odd pywebview with no settings attr

    ui._run_window(fake, "http://127.0.0.1:8765/", threading.Event())

    # Window still created + started despite the missing settings mapping.
    assert len(fake.create_window_calls) == 1
    assert len(fake.start_calls) == 1


def test_run_window_closing_callback_funnels_shutdown(monkeypatch, tmp_path):
    """The closing subscriber, when fired, sets the shutdown event and
    signals uvicorn through the shared bridge."""
    import backend.app_shutdown as shutdown
    import backend.config as config

    monkeypatch.setattr(config, "USER_DATA_ROOT", tmp_path, raising=False)

    # Register a fake server so _signal_shutdown has something to flip.
    class _FakeServer:
        should_exit = False

    shutdown._server_ref.clear()
    server = _FakeServer()
    shutdown._server_ref["server"] = server

    fake = _FakeWebview()
    shutdown_event = threading.Event()
    try:
        ui._run_window(fake, "http://127.0.0.1:8765/", shutdown_event)

        # Exactly one closing subscriber was registered.
        subs = fake.window.events.subscribers
        assert len(subs) == 1
        assert shutdown_event.is_set() is False

        # Fire it: event set + server signalled.
        subs[0]()
        assert shutdown_event.is_set() is True
        assert server.should_exit is True

        # Idempotent second fire.
        subs[0]()
        assert shutdown_event.is_set() is True
    finally:
        shutdown._server_ref.clear()


def test_run_window_falls_back_on_typeerror_start(monkeypatch, tmp_path):
    """Older pywebview that rejects storage_path/private_mode raises
    TypeError on the first start(); _run_window retries with the bare
    start(debug=False) call so the EXE still launches."""
    import backend.config as config

    monkeypatch.setattr(config, "USER_DATA_ROOT", tmp_path, raising=False)

    fake = _FakeWebview()
    calls = []

    def picky_start(**kwargs):
        calls.append(kwargs)
        if "storage_path" in kwargs or "private_mode" in kwargs:
            raise TypeError("unexpected kwarg")
        # bare retry path records here

    fake.start = picky_start
    shutdown_event = threading.Event()

    ui._run_window(fake, "http://127.0.0.1:8765/", shutdown_event)

    # First call carried the rich kwargs (and raised); the retry was bare.
    assert len(calls) == 2
    assert "private_mode" in calls[0]
    assert calls[1] == {"debug": False}


# --------------------------------------------------------------------------
# _run_browser_fallback  (stubbed webbrowser.open, no real browser)
# --------------------------------------------------------------------------
def test_run_browser_fallback_opens_url_and_exits(monkeypatch):
    """Opens the URL via webbrowser.open(url, new=2) then returns promptly
    because the shutdown event is already set."""
    opened = []
    monkeypatch.setattr(
        ui.webbrowser, "open", lambda url, new=0: opened.append((url, new)) or True
    )

    shutdown_event = threading.Event()
    shutdown_event.set()  # makes the sleep-loop exit on the first check
    # A thread that is already finished -> is_alive() False, loop won't spin.
    dead_thread = threading.Thread(target=lambda: None)
    dead_thread.start()
    dead_thread.join()

    ui._run_browser_fallback(
        "http://127.0.0.1:8765/",
        shutdown_event,
        dead_thread,
        reason="unit-test",
    )

    assert opened == [("http://127.0.0.1:8765/", 2)]
    assert len(opened) == 1
    assert opened[0][1] == 2  # new=2 -> new browser tab


def test_run_browser_fallback_survives_open_failure(monkeypatch, caplog):
    """If webbrowser.open raises, the fallback logs and still returns the
    user a usable URL rather than crashing the process."""
    def boom(*_a, **_k):
        raise RuntimeError("no browser available")

    monkeypatch.setattr(ui.webbrowser, "open", boom)

    shutdown_event = threading.Event()
    shutdown_event.set()
    dead_thread = threading.Thread(target=lambda: None)
    dead_thread.start()
    dead_thread.join()

    with caplog.at_level(logging.WARNING, logger="backend.app_ui"):
        # Must not raise despite the open() failure.
        ui._run_browser_fallback(
            "http://127.0.0.1:8765/",
            shutdown_event,
            dead_thread,
            reason="open-fails",
        )
    assert any("webbrowser.open failed" in r.getMessage() for r in caplog.records)
    assert shutdown_event.is_set() is True
