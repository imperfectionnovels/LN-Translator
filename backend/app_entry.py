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

Module layout note: the cohesive pieces of this entry point live in
sibling modules, all imported (and thus bundled by PyInstaller) here:
  - backend.app_ports     — port probe, launch-port sentinel, health poll.
  - backend.app_platform  — Windows console suppression + console-control handler.
  - backend.app_ui        — startup-log handler, pywebview window, browser fallback.
  - backend.app_shutdown  — the uvicorn server reference + _signal_shutdown bridge.
This module re-exports each moved name so existing
`backend.app_entry.<name>` references (and the test suite) keep working,
and keeps only first-run routing, _run_uvicorn, and main()/_main_inner
as the thin orchestrator.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
import traceback
import webbrowser  # noqa: F401 — kept importable so tests can monkeypatch app_entry.webbrowser
from pathlib import Path

# --- Re-exported helpers (moved into sibling modules) -----------------------
# These imports both wire the orchestrator below AND keep every
# `backend.app_entry.<name>` reference working for callers and tests.
from backend.app_platform import (  # noqa: F401  (re-exported for tests/callers)
    _CONSOLE_CLOSE_EVENTS,
    _CREATE_NO_WINDOW,
    _CTRL_CLOSE_EVENT,
    _CTRL_LOGOFF_EVENT,
    _CTRL_SHUTDOWN_EVENT,
    _install_no_window_subprocess_patch,
    _install_windows_console_handler,
    _make_console_handler,
)
from backend.app_ports import (  # noqa: F401  (re-exported for tests/callers)
    _HEALTH_POLL_INTERVAL_SECONDS,
    _HEALTH_POLL_TIMEOUT_SECONDS,
    _PORT_PROBE_MAX_ATTEMPTS,
    _PORT_PROBE_START,
    _PORT_SENTINEL_NAME,
    _find_free_port,
    _port_is_free,
    _read_sentinel_port,
    _remove_port_sentinel,
    _wait_for_health,
    _write_port_sentinel,
)
from backend.app_shutdown import _server_ref, _signal_shutdown
from backend.app_ui import (  # noqa: F401  (re-exported for tests/callers)
    _install_startup_log_handler,
    _resolve_webview_storage_path,
    _run_browser_fallback,
    _run_window,
    _try_import_pywebview,
)

logger = logging.getLogger(__name__)


# Env-var name that switches the entry point into explicit-headless mode:
# server boots, NO window opens, NO browser tab opens, sleep loop waits
# for a signal. Read inside main() rather than at import time so tests
# can monkey-patch os.environ before calling main().
_NO_WINDOW_ENV = "LN_TRANSLATOR_NO_WINDOW"


def __getattr__(name: str):
    """Forward reads of _WINDOWS_CTRL_HANDLER_REF to the live value in
    backend.app_platform.

    _install_windows_console_handler rebinds that module-global on
    success; a plain `from ... import _WINDOWS_CTRL_HANDLER_REF` would
    snapshot the initial None and never see the update. test_phase6's
    Windows registration test reads `app_entry._WINDOWS_CTRL_HANDLER_REF`
    and expects the post-registration value, so resolve it dynamically.
    """
    if name == "_WINDOWS_CTRL_HANDLER_REF":
        from backend import app_platform
        return app_platform._WINDOWS_CTRL_HANDLER_REF
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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


def main() -> int:
    """Entry point. Returns a process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Frozen-EXE only: suppress the console-window flash on child subprocesses.
    # Applied here rather than at import so importing this module is side-effect
    # -free. Must run before uvicorn (and thus the queue worker) spawns anything.
    _install_no_window_subprocess_patch()
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
