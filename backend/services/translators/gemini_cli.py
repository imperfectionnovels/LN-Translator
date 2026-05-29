"""Google Gemini CLI translator.

Shells out to the local `gemini` binary so translations run on the user's
Google account (free tier + Gemini Advanced subscription) instead of an API
key. The subprocess is invoked non-interactively via `gemini --prompt`, with
the prompt passed as a flag value.

Subprocess invocation:
    gemini --prompt <prompt> --model <model_id> --output-format text

We pass the prompt on stdin via `--prompt-interactive=false` mode where
supported; if the binary doesn't accept stdin, fall back to the `-p` flag
form. The prompt is large enough that Windows' ~32 KB command-line cap is
a real risk, so the stdin path is preferred.
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

_BINARY = "gemini"
_CALL_TIMEOUT = 600.0


class GeminiCliError(Exception):
    """Hard failure from the Gemini CLI — bad model, auth missing, etc."""


class GeminiCliTranslator(BaseTranslator):
    name = "gemini_cli"
    model_id = "gemini-2.5-pro"
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
            except GeminiCliError:
                raise
            except (asyncio.TimeoutError, OSError, ConnectionError) as e:
                last_exc = e
                if attempt >= len(BACKOFF_SCHEDULE):
                    break
                delay = BACKOFF_SCHEDULE[attempt]
                logger.warning(
                    "Gemini CLI transient error (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    len(BACKOFF_SCHEDULE) + 1,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
        raise TransientTranslatorError(
            "Gemini CLI temporarily unavailable. The chapter is unchanged — "
            "try Retranslate later."
        ) from last_exc

    async def _run(self, prompt: str) -> str:
        path = resolve_binary(_BINARY)
        # `-p -` reads the prompt from stdin (the `-p` shorthand for
        # --prompt, `-` denoting stdin). `-m` selects the model.
        args = [path, "-p", "-"]
        if self.model_id:
            args = [path, "-m", self.model_id, "-p", "-"]
        args = build_argv(args)

        rc, stdout, stderr = await run_subprocess(
            args, stdin_text=prompt, timeout_seconds=_CALL_TIMEOUT,
        )
        if rc != 0:
            combined = f"{stderr}\n{stdout}".strip()
            snippet = combined[:300]
            lower = combined.lower()
            if "not authenticated" in lower or "login" in lower or "auth" in lower:
                raise GeminiCliError(
                    "Gemini CLI is not authenticated. Run `gemini` in a "
                    f"terminal and follow the login prompt, then retry. CLI "
                    f"output: {snippet}"
                )
            if "rate limit" in lower or "quota" in lower or "429" in lower:
                raise TransientTranslatorError(
                    "Gemini account quota / rate limit hit. Wait for the "
                    f"limit window to reset, then Retranslate. CLI output: {snippet}"
                )
            raise GeminiCliError(
                f"Gemini CLI failed (exit {rc}): {snippet}"
            )
        if not stdout.strip():
            raise GeminiCliError(
                f"Gemini CLI returned empty stdout. stderr: {stderr[:200]}"
            )
        return stdout
