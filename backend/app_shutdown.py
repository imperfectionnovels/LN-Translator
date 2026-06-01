"""Shutdown bridge between the background uvicorn thread and every
shutdown trigger (SIGINT/SIGTERM, Win32 console-close, window-close,
headless sleep loop).

Pulled into its own tiny module so both app_entry (which owns
_run_uvicorn and main()) and app_ui (which owns the window / browser
surfaces) can share the single server reference without an import
cycle. app_ui imports _signal_shutdown from here; app_entry re-exports
both names so `backend.app_entry._server_ref` /
`backend.app_entry._signal_shutdown` keep working for callers and tests.

_server_ref is a module-level dict, deliberately mutable: every consumer
mutates the same object in place (`_server_ref["server"] = ...`,
`_server_ref.clear()`, `_server_ref.get("server")`), so re-importing the
name elsewhere stays consistent with this module's view.
"""

from __future__ import annotations

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
