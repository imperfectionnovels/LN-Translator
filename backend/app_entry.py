"""Desktop EXE entry point.

Run as `python -m backend.app_entry` in dev, or as the entry point of the
PyInstaller-frozen executable in production. This is the single application
entry point; the server is bound to loopback only.

What this does:
  1. Pick a free localhost port starting at 8765 (8766, 8767, ... if 8765
     is taken). Never falls back to 0.0.0.0 — we deliberately bind to
     loopback only so the EXE doesn't accidentally expose itself on the LAN.
  2. Start uvicorn in a background thread (uvicorn's own event loop).
  3. Wait until /api/health returns 200.
  4. Decide first-run routing: no providers configured (or
     `config_kv.first_run_complete` unset) → /onboarding, else /.
  5. Surface the UI. Three distinct modes:
     a. **Window mode** (default): open a pywebview / WebView2 window
        owning the main thread. Closing the window initiates clean
        shutdown via the shared _shutdown_event.
     b. **Explicit headless**: env LN_TRANSLATOR_NO_WINDOW=1 — start
        the server but do NOT open a window and do NOT open a browser
        tab. Used by smoke tests and background-server scenarios driven
        from another tool.
     c. **pywebview-import-failed fallback**: degraded path when
        pywebview isn't installed or WebView2 isn't available. Falls
        back to webbrowser.open() so the user still has *something* to
        click; logs a warning.
  6. Handle Ctrl+C (SIGINT), SIGTERM, Win32 console-close, and window-
     close as four triggers into a single _shutdown_event funnel. uvicorn
     gets server.should_exit=True; FastAPI lifespan shutdown runs;
     queue workers cancel; process exits.

The console window is hidden in the frozen build (spec sets
console=False). With no console, stdout/stderr go nowhere, so any
startup-time diagnostic — uncaught exception, health-poll timeout,
port-find failure, pywebview import failure — is mirrored to
USER_DATA_ROOT/logs/startup.log so the user has something to send if
the EXE fails to come up.

To build the EXE:
    pyinstaller LN-Translator.spec

The frozen build then runs this module's main() as its entry point.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Windows: suppress console-window popup for every child subprocess.
#
# In the frozen EXE the parent has no console (spec sets console=False).
# Any child spawned without CREATE_NO_WINDOW (0x08000000) makes Windows
# allocate a fresh cmd window for it — a black box flashes up every time
# we shell out. Three known callers:
#   - claude_cli.Popen (the claude CLI subprocess)
#   - _subprocess_utils.kill_process_tree's taskkill (via claude_cli)
#   - claude_cli.probe_cli's `claude --version`
# Plus claude_agent_sdk internally uses anyio.open_process →
# asyncio.create_subprocess_exec, which has no creationflags hook in its
# public API. Patching subprocess.Popen.__init__ is the single point that
# covers all of them, including any future caller we don't know about.
# stdout/stderr piping is unaffected — CREATE_NO_WINDOW only suppresses
# the console-window allocation, so dev users running uvicorn from a
# terminal lose nothing.
if sys.platform == "win32":
    import subprocess

    _CREATE_NO_WINDOW = 0x08000000
    _orig_popen_init = subprocess.Popen.__init__

    def _popen_init_no_window(self, *args, **kwargs):
        if "creationflags" in kwargs and kwargs["creationflags"] is not None:
            kwargs["creationflags"] |= _CREATE_NO_WINDOW
        else:
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        _orig_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _popen_init_no_window


# Port-probe ordering. Start at 8765 (not 8000 — many other dev servers
# default there) and walk up. After this many failures, give up rather than
# probing the whole port space.
_PORT_PROBE_START = 8765
_PORT_PROBE_MAX_ATTEMPTS = 50

# Health-poll cadence. uvicorn typically comes up inside 200-500ms; cap
# the wait so a hung startup doesn't leave the user staring at a frozen
# window.
_HEALTH_POLL_INTERVAL_SECONDS = 0.15
_HEALTH_POLL_TIMEOUT_SECONDS = 20.0

# Env-var name that switches the entry point into explicit-headless mode:
# server boots, NO window opens, NO browser tab opens, sleep loop waits
# for a signal. Read inside main() rather than at import time so tests
# can monkey-patch os.environ before calling main().
_NO_WINDOW_ENV = "LN_TRANSLATOR_NO_WINDOW"


def _port_is_free(port: int) -> bool:
    """True iff we can bind 127.0.0.1:port right now. Used to decide
    whether to reuse the last-launch port from the sentinel."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _read_sentinel_port() -> int | None:
    """Read USER_DATA_ROOT/port.txt and return the integer port if the
    file is present, well-formed, and inside a reasonable range. Returns
    None on any failure so the caller falls back to the usual walk.

    The sentinel exists primarily so smoke harnesses can talk to the
    running instance, but reusing it across launches also keeps the
    URL origin stable, which is what makes localStorage / WebView2
    storage actually persist (per-origin keyed). Without this, a previous
    instance lingering on 8765 forces the next launch onto 8766, and
    every theme / font-size / scroll-position the user set is gone.
    """
    try:
        from backend.config import USER_DATA_ROOT
        sentinel = USER_DATA_ROOT / _PORT_SENTINEL_NAME
        if not sentinel.is_file():
            return None
        raw = sentinel.read_text(encoding="utf-8").strip()
        port = int(raw)
        if 1024 <= port <= 65535:
            return port
    except Exception:
        pass
    return None


def _find_free_port(start: int = _PORT_PROBE_START) -> int:
    """Return a localhost TCP port to bind on.

    When called with no explicit `start` (production path), order of
    preference is:
      1. The port from USER_DATA_ROOT/port.txt if it's currently free —
         keeps the URL origin stable across launches so per-origin
         storage (localStorage, WebView2 disk cache) persists.
      2. The historical default (`_PORT_PROBE_START`, 8765) if free.
      3. The first free port at 8766, 8767, ...

    When `start` is passed explicitly (tests, embedding), the sentinel
    is bypassed — caller wants a port at or above that specific start,
    not a magic-restored sentinel from elsewhere.

    Binds-and-closes to actually verify availability rather than trusting
    the sentinel. Loopback-only (127.0.0.1) — never bind 0.0.0.0 from
    the EXE.
    """
    if start == _PORT_PROBE_START:
        sentinel = _read_sentinel_port()
        if sentinel is not None and _port_is_free(sentinel):
            logger.info("reusing previous launch's port %d (sentinel)", sentinel)
            return sentinel
    for offset in range(_PORT_PROBE_MAX_ATTEMPTS):
        port = start + offset
        if _port_is_free(port):
            return port
    raise RuntimeError(
        f"could not find a free localhost port in {start}..{start + _PORT_PROBE_MAX_ATTEMPTS}"
    )


def _wait_for_health(port: int, timeout: float = _HEALTH_POLL_TIMEOUT_SECONDS) -> bool:
    """Poll /api/health until it returns 200 or the timeout fires.
    Returns True on success, False on timeout."""
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(_HEALTH_POLL_INTERVAL_SECONDS)
    return False


def _has_any_provider() -> bool:
    """True if the providers table has at least one row. Used as the
    fallback first-run signal when config_kv.first_run_complete isn't
    set (e.g. installs that predate Phase G). Direct sqlite query —
    avoids needing the FastAPI app already up for this read."""
    try:
        import sqlite3

        from backend.config import DB_PATH
        if not DB_PATH.exists():
            return False
        # Read-only connection so we don't accidentally write a journal
        # file before init_db has had a chance to set WAL mode.
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
            cur = conn.execute("SELECT 1 FROM providers LIMIT 1")
            return cur.fetchone() is not None
    except Exception:
        # Any failure (DB doesn't exist yet, providers table missing on
        # first run) → treat as "no providers" and fall through to the
        # onboarding route.
        return False


def _first_run_done_flag() -> bool | None:
    """Read config_kv.first_run_complete. Returns True when the user has
    walked through the onboarding wizard at least once, False when the
    key is explicitly "0", and None when the key isn't set (the
    pre-Phase-G default — caller falls back to the provider-count
    heuristic)."""
    try:
        import sqlite3

        from backend.config import DB_PATH
        if not DB_PATH.exists():
            return None
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
            cur = conn.execute(
                "SELECT value FROM config_kv WHERE key = 'first_run_complete'"
            )
            row = cur.fetchone()
            if row is None:
                return None
            return row[0] == "1"
    except Exception:
        return None


def _initial_url(port: int) -> str:
    """First-run-aware URL to land on. Phase G adds the /onboarding route
    as the dedicated first-run destination. Routing precedence:

    1. config_kv.first_run_complete='1'  → land on /  (returning user)
    2. config_kv.first_run_complete='0'  → /onboarding (user exited mid-
       wizard last session; pick up where they left off)
    3. key unset, any provider configured → /  (legacy install — they
       got there before Phase G, treat as returning)
    4. key unset, no provider configured  → /onboarding (genuine fresh
       install)
    """
    done = _first_run_done_flag()
    if done is True:
        path = "/"
    elif done is False:
        path = "/onboarding"
    elif _has_any_provider():
        path = "/"
    else:
        path = "/onboarding"
    return f"http://127.0.0.1:{port}{path}"


def _install_startup_log_handler() -> Path | None:
    """Mirror INFO+ log records to USER_DATA_ROOT/logs/startup.log so
    users have something to send when the frozen EXE (console=False)
    fails to come up. Best-effort: if we can't set this up (e.g.,
    permission denied), return None and continue with stderr-only
    logging — the show must still try to go on.

    Size-rotates at 1MB, keeps one rolled file. Idempotent so tests
    calling main() repeatedly don't stack duplicate handlers."""
    try:
        from backend.config import USER_DATA_ROOT
        log_dir = USER_DATA_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "startup.log"
    except Exception:
        return None

    root = logging.getLogger()
    # Guard against duplicate handlers if main() runs twice in one process.
    for h in root.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler) and getattr(
            h, "_ln_translator_startup_log", False
        ):
            return log_path

    try:
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=1_048_576, backupCount=1, encoding="utf-8",
        )
    except OSError:
        return None
    handler._ln_translator_startup_log = True  # type: ignore[attr-defined]
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    # uvicorn's default LOGGING_CONFIG would replace its loggers' handlers
    # after this point, dropping ours. We pass `log_config=None` to
    # uvicorn.Config (see _run_uvicorn) so uvicorn skips that dictConfig
    # and its loggers stay connected to root via standard propagation —
    # which means this single root handler captures everything.
    return log_path


_PORT_SENTINEL_NAME = "port.txt"


def _write_port_sentinel(port: int) -> Path | None:
    """Write the bound port to USER_DATA_ROOT/port.txt so out-of-process
    tools (smoke harnesses, CLI helpers) can talk to *this* EXE rather
    than guessing 8765 and racing a stale instance (Bug #7).

    Best-effort: if USER_DATA_ROOT isn't writable or the import fails,
    return None and continue — the file is a convenience, not load-bearing.
    The file is removed on clean shutdown via _remove_port_sentinel.
    """
    try:
        from backend.config import USER_DATA_ROOT
        USER_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        sentinel = USER_DATA_ROOT / _PORT_SENTINEL_NAME
        sentinel.write_text(str(port), encoding="utf-8")
        return sentinel
    except Exception as e:
        logger.warning("could not write port sentinel: %s", e)
        return None


def _remove_port_sentinel(path: Path | None) -> None:
    """Idempotent cleanup. Called from the shutdown tail in _main_inner."""
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _run_uvicorn(port: int) -> None:
    """Run uvicorn in the current thread, bound to 127.0.0.1:port. Called
    in a background thread by main(); main itself owns the GUI loop (or
    the headless sleep loop)."""
    import uvicorn

    from backend.main import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        # Disable access logs — they're noise. Real errors still surface.
        access_log=False,
        log_level="info",
        # log_config=None: skip uvicorn's default LOGGING_CONFIG dictConfig.
        # That default reconfigures the uvicorn/uvicorn.error/uvicorn.access
        # loggers fresh AFTER main() has already attached our RotatingFileHandler,
        # which silently drops our handler. With log_config=None, uvicorn's
        # loggers inherit from root — so our handler captures lifespan errors,
        # which is exactly the diagnostic the console=False frozen build needs.
        log_config=None,
    )
    server = uvicorn.Server(config)
    # Allow other code in main() to ask the server to stop cleanly.
    _server_ref["server"] = server
    server.run()


# Communication channel between the background uvicorn thread and the
# main() shutdown handler. Keyed dict so we can extend it later without
# changing the bridge shape.
_server_ref: dict = {}


def _signal_shutdown() -> None:
    """Tell the uvicorn server to exit. Triggers FastAPI's lifespan
    shutdown which cancels in-flight queue workers cleanly."""
    server = _server_ref.get("server")
    if server is not None:
        server.should_exit = True


# Win32 console control events. Names match the Windows API constants so
# anyone reading this can cross-reference docs.microsoft.com directly.
_CTRL_CLOSE_EVENT = 2
_CTRL_LOGOFF_EVENT = 5
_CTRL_SHUTDOWN_EVENT = 6
_CONSOLE_CLOSE_EVENTS = frozenset(
    {_CTRL_CLOSE_EVENT, _CTRL_LOGOFF_EVENT, _CTRL_SHUTDOWN_EVENT}
)

# Module-level reference to the active ctypes callback. The Win32 API
# stores only the C function pointer, so Python must keep the callable
# alive — otherwise GC reclaims it and the handler segfaults on the next
# console event. None on non-Windows platforms.
_WINDOWS_CTRL_HANDLER_REF = None


def _make_console_handler(on_close):
    """Pure-Python factory for the console-event dispatcher. Separated
    from _install_windows_console_handler so tests can call the handler
    function directly without setting up Win32 / ctypes machinery.

    Returns a function that takes an event_code (DWORD on Win32) and
    returns True iff the event was handled. CTRL_CLOSE / CTRL_LOGOFF /
    CTRL_SHUTDOWN route to `on_close(event_code)` and return True;
    CTRL_C / CTRL_BREAK return False so Python's signal module (which
    main() already wires SIGINT into) sees them."""
    def _handler(event_code):
        if event_code in _CONSOLE_CLOSE_EVENTS:
            try:
                on_close(event_code)
            except Exception:
                # The handler runs on a Windows-managed control-handler
                # thread; an exception here would crash the process
                # instead of letting the OS proceed with its own
                # shutdown sequence. Swallow.
                pass
            return True
        return False
    return _handler


def _install_windows_console_handler(on_close) -> bool:
    """Register a Win32 console control handler so closing the console
    window (CTRL_CLOSE_EVENT), logging off (CTRL_LOGOFF_EVENT), and OS
    shutdown (CTRL_SHUTDOWN_EVENT) trigger graceful shutdown instead of
    the OS's bare ~5s + SIGKILL.

    Returns True on success, False on any failure. Caller continues
    either way — the OS's default behavior is still safe, just less
    polite to in-flight chapters."""
    global _WINDOWS_CTRL_HANDLER_REF
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False
    HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    callback = HANDLER_ROUTINE(_make_console_handler(on_close))
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ok = bool(kernel32.SetConsoleCtrlHandler(callback, True))
    except OSError:
        return False
    if not ok:
        return False
    # Hold the reference at module scope — Win32 doesn't manage Python
    # GC, so a local var dropping out of scope would invalidate the
    # function pointer it gave the kernel.
    _WINDOWS_CTRL_HANDLER_REF = callback
    return True


def _try_import_pywebview():
    """Return the pywebview module on success, None on failure. Failure
    paths: package not installed, native dependency missing, etc. The
    caller logs the warning to startup.log and falls back to
    webbrowser.open()."""
    try:
        import webview  # noqa: F401  — confirms the package loads
        return webview
    except Exception as e:
        logger.warning("pywebview unavailable, falling back to browser tab: %s", e)
        return None


def _resolve_webview_storage_path() -> str | None:
    """Returns a stable on-disk path for WebView2's UserData folder,
    creating it if necessary. WebView2 stores localStorage, cookies,
    IndexedDB, the HTTP cache, and the favicon cache all under this dir.

    Without this AND `private_mode=False` (set in _run_window), pywebview
    opens WebView2 in private mode and discards every byte of storage on
    each launch — which is exactly why themes / font size / focus mode /
    "last read chapter" / etc. all reset every time the user opens the
    EXE.

    Best-effort: if USER_DATA_ROOT isn't writable, return None and let
    pywebview pick its default (storage still discarded on close in
    private mode, but the app keeps working).
    """
    try:
        from backend.config import USER_DATA_ROOT
        storage = USER_DATA_ROOT / "webview-data"
        storage.mkdir(parents=True, exist_ok=True)
        return str(storage)
    except Exception as e:
        logger.warning("could not prepare WebView2 storage dir: %s", e)
        return None


def _run_window(webview_mod, url: str, shutdown_event: threading.Event) -> None:
    """Open a pywebview window pointed at `url` and block until the
    window closes. Wires the window's `closing` event into the shared
    `shutdown_event` + `_signal_shutdown()` so window-close is one more
    trigger funnelling through the same shutdown path SIGINT uses.

    Runs on the main thread (pywebview / WebView2 requires it).

    Raises whatever `webview.create_window` / `webview.start` raise. On
    Windows, `import webview` succeeds even when the WebView2 Evergreen
    Runtime is absent — the failure surfaces here at `start()` time as
    a native error. Callers should catch broadly and degrade to a
    browser-tab fallback.

    Persistence: `private_mode=False` + a stable `storage_path` is what
    makes WebView2's storage (localStorage, cookies, IndexedDB, disk
    cache) actually survive a window close. pywebview's default is
    `private_mode=True`, which discards everything on close — and that
    was the root cause of "theme / font size / focus mode reset every
    time I open the app". Combined with the sentinel-based port
    stability in `_find_free_port`, this gives localStorage a stable
    origin AND a stable on-disk home, so per-origin state finally
    persists across launches.
    """
    window = webview_mod.create_window(
        "LN-Translator",
        url=url,
        width=1280,
        height=900,
        resizable=True,
        confirm_close=False,
    )

    def _on_window_closing():
        # Single shared funnel: set the event AND signal uvicorn.
        # Idempotent against repeated fires (event.set() is a no-op the
        # second time; server.should_exit=True is too).
        if not shutdown_event.is_set():
            shutdown_event.set()
        _signal_shutdown()

    # pywebview 5.x/6.x events API: callable subscription via `+=`.
    try:
        window.events.closing += _on_window_closing
    except Exception as e:
        # Defensive — different pywebview versions wire events
        # differently. If subscription fails, window-close still
        # eventually drops to the post-loop _signal_shutdown() because
        # webview.start() returns when the window closes. Include the
        # exception type/message so the warning in startup.log is
        # actionable rather than just "something failed" (Bug #6).
        logger.warning(
            "could not subscribe to window.events.closing (%s: %s); "
            "shutdown will still fire via post-start finally",
            type(e).__name__, e,
        )

    storage_path = _resolve_webview_storage_path()
    start_kwargs: dict = {"debug": False, "private_mode": False}
    if storage_path is not None:
        start_kwargs["storage_path"] = storage_path
        logger.info("WebView2 storage_path = %s", storage_path)
    else:
        logger.warning(
            "WebView2 storage_path could not be set; storage will use "
            "pywebview's default location (may not persist as expected)."
        )

    # Blocks on the main thread until the last window closes.
    try:
        webview_mod.start(**start_kwargs)
    except TypeError:
        # Older pywebview versions that don't know storage_path /
        # private_mode kwargs. Fall back to the bare-bones call so the
        # EXE still launches — the persistence fix is lost, but the app
        # is usable.
        logger.warning(
            "pywebview.start does not accept storage_path/private_mode; "
            "falling back to defaults — settings may not persist on this "
            "version of pywebview."
        )
        webview_mod.start(debug=False)


def _run_browser_fallback(
    url: str,
    shutdown_event: threading.Event,
    uvicorn_thread: threading.Thread,
    *,
    reason: str,
) -> None:
    """Degraded UI surface: open the URL in the user's default browser
    and sleep-loop until shutdown. Shared between the
    pywebview-import-failed path and the pywebview-start-failed path
    (Bug #2: import succeeds on Windows even without the WebView2
    runtime — the failure is at start() time, not import time)."""
    print(
        f"WARNING: pywebview unavailable ({reason}) — falling back to browser tab. "
        "Install the WebView2 Runtime from Microsoft for the native window experience."
    )
    logger.warning("pywebview fallback engaged: %s", reason)
    try:
        webbrowser.open(url, new=2)
    except Exception as e:
        logger.warning("webbrowser.open failed: %s", e)
        print(f"\nApp is running at {url} — open it in your browser.\n")
    print("Press Ctrl+C to quit cleanly.")
    try:
        while uvicorn_thread.is_alive() and not shutdown_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        # Outer caller's signal handler runs first; this is belt-and-suspenders.
        shutdown_event.set()
        _signal_shutdown()


def main() -> int:
    """Entry point. Returns a process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Install the startup-log handler EARLY so failures during port
    # finding / uvicorn boot land in the file. Returns the log path so
    # we can mention it in error messages.
    startup_log_path = _install_startup_log_handler()

    try:
        return _main_inner(startup_log_path)
    except SystemExit:
        # Honor explicit sys.exit() calls without wrapping their code.
        raise
    except BaseException:
        # Capture-and-log any uncaught exception before re-raising. With
        # console=False in the frozen build, an uncaught exception
        # crashes the process silently; the user has no diagnostic
        # surface other than startup.log.
        logger.critical("unhandled startup error:\n%s", traceback.format_exc())
        if startup_log_path is not None:
            try:
                print(
                    f"Fatal startup error — see {startup_log_path}",
                    file=sys.stderr,
                )
            except Exception:
                pass
        raise


def _main_inner(startup_log_path: Path | None) -> int:
    """Actual entry point body. Split out so main() can wrap it in a
    try/except for startup-log capture without duplicating the body."""
    try:
        port = _find_free_port()
    except RuntimeError as e:
        logger.error("port-find failure: %s", e)
        if startup_log_path is not None:
            print(f"ERROR: {e}\nSee {startup_log_path} for details.", file=sys.stderr)
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"LN-Translator starting on http://127.0.0.1:{port}")
    logger.info("uvicorn starting on http://127.0.0.1:%d", port)

    # Sentinel so smoke harnesses can read this process's bound port
    # instead of walking 8765..8814 and racing any stale instance.
    # Kept across shutdown (next launch reuses it when free) so the URL
    # origin stays stable and WebView2 localStorage persists. The return
    # value is unused — the side effect (writing the file) is the
    # whole point.
    _write_port_sentinel(port)

    # uvicorn runs in a daemon thread so main() can own the GUI loop (or
    # the headless sleep loop) and signal shutdown. A daemon thread also
    # means a hung uvicorn doesn't keep the process alive after main()
    # returns.
    uvicorn_thread = threading.Thread(
        target=_run_uvicorn, args=(port,), daemon=True, name="uvicorn",
    )
    uvicorn_thread.start()

    if not _wait_for_health(port):
        msg = (
            f"server did not become healthy within "
            f"{_HEALTH_POLL_TIMEOUT_SECONDS:.0f}s"
        )
        logger.error("health-poll timeout: %s", msg)
        if startup_log_path is not None:
            print(
                f"ERROR: {msg}. Check {startup_log_path} for details.",
                file=sys.stderr,
            )
        else:
            print(f"ERROR: {msg}. Check the log above.", file=sys.stderr)
        _signal_shutdown()
        uvicorn_thread.join(timeout=5.0)
        return 1

    # The single shared shutdown event. Set by SIGINT/SIGTERM handler,
    # by Win32 console-close handler, by pywebview's window-closing
    # event, AND read by the headless-mode sleep loop. One funnel.
    shutdown_event = threading.Event()

    def _on_signal(signum, _frame):
        if not shutdown_event.is_set():
            shutdown_event.set()
            print(f"\nReceived signal {signum}, shutting down…")
            _signal_shutdown()

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _on_signal)
        except (ValueError, OSError):
            # Some embedded Python contexts forbid signal.signal off
            # the main thread.
            pass
    _install_windows_console_handler(lambda _code: _on_signal(_code, None))

    url = _initial_url(port)
    headless = os.getenv(_NO_WINDOW_ENV, "").strip() == "1"

    if headless:
        # Explicit headless: server is up, but we do NOT open a window
        # and we do NOT open a browser tab. Caller (smoke test) is
        # responsible for driving the HTTP surface.
        logger.info("LN_TRANSLATOR_NO_WINDOW=1 → headless mode (no window, no browser tab)")
        print(
            "Headless mode — server is running at "
            f"{url}. Press Ctrl+C to quit."
        )
        try:
            while uvicorn_thread.is_alive() and not shutdown_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            _on_signal(signal.SIGINT, None)
    else:
        webview_mod = _try_import_pywebview()
        if webview_mod is None:
            _run_browser_fallback(
                url, shutdown_event, uvicorn_thread,
                reason="pywebview module failed to import",
            )
        else:
            # Default: native window owns the main thread.
            print(
                "Native window opening — close the window to quit. "
                "(Closing mid-translation leaves the chapter in "
                "'translating' state; it auto-recovers on next launch.)"
            )
            window_started_ok = False
            try:
                _run_window(webview_mod, url, shutdown_event)
                window_started_ok = True
            except Exception as e:
                # Bug #2: `import webview` succeeds on Windows without the
                # WebView2 runtime; the failure surfaces here. Fall back to
                # the same browser-tab path that handles import-fail rather
                # than letting the EXE silently exit.
                logger.exception("pywebview.start() failed; engaging browser fallback")
                if not shutdown_event.is_set():
                    _run_browser_fallback(
                        url, shutdown_event, uvicorn_thread,
                        reason=f"webview.start() raised: {e!s}",
                    )
            finally:
                # Window returned (closed by user, crashed-and-fell-back, or
                # webview.start exited). Funnel into shutdown.
                if window_started_ok and not shutdown_event.is_set():
                    shutdown_event.set()

    # Common shutdown tail — every path above ends here.
    _signal_shutdown()
    # NB: we deliberately do NOT remove the port sentinel on shutdown.
    # The next launch reuses it (when free) so the URL origin stays
    # stable across launches — that's what makes WebView2 localStorage
    # actually persist. _read_sentinel_port validates the port is free
    # before reusing it, so a stale entry from a crashed instance is
    # safe; a stale entry from a still-running instance falls through
    # to the next-port walk.
    uvicorn_thread.join(timeout=10.0)
    if uvicorn_thread.is_alive():
        print(
            "WARNING: server did not exit within 10s — there may be an "
            "orphan child process.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
