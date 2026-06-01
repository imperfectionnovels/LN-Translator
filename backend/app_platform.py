"""Windows platform shims: child-subprocess console suppression and the
Win32 console-control handler.

Extracted from app_entry so the entry-point module stays a thin
orchestrator. Both helpers are no-ops off Windows / outside the frozen
build, and neither has an import-time side effect, so tests import this
module freely.

This module must NOT import backend.app_entry (would create an import
cycle). The console handler takes its shutdown callback as a parameter
(`on_close`), so it never needs to reach back into the entry point.
app_entry re-exports every public name here so existing
`backend.app_entry._make_console_handler` style references keep working.
"""

from __future__ import annotations

import logging
import sys

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
_CREATE_NO_WINDOW = 0x08000000


def _install_no_window_subprocess_patch() -> None:
    """Apply the win32 CREATE_NO_WINDOW Popen patch described above.

    Called from main(), NOT at import, so merely importing this module has no
    process-global side effect (tests import it freely). Gated behind the
    frozen build: a dev run from a terminal already has a console and does not
    need the patch. Idempotent, so a harness that calls main() twice in one
    process does not stack patches.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    import subprocess

    if getattr(subprocess.Popen.__init__, "_ln_no_window", False):
        return
    orig_popen_init = subprocess.Popen.__init__

    def _popen_init_no_window(self, *args, **kwargs):
        if kwargs.get("creationflags") is not None:
            kwargs["creationflags"] |= _CREATE_NO_WINDOW
        else:
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        orig_popen_init(self, *args, **kwargs)

    _popen_init_no_window._ln_no_window = True  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = _popen_init_no_window


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
