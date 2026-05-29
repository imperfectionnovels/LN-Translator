"""OpenAI Codex CLI translator.

Shells out to the local `codex` binary so translations run on the user's
ChatGPT Plus / Pro / Team subscription instead of a paid API key. The
subprocess is invoked non-interactively via `codex exec`, with the prompt
piped on stdin.

Subprocess invocation:
    codex exec --model <model_id> --output-last-message <tmpfile>

`exec` is Codex's non-interactive entry point — it runs the request once,
prints the assistant output to stdout, and exits. We pipe the prompt on
stdin to dodge Windows' ~32 KB command-line cap. The `--model` flag selects
between gpt-5 / gpt-5-codex / o3 / etc.

Note: Codex CLI is framed as a *coding* agent, so the output may carry
some agent-style framing. For best translation results, use the direct
`openai` API provider type instead. This wrapper is for users who want to
burn their ChatGPT subscription instead of API credits.
"""

from __future__ import annotations

import asyncio
import logging

from backend.services.providers import Provider

from ._subprocess_utils import build_argv, resolve_binary, run_subprocess
from .base import (
    BACKOFF_SCHEDULE,
    BaseTranslator,
    TransientTranslatorError,
)

logger = logging.getLogger(__name__)

_BINARY = "codex"
_CALL_TIMEOUT = 600.0


class CodexCliError(Exception):
    """Hard failure from the Codex CLI — bad model name, auth missing, etc."""


class CodexCliTranslator(BaseTranslator):
    name = "codex_cli"
    model_id = "gpt-5"
    max_parallel = 1

    def __init__(self, provider: Provider | None = None) -> None:
        if provider is not None and provider.model_id:
            self.model_id = provider.model_id
        # Same one-call-at-a-time semaphore the existing claude_cli uses —
        # bulk-upload flows pile up behind it instead of spawning N copies
        # of the CLI in parallel.
        self._semaphore = asyncio.Semaphore(1)

    async def _complete(self, prompt: str) -> str:
        full = (
            f"SYSTEM INSTRUCTIONS:\n{self.system_instruction}\n\n"
            f"USER REQUEST:\n{prompt}"
        )
        return await self._call(full)

    async def _complete_plain(self, prompt: str) -> str:
        return await self._call(prompt)

    async def _call(self, prompt: str) -> str:
        async with self._semaphore:
            return await self._call_with_retry(prompt)

    async def _call_with_retry(self, prompt: str) -> str:
        last_exc: BaseException | None = None
        for attempt in range(len(BACKOFF_SCHEDULE) + 1):
            try:
                return await self._run(prompt)
            except CodexCliError:
                raise
            except (asyncio.TimeoutError, OSError, ConnectionError) as e:
                last_exc = e
                if attempt >= len(BACKOFF_SCHEDULE):
                    break
                delay = BACKOFF_SCHEDULE[attempt]
                logger.warning(
                    "Codex CLI transient error (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    len(BACKOFF_SCHEDULE) + 1,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
        raise TransientTranslatorError(
            "Codex CLI temporarily unavailable. The chapter is unchanged — "
            "try Retranslate later."
        ) from last_exc

    async def _run(self, prompt: str) -> str:
        path = resolve_binary(_BINARY)
        args = [path, "exec", "--skip-git-repo-check", "-"]
        if self.model_id and self.model_id != "default":
            args = [path, "exec", "--skip-git-repo-check", "--model", self.model_id, "-"]
        args = build_argv(args)

        rc, stdout, stderr = await run_subprocess(
            args, stdin_text=prompt, timeout_seconds=_CALL_TIMEOUT,
        )
        if rc != 0:
            combined = f"{stderr}\n{stdout}".strip()
            snippet = combined[:300]
            lower = combined.lower()
            if "not logged in" in lower or "authentication" in lower:
                raise CodexCliError(
                    "Codex CLI is not authenticated. Run `codex login` in a "
                    f"terminal, then retry. CLI output: {snippet}"
                )
            if "rate limit" in lower or "429" in lower:
                raise TransientTranslatorError(
                    "ChatGPT subscription rate limit hit. Wait for the limit "
                    f"window to reset, then Retranslate. CLI output: {snippet}"
                )
            raise CodexCliError(
                f"Codex CLI failed (exit {rc}): {snippet}"
            )
        if not stdout.strip():
            raise CodexCliError(
                f"Codex CLI returned empty stdout. stderr: {stderr[:200]}"
            )
        return stdout
