"""Shared transient-error classification for OpenAI-compatible backends.

DeepSeek and every `OpenAICompatibleTranslator` subclass talk to upstreams
through the same `openai` async SDK, so they share one rule for which failures
are worth a backoff-retry: 408 / 429 / 5xx and transport-level network blips
are transient; auth and bad-model errors are permanent and must surface.

The Gemini and Anthropic backends speak different SDKs with different exception
hierarchies, so they keep their own classifiers; only the openai-SDK pair lives
here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Sequence

import httpx
import openai

logger = logging.getLogger(__name__)


async def request_with_backoff(
    make_call: Callable[[], Awaitable],
    *,
    backoff: Sequence[float],
    name: str,
    transient_error_factory: Callable[[BaseException | None], Exception],
):
    """Run an openai-SDK call with the shared transient-retry backoff loop.

    This owns ONLY the retry scaffolding that DeepSeek and every
    OpenAICompatibleTranslator share: issue `make_call()`, on a transient
    error (per `is_transient_openai_error`) sleep for the next `backoff`
    delay and retry, and on exhaustion raise whatever
    `transient_error_factory(last_exc)` builds (each caller has its own
    user-facing message). A non-transient error propagates immediately on the
    first attempt.

    Per-response processing (usage emit, finish_reason gating, content
    extraction) stays with the caller and runs AFTER this returns the raw
    response. Those steps may raise non-transient errors (a `ValueError` for
    "no choices", a truncation error); doing them outside this loop is
    behavior-equivalent because such errors were never retried inside it.
    """
    attempts = len(backoff) + 1
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await make_call()
        except Exception as e:
            if not is_transient_openai_error(e):
                raise
            last_exc = e
            if attempt >= len(backoff):
                break
            delay = backoff[attempt]
            logger.warning(
                "%s transient error (attempt %d/%d): %s, retrying in %.1fs",
                name, attempt + 1, attempts, e, delay,
            )
            await asyncio.sleep(delay)
    raise transient_error_factory(last_exc) from last_exc


def is_transient_openai_error(exc: BaseException) -> bool:
    """True when `exc` from an openai-SDK call is worth retrying."""
    if isinstance(
        exc,
        (
            openai.RateLimitError,
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.InternalServerError,
        ),
    ):
        return True
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and (status == 408 or status == 429 or status >= 500):
            return True
        return False
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return True
    return False
