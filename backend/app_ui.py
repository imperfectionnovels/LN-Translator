"""UI-surface selection: startup-log handler, pywebview window, and the
browser-tab fallback.

Extracted from app_entry so the entry-point module stays a thin
orchestrator. None of these helpers has an import-time side effect, so
tests import this module freely.

This module must NOT import backend.app_entry (would create an import
cycle). The two surface-runners (`_run_window`, `_run_browser_fallback`)
need to signal uvicorn shutdown when the window / browser loop ends.
That bridge (`_signal_shutdown`) lives in backend.app_shutdown, which
both this module and app_entry import — no cycle, single source of
truth for the server reference.

app_entry re-exports every public name here so existing
`backend.app_entry._run_window` style references keep working. Note
test_app_entry_pywebview_fallback monkeypatches
`backend.app_entry.webbrowser.open`; because `webbrowser` is the same
module object whether reached via app_entry or app_ui, the patch is
visible to `_run_browser_fallback` here regardless.
"""

from __future__ import annotations

import logging
import logging.handlers
import threading
import time
import webbrowser
from pathlib import Path

from backend.app_shutdown import _signal_shutdown

logger = logging.getLogger(__name__)


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
    # Enable file downloads. pywebview defaults webview.settings['ALLOW_DOWNLOADS']
    # to False, and the WebView2 backend's DownloadStarting handler hard-cancels
    # every download when it is false (args.Cancel = True). That silently broke
    # the reader's "Whole novel .txt / .md / .epub" links AND the glossary CSV/MD
    # exports inside the packaged window: the server returns 200 with a proper
    # Content-Disposition attachment, but the webview throws the bytes away, so a
    # plain browser downloaded fine while the EXE did nothing. Mutate the key in
    # place (do NOT rebind `settings` to a new dict — the backend imported the
    # ImmutableDict by reference, so a rebind would leave it reading the old
    # object). With it on, WebView2 raises a native Save As dialog. Guarded so a
    # pywebview build without the key still launches.
    try:
        webview_mod.settings["ALLOW_DOWNLOADS"] = True
    except Exception as e:  # noqa: BLE001 - never let a settings shape block launch
        logger.warning(
            "could not enable webview downloads (%s: %s); in-app file downloads "
            "may be blocked by WebView2",
            type(e).__name__, e,
        )

    window = webview_mod.create_window(
        "LN-Translator",
        url=url,
        width=1280,
        height=900,
        resizable=True,
        confirm_close=False,
        # pywebview defaults text_select=False, which injects
        # `body { user-select: none; cursor: default }` into the page at
        # runtime (see pywebview's js/customize.js). That kills native text
        # selection app-wide in the WebView2 window, so users cannot drag
        # to highlight chapter text or settings, only inside form inputs.
        # A plain browser never gets that injection, so selection looked fine
        # in dev and only the packaged app was affected. Set True so the
        # whole app is selectable / copyable like any normal window.
        text_select=True,
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
