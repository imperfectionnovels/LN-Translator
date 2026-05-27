"""Shared subprocess plumbing for CLI-backed translators.

Each CLI translator (claude_cli, codex_cli, gemini_cli, opencode) spawns an
external binary, writes a prompt, captures stdout. The Windows / cancellation
/ process-tree-kill quirks are identical across them, so they live here.

Per-CLI envelope parsing, rate-limit classification, and the specific argv
shape stay inside each translator file. This helper is intentionally narrow:
exec safely, drain pipes, kill the tree on cancel.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)


def resolve_binary(binary: str) -> str:
    """Look up `binary` on PATH. On Windows the CLI shims installed by npm
    (`codex.CMD`, `gemini.CMD`, `opencode.CMD`) need PATHEXT resolution that
    Python's CreateProcess does not apply directly — `shutil.which` handles
    it. Returns the resolved path if found, otherwise the input verbatim so
    the caller's downstream FileNotFoundError surfaces with a clear path.

    Not cached so a freshly-installed CLI is picked up without restarting
    the server."""
    resolved = shutil.which(binary)
    return resolved or binary


def build_argv(args: list[str]) -> list[str]:
    """Wrap a CLI invocation through `cmd /c` when the resolved binary is a
    Windows batch file (`.cmd` / `.bat`). Python 3.13 tightened subprocess
    so `.cmd` / `.bat` files can no longer be executed directly without
    `shell=True`; the npm shims are all `.CMD`, so the direct call raises
    OSError on 3.13+. Wrapping with `cmd /c` works on every Python version
    and avoids the shell-injection risk of `shell=True`.
    """
    if not args:
        return args
    if os.name != "nt":
        return args
    head = args[0]
    if head.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", *args]
    return args


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill the process AND all its descendants. On Windows the CLI shim
    runs cmd.exe → <binary>.CMD → node.exe / python.exe; `proc.kill()` ends
    only the cmd.exe shim, orphaning the grandchild — which keeps consuming
    subscription quota and (worse) hogs the model rate-limit window. POSIX
    has no shim, so a direct kill is enough.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        proc.kill()


async def run_subprocess(
    args: list[str],
    *,
    stdin_text: str | None,
    timeout_seconds: float,
) -> tuple[int, str, str]:
    """Spawn `args`, optionally pipe `stdin_text`, wait up to
    `timeout_seconds`, and return (returncode, stdout, stderr) as strings.

    Cancellation- and timeout-safe: on any exception in the wait phase we
    kill the entire process tree and drain the pipes before re-raising, so
    the OS releases the pipe handles and the CLI doesn't keep eating
    subscription quota for a chapter nobody is reading anymore.

    Uses `subprocess.Popen` + `asyncio.to_thread(proc.communicate)` rather
    than `asyncio.create_subprocess_exec` because the latter raises
    NotImplementedError under uvicorn's default Windows Selector event loop
    policy. The Popen route works on every Python/uvicorn/Windows combo.
    """
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdin_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
        stdout_bytes, stderr_bytes = await asyncio.to_thread(
            proc.communicate, stdin_bytes, timeout_seconds,
        )
    except BaseException:
        # CancelledError, TimeoutExpired, anything else — kill the tree and
        # drain so the OS releases the pipe handles. `wait()` alone does NOT
        # drain pipes and can deadlock when the child has already filled its
        # stdout buffer.
        kill_process_tree(proc)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("CLI subprocess did not exit within 5s of kill()")
        except Exception as drain_exc:
            logger.warning("CLI subprocess cleanup-drain failed: %s", drain_exc)
        raise

    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    return proc.returncode, stdout, stderr


async def probe_binary(binary: str, version_args: list[str], *, timeout: float = 20.0) -> str:
    """Run `<binary> <version_args...>` and return the version string. Raises
    `RuntimeError` with installation guidance when the binary is missing or
    the command fails. Used by `main.py::_probe_one` to fail fast at boot
    rather than on the first translate request."""
    path = resolve_binary(binary)
    argv = build_argv([path, *version_args])

    def _run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    try:
        completed = await asyncio.to_thread(_run)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        raise RuntimeError(
            f"{binary} CLI not found at {path!r}. Install the CLI and ensure "
            "it is on PATH; see the provider's catalog entry for install hints."
        ) from e
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"{binary} CLI failed `{' '.join(version_args)}` "
            f"(exit {completed.returncode}): {stderr[:200]}."
        )
    return completed.stdout.decode("utf-8", errors="replace").strip()
