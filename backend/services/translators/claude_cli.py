"""Claude Code CLI-backed translator.

Shells out to the local `claude` binary (the Claude Code CLI) so translations
run on the user's Claude subscription instead of a paid API key. No HTTP
client; we spawn a subprocess per chapter, write the prompt to stdin, and read
a JSON envelope from stdout.

Subprocess invocation:
    claude -p --output-format json --max-turns 1 [--model <name>]

- `-p`: print mode, non-interactive (with no positional prompt, reads stdin).
- `--output-format json`: returns one JSON envelope with `result` field.
- `--max-turns 1`: forces a single response, no tool-using loops.
- Prompt is piped on stdin to dodge Windows' ~32 KB command-line cap.

Subprocesses run via `subprocess.Popen` + `asyncio.to_thread(proc.communicate)`
rather than `asyncio.create_subprocess_exec`. Uvicorn defaults to the Windows
Selector event loop policy on Windows, which raises NotImplementedError on
asyncio subprocess calls. The Popen route works on every Python/uvicorn/Windows
combination and lets us kill the child if the asyncio task is cancelled — so a
user closing a bulk upload halfway through doesn't leave an orphan `claude`
process eating subscription quota.

Concurrency is forced serial via an instance Semaphore(1) and an exported
`max_parallel = 1` that the route layer also respects, so bulk uploads queue
behind the semaphore rather than racing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess

from backend.config import CLAUDE_CLI_PATH, CLAUDE_CLI_TRANSLATOR_MODEL
from backend.services.providers import Provider

from ._claude_errors import classify as _classify_cli_error
from .base import (
    BACKOFF_SCHEDULE,
    BaseTranslator,
    TransientTranslatorError,
)

logger = logging.getLogger(__name__)

# Hard ceiling per chapter. Cultivation chapters are usually 2-5k Chinese chars
# and complete in 30-90 seconds, but the first call after a session warmup or
# a rate-limit retry can take much longer.
_CALL_TIMEOUT_SECONDS = 600.0

# `claude --version` via the npm shim → node startup can take 5-10s on a cold
# disk. Give it real headroom so a freshly-rebooted machine doesn't fail the
# startup probe and force a manual retry.
_PROBE_TIMEOUT_SECONDS = 20.0


class ClaudeCliError(Exception):
    """Subprocess returned an error envelope or non-zero exit code with no
    retryable signal. Surfaced to the caller as a hard failure."""


def _resolve_cli_path() -> str:
    """Resolve the CLI binary path. On Windows the `claude` shim installed by
    npm is `claude.CMD`; Python's CreateProcess does not apply PATHEXT, so we
    have to look it up via shutil.which (which does). Falls back to the raw
    configured value so probe_cli can produce a clear "not found" error.

    Not cached — shutil.which is cheap (microseconds) and not caching means a
    fresh install of the CLI is picked up without restarting the server."""
    resolved = shutil.which(CLAUDE_CLI_PATH)
    return resolved or CLAUDE_CLI_PATH


def _build_cli_argv(args: list[str]) -> list[str]:
    """Wrap a CLI invocation through `cmd /c` when the resolved binary is a
    Windows batch file (`.cmd` / `.bat`). Python 3.13 tightened subprocess so
    `.cmd` / `.bat` files can no longer be executed directly without
    `shell=True`; the npm `claude` shim is `claude.CMD`, so the direct call
    raises OSError on 3.13+. Wrapping with `cmd /c` works on every Python
    version and avoids the shell-injection risk of `shell=True`."""
    if not args:
        return args
    if os.name != "nt":
        return args
    head = args[0]
    if head.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", *args]
    return args


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill the process AND all its descendants. On Windows the CLI runs as
    cmd.exe → claude.CMD → node.exe; `proc.kill()` ends only the cmd.exe shim,
    orphaning the grandchild `node` — which keeps eating subscription quota.
    `taskkill /T` walks the whole tree. POSIX has no such shim, so `kill()`
    on the direct child is enough there."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        proc.kill()


class ClaudeCliTranslator(BaseTranslator):
    name = "claude_cli"
    model_id = CLAUDE_CLI_TRANSLATOR_MODEL or "default"
    max_parallel = 1

    def __init__(self, provider: Provider | None = None) -> None:
        # claude_cli auth is the local Claude install's subscription — no
        # API key to thread through. Provider only contributes model_id.
        if provider is not None and provider.model_id:
            self.model_id = provider.model_id
        self._semaphore = asyncio.Semaphore(1)

    async def _complete(self, prompt: str) -> str:
        # CLI has no separate system slot — prepend with explicit markers.
        full = (
            f"SYSTEM INSTRUCTIONS:\n{self.system_instruction}\n\n"
            f"USER REQUEST:\n{prompt}"
        )
        return await self._call_cli(full)

    async def _complete_plain(self, prompt: str) -> str:
        return await self._call_cli(prompt)

    async def _call_cli(self, prompt: str) -> str:
        async with self._semaphore:
            return await self._call_cli_with_retry(prompt)

    async def _call_cli_with_retry(self, prompt: str) -> str:
        last_exc: BaseException | None = None
        for attempt in range(len(BACKOFF_SCHEDULE) + 1):
            try:
                return await self._run_subprocess(prompt)
            except (ClaudeCliError, TransientTranslatorError):
                # Both are classified errors. ClaudeCliError = non-retryable
                # (bad model name, auth, etc.). TransientTranslatorError =
                # already-classified rate-limit; retrying inside the 5-hour
                # cap won't help, so surface immediately and let the user
                # hit /start later to reset and re-queue.
                raise
            except (asyncio.TimeoutError, subprocess.TimeoutExpired, ConnectionError, OSError) as e:
                last_exc = e
                if attempt >= len(BACKOFF_SCHEDULE):
                    break
                delay = BACKOFF_SCHEDULE[attempt]
                logger.warning(
                    "Claude CLI transient error (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    len(BACKOFF_SCHEDULE) + 1,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
        raise TransientTranslatorError(
            "Claude CLI temporarily unavailable. The chapter is unchanged — "
            "try Retranslate later."
        ) from last_exc

    async def _run_subprocess(self, prompt: str) -> str:
        args = [
            _resolve_cli_path(),
            "-p",
            "--output-format", "json",
            "--max-turns", "1",
        ]
        if self.model_id and self.model_id != "default":
            args.extend(["--model", self.model_id])
        args = _build_cli_argv(args)

        # Popen returns immediately. We then wait for I/O in a worker thread
        # via asyncio.to_thread so the event loop stays unblocked. If the
        # asyncio task gets cancelled (e.g. server shutdown, user navigates
        # away), CancelledError propagates through the await, we fall into
        # the except clause and kill the child so it stops eating quota.
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.to_thread(
                proc.communicate, prompt.encode("utf-8"), _CALL_TIMEOUT_SECONDS,
            )
        except BaseException:
            # CancelledError, TimeoutExpired, anything else — leaving the child
            # alive would burn subscription quota with no consumer for the
            # output. Kill the whole tree (the npm shim spawns a node grandchild
            # that proc.kill() alone would orphan), then drain the pipes via
            # communicate() so the OS pipe handles are released. `wait()` does
            # NOT drain pipes — it leaves stdout/stderr handles referenced and
            # can deadlock if the child has already filled its stdout buffer
            # before being killed.
            _kill_process_tree(proc)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("claude CLI did not exit within 5s of kill()")
            except Exception as drain_exc:
                logger.warning("claude CLI cleanup-drain failed: %s", drain_exc)
            raise

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            return self._handle_nonzero_exit(proc.returncode, stdout, stderr)

        # Envelope shape: {"type": "result", "result": "<model text>",
        #                  "is_error": false, "subtype": "success", ...}
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as e:
            logger.error("claude CLI returned non-JSON stdout: %s", stdout[:500])
            raise ClaudeCliError(
                f"claude CLI returned non-JSON stdout: {e}"
            ) from e

        if envelope.get("is_error"):
            err_text = str(envelope.get("result") or envelope)
            # HTTP 429 from the upstream API is unambiguously a rate limit, even
            # if the wording in `result` doesn't match any known pattern (e.g.
            # "You've hit your limit · resets 10:30pm").
            if envelope.get("api_error_status") == 429:
                logger.warning("claude CLI rate-limited (429): %s", err_text[:300])
                raise TransientTranslatorError(
                    "Claude subscription rate limit hit. Wait for your 5-hour "
                    "window to reset, then click Start to resume. "
                    f"CLI message: {err_text.strip()[:300]}"
                )
            self._raise_classified(err_text, exit_code=None)

        result = envelope.get("result")
        if not isinstance(result, str):
            raise ClaudeCliError(
                f"claude CLI envelope missing 'result' string: {envelope}"
            )
        # The CLI's JSON envelope includes a `usage` sub-object on success
        # ({input_tokens, output_tokens, cache_read_input_tokens, ...}).
        # Plumb it into the BaseTranslator accumulator. Older CLI versions
        # omit usage entirely — we treat that as 0 tokens (the cost
        # calculator's "unknown" branch).
        usage = envelope.get("usage")
        if isinstance(usage, dict):
            self._emit_usage(
                input_tokens=usage.get("input_tokens") or 0,
                output_tokens=usage.get("output_tokens") or 0,
                cached_input_tokens=usage.get("cache_read_input_tokens") or 0,
            )
        return result

    def _handle_nonzero_exit(self, exit_code: int, stdout: str, stderr: str) -> str:
        """Non-zero exit always raises; the return type is just there so the
        call site can `return self._handle_nonzero_exit(...)` for flow clarity."""
        combined = f"{stderr}\n{stdout}"
        self._raise_classified(combined, exit_code=exit_code)
        raise AssertionError("unreachable — _raise_classified always raises")

    def _raise_classified(self, text: str, exit_code: int | None) -> None:
        cls = _classify_cli_error(text)
        snippet = text.strip()[:300]
        if cls == "rate_limit":
            logger.warning("claude CLI rate-limited: %s", snippet)
            raise TransientTranslatorError(
                "Claude subscription rate limit hit. Wait for your 5-hour "
                "window to reset, then click Start to resume. "
                f"CLI message: {snippet}"
            )
        if cls == "auth":
            logger.error("claude CLI auth failure: %s", snippet)
            raise ClaudeCliError(
                "Claude CLI is not authenticated. Run `claude` in a terminal "
                f"to log in, then restart the server. CLI message: {snippet}"
            )
        if exit_code is None:
            raise ClaudeCliError(f"claude CLI returned error envelope: {snippet}")
        raise ClaudeCliError(f"claude CLI failed (exit {exit_code}): {snippet}")


async def probe_cli() -> None:
    """Run `claude --version` once at startup to confirm the binary is
    installed and accessible. Raises RuntimeError with installation guidance
    on failure. We don't probe auth here because that would require a real
    API call (subscription quota), and `--reload` dev cycles would burn it
    on every save. Auth failures surface with a clear message on the first
    translate request instead."""
    path = _resolve_cli_path()

    argv = _build_cli_argv([path, "--version"])

    def _run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            argv,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )

    try:
        completed = await asyncio.to_thread(_run)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        raise RuntimeError(
            f"Claude CLI not found at '{path}' (configured: {CLAUDE_CLI_PATH!r}). "
            "Install Claude Code (https://docs.claude.com/claude-code) and run "
            "`claude` once to log in, or set TRANSLATOR_BACKEND=gemini in .env "
            "to use the API backend."
        ) from e
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Claude CLI at '{path}' failed `--version` "
            f"(exit {completed.returncode}): {stderr[:200]}. Run `claude` once to "
            "log in, or set TRANSLATOR_BACKEND=gemini."
        )
    version = completed.stdout.decode("utf-8", errors="replace").strip()
    logger.info("Claude CLI detected at %s: %s", path, version)
