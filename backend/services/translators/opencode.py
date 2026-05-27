"""OpenCode translator.

OpenCode (https://opencode.ai) is an open-source multi-provider CLI agent —
the user logs into whichever providers they want (Anthropic, OpenAI, GitHub
Copilot, etc.) through OpenCode's own auth, and OpenCode routes requests to
the chosen model. This translator spawns `opencode run` as a subprocess and
forwards the user's chosen model alias verbatim.

Subprocess invocation:
    opencode run --model <provider/model> -

The `--model` flag uses OpenCode's namespaced form (e.g.
`anthropic/claude-opus-4-7`, `github-copilot/gpt-5`). Passing `-` on stdin
keeps the prompt off the command line so the Windows ~32 KB cap doesn't
bite.
"""

from __future__ import annotations

import asyncio
import logging

from backend.services.providers import Provider

from .base import (
    BACKOFF_SCHEDULE,
    BaseTranslator,
    TransientTranslatorError,
)
from ._subprocess_utils import build_argv, resolve_binary, run_subprocess

logger = logging.getLogger(__name__)

_BINARY = "opencode"
_CALL_TIMEOUT = 600.0


class OpenCodeError(Exception):
    """Hard failure from OpenCode — auth missing, unknown model alias, etc."""


class OpenCodeTranslator(BaseTranslator):
    name = "opencode"
    model_id = "anthropic/claude-opus-4-7"
    max_parallel = 1

    def __init__(self, provider: Provider | None = None) -> None:
        if provider is not None and provider.model_id:
            self.model_id = provider.model_id
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
            except OpenCodeError:
                raise
            except (asyncio.TimeoutError, OSError, ConnectionError) as e:
                last_exc = e
                if attempt >= len(BACKOFF_SCHEDULE):
                    break
                delay = BACKOFF_SCHEDULE[attempt]
                logger.warning(
                    "OpenCode transient error (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    len(BACKOFF_SCHEDULE) + 1,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
        raise TransientTranslatorError(
            "OpenCode temporarily unavailable. The chapter is unchanged — "
            "try Retranslate later."
        ) from last_exc

    async def _run(self, prompt: str) -> str:
        path = resolve_binary(_BINARY)
        args = [path, "run", "-"]
        if self.model_id:
            args = [path, "run", "--model", self.model_id, "-"]
        args = build_argv(args)

        rc, stdout, stderr = await run_subprocess(
            args, stdin_text=prompt, timeout_seconds=_CALL_TIMEOUT,
        )
        if rc != 0:
            combined = f"{stderr}\n{stdout}".strip()
            snippet = combined[:300]
            lower = combined.lower()
            if "not authenticated" in lower or "auth" in lower or "login" in lower:
                raise OpenCodeError(
                    "OpenCode is not authenticated for this provider. Run "
                    "`opencode auth login` in a terminal and select the "
                    f"provider you want to route through. CLI output: {snippet}"
                )
            if "rate limit" in lower or "429" in lower or "quota" in lower:
                raise TransientTranslatorError(
                    "OpenCode upstream rate limit hit. Wait for the limit "
                    f"window to reset, then Retranslate. CLI output: {snippet}"
                )
            raise OpenCodeError(
                f"OpenCode failed (exit {rc}): {snippet}"
            )
        if not stdout.strip():
            raise OpenCodeError(
                f"OpenCode returned empty stdout. stderr: {stderr[:200]}"
            )
        return stdout
