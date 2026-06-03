"""Shared scaffolding for the CLI-subprocess translators.

`codex_cli`, `gemini_cli`, and `opencode` are the same wrapper around three
different binaries: a one-call-at-a-time semaphore, the SYSTEM/USER prompt
framing, and the `BACKOFF_SCHEDULE` transient-retry loop are identical. The
only per-backend differences live in `_run` — the argv it builds and how it
classifies a non-zero exit into a permanent vs transient error.

This base owns the identical parts so a change to the retry/backoff policy
happens once. A subclass sets `permanent_error` (the exception type its
`_run` raises for unrecoverable failures, re-raised without retry) and
`unavailable_message` (surfaced after the retries are exhausted), and
implements `_run`.

(The API backends keep their own loops: `openai_compatible` already shares
one via `_openai_errors`, and the gemini/anthropic SDK backends have
streaming-specific handling. `claude_cli` / `claude_agent` classify errors
differently. Only the three identical subprocess wrappers fold in here.)
"""

from __future__ import annotations

import asyncio
import logging

from backend.services.providers import Provider

from .base import BACKOFF_SCHEDULE, BaseTranslator, TransientTranslatorError

logger = logging.getLogger(__name__)


class SubprocessCliTranslator(BaseTranslator):
    #: Permanent (non-retryable) error type the subclass's `_run` raises for
    #: an unrecoverable failure (bad model, auth missing, empty output). It is
    #: re-raised immediately instead of being retried.
    permanent_error: type[Exception] = Exception
    #: Surfaced as a TransientTranslatorError once every retry is exhausted.
    unavailable_message: str = (
        "The translator CLI is temporarily unavailable. The chapter is "
        "unchanged. Try Retranslate later."
    )

    def __init__(self, provider: Provider | None = None) -> None:
        if provider is not None and provider.model_id:
            self.model_id = provider.model_id
        # One CLI invocation at a time — bulk flows queue behind it instead of
        # spawning N copies of the binary in parallel.
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
            except self.permanent_error:
                raise
            except (asyncio.TimeoutError, OSError, ConnectionError) as e:
                last_exc = e
                if attempt >= len(BACKOFF_SCHEDULE):
                    break
                delay = BACKOFF_SCHEDULE[attempt]
                logger.warning(
                    "%s transient error (attempt %d/%d): %s — retrying in %.1fs",
                    self.name, attempt + 1, len(BACKOFF_SCHEDULE) + 1, e, delay,
                )
                await asyncio.sleep(delay)
        raise TransientTranslatorError(self.unavailable_message) from last_exc

    async def _run(self, prompt: str) -> str:
        """Invoke the binary once and return its stdout, or raise
        `permanent_error` / `TransientTranslatorError`. Subclass-specific."""
        raise NotImplementedError
