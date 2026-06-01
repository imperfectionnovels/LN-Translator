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

import httpx
import openai


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
