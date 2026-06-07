"""Anthropic Claude translator using a direct API key (not the local Claude
Code subscription).

Talks to api.anthropic.com via the `anthropic` async SDK. This is the API-key
sibling of `claude_agent` and `claude_cli`, which use the local subscription.

Auth: API key (x-api-key header). The provider's `secret_ref` names the env
var (default suggestion: `ANTHROPIC_API_KEY`).
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from backend.services.providers import Provider, resolve_secret

from .base import (
    BACKOFF_SCHEDULE,
    BaseTranslator,
    TransientTranslatorError,
)

logger = logging.getLogger(__name__)

# Per-request timeout. Matches gemini.py / deepseek.py — long enough for a
# long-context chapter, short enough that a hung connection doesn't wedge the
# serial queue forever.
_REQUEST_TIMEOUT = 300.0

# Claude API requires a max_tokens parameter on every request. 16k covers any
# realistic chapter; raise if a user reports truncation on a giant chapter.
_MAX_TOKENS = 16_384


def _is_transient(exc: BaseException) -> bool:
    try:
        import anthropic
    except ImportError:
        anthropic = None  # type: ignore[assignment]
    if anthropic is not None:
        transient_classes = (
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        )
        if isinstance(exc, transient_classes):
            return True
        if isinstance(exc, anthropic.APIStatusError):
            status = getattr(exc, "status_code", None)
            if isinstance(status, int) and (
                status == 408 or status == 429 or status >= 500
            ):
                return True
            return False
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return True
    return False


class AnthropicApiTranslator(BaseTranslator):
    name = "anthropic_api"
    max_parallel = 1

    def __init__(self, provider: Provider | None = None) -> None:
        if provider is None:
            raise RuntimeError(
                "AnthropicApiTranslator requires an explicit Provider row "
                "— configure one via /settings."
            )
        api_key = resolve_secret(provider)
        if not api_key:
            raise RuntimeError(
                f"Provider {provider.name!r} (anthropic_api) has no resolvable "
                f"API key. Set the env var named in its secret_ref "
                f"({provider.secret_ref!r}) or store it via the settings UI."
            )
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError(
                "The `anthropic` Python package is required for the "
                "anthropic_api provider type. Install with `pip install "
                "anthropic`."
            ) from e
        self.model_id = provider.model_id
        self._provider_name = provider.name
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=_REQUEST_TIMEOUT,
        )

    async def _complete(self, prompt: str) -> str:
        return await self._call(
            user_prompt=prompt,
            system_prompt=self.system_instruction,
            label="translate",
        )

    async def _complete_plain(self, prompt: str) -> str:
        return await self._call(
            user_prompt=prompt,
            system_prompt=None,
            label="fallback",
        )

    async def _call(
        self,
        *,
        user_prompt: str,
        system_prompt: str | None,
        label: str,
    ) -> str:
        self._check_call_budget()

        kwargs: dict = {
            "model": self.model_id,
            "max_tokens": _MAX_TOKENS,
            "messages": [{"role": "user", "content": user_prompt}],
            "temperature": 0.3,
        }
        if system_prompt:
            # Claude's API takes the system prompt as a top-level field. Send it
            # as a structured block with cache_control so the static system
            # instruction (byte-identical across a novel's chapters) is
            # prompt-cached: chapters within the 5-minute window read it from
            # cache instead of re-billing the full prefix. The cache hits surface
            # as usage.cache_read_input_tokens (plumbed into _emit_usage above).
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        t0 = time.perf_counter()
        last_exc: BaseException | None = None
        for attempt in range(len(BACKOFF_SCHEDULE) + 1):
            try:
                response = await self._client.messages.create(**kwargs)
                # Plumb usage into the cost accumulator. Anthropic exposes
                # cache_read_input_tokens for prompt-cache hits, mapped to
                # cached_input_tokens here. Coerce missing fields to 0.
                usage = getattr(response, "usage", None)
                if usage is not None:
                    self._emit_usage(
                        input_tokens=getattr(usage, "input_tokens", 0) or 0,
                        output_tokens=getattr(usage, "output_tokens", 0) or 0,
                        cached_input_tokens=(
                            getattr(usage, "cache_read_input_tokens", 0) or 0
                        ),
                    )
                if response.stop_reason == "max_tokens":
                    raise TransientTranslatorError(
                        f"Anthropic API response truncated at the {_MAX_TOKENS}-token "
                        f"limit (label={label}). The chapter is unchanged. Either "
                        f"split the chapter or raise _MAX_TOKENS in anthropic_api.py."
                    )
                # Concatenate all text blocks. For a non-tool-using model the
                # response is one TextBlock, but the schema allows more.
                parts = []
                for block in response.content or []:
                    text = getattr(block, "text", None)
                    if text:
                        parts.append(text)
                logger.info(
                    "anthropic_api %s call (%s): %.1fs",
                    label, self._provider_name, time.perf_counter() - t0,
                )
                return "".join(parts)
            except Exception as e:
                if not _is_transient(e):
                    raise
                last_exc = e
                if attempt >= len(BACKOFF_SCHEDULE):
                    break
                delay = BACKOFF_SCHEDULE[attempt]
                logger.warning(
                    "anthropic_api transient error (attempt %d/%d): %s — "
                    "retrying in %.1fs",
                    attempt + 1,
                    len(BACKOFF_SCHEDULE) + 1,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
        status = getattr(last_exc, "status_code", None)
        raise TransientTranslatorError(
            f"Anthropic API temporarily unavailable ({status or 'transient error'}). "
            "The chapter is unchanged — try Retranslate later."
        ) from last_exc
