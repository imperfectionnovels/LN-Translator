"""Port selection, the launch-port sentinel, and the health poll.

Extracted from app_entry so the entry-point module stays a thin
orchestrator. These helpers are pure utilities: they import
backend.config lazily (inside each function) so importing this module
has no side effect and tests can monkeypatch USER_DATA_ROOT / DB_PATH
freely.

This module must NOT import backend.app_entry (would create an import
cycle). app_entry re-exports every public name here so existing
`backend.app_entry._find_free_port` style references keep working.
"""

from __future__ import annotations

import logging
import socket
import time
import urllib.request
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)


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

_PORT_SENTINEL_NAME = "port.txt"


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
