"""Gemini-backed translator. Behavior preserved from the pre-refactor module:
- google-genai async client
- response_schema + response_mime_type for structured output
- exponential-backoff retry on transient HTTP/RPC errors
- bubbles up as TransientTranslatorError after retries are exhausted
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from backend.config import (
    GEMINI_API_KEY,
    GEMINI_REQUEST_TIMEOUT,
    GEMINI_TRANSLATOR_MODEL,
)
from backend.services.providers import Provider, resolve_secret

from .base import (
    BACKOFF_SCHEDULE,
    BaseTranslator,
    TransientTranslatorError,
)

logger = logging.getLogger(__name__)

# HTTP codes / RPC statuses that mean "try again later." 429 is rate-limit
# pressure; 5xx and the matching string statuses cover Gemini's typical
# transient signals ("UNAVAILABLE", "RESOURCE_EXHAUSTED", etc.).
_TRANSIENT_CODES = {408, 429, 500, 502, 503, 504}
_TRANSIENT_STATUSES = {
    "UNAVAILABLE",
    "RESOURCE_EXHAUSTED",
    "DEADLINE_EXCEEDED",
    "INTERNAL",
    "UNKNOWN",
}


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, genai_errors.APIError):
        if exc.code in _TRANSIENT_CODES:
            return True
        if (exc.status or "").upper() in _TRANSIENT_STATUSES:
            return True
        return False
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, _GeminiTruncatedEmpty):
        return True
    return False


class _GeminiBlocked(Exception):
    """Gemini returned a candidate with a hard block reason (SAFETY /
    RECITATION / PROHIBITED_CONTENT / etc). Not retryable inside this call —
    the model has explicitly refused. Surfaces as a TransientTranslatorError
    only because that's the chapter-error message the UI already explains
    to the user; the cause string makes the reason visible."""


class _GeminiTruncatedEmpty(Exception):
    """Gemini hit MAX_TOKENS with no body text. Retrying once with a fresh
    call sometimes recovers; if it doesn't, the retry loop's transient path
    routes through plain-text fallback."""


def _log_usage(response: object) -> None:
    """Surface Gemini's per-call token usage, especially cached input tokens.

    Implicit prompt caching activates automatically for Gemini Pro/Flash 2.x +
    when a recent call shares a long prefix (system_instruction + the front of
    the user prompt). Logging cached_content_token_count lets the user verify
    that bulk translations are actually hitting the cache; if this stays at 0
    chapter after chapter, the prompt prefix has drifted somewhere."""
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return
    prompt_tokens = getattr(meta, "prompt_token_count", None) or 0
    cached = getattr(meta, "cached_content_token_count", None) or 0
    output = getattr(meta, "candidates_token_count", None) or 0
    if cached:
        logger.info(
            "gemini translator usage: input=%d, cached=%d, output=%d (cache hit %.0f%% of input)",
            prompt_tokens, cached, output,
            (100 * cached / prompt_tokens) if prompt_tokens else 0.0,
        )
    else:
        logger.debug(
            "gemini translator usage: input=%d, cached=0, output=%d",
            prompt_tokens, output,
        )


def _check_finish_reason(response: object) -> None:
    """Inspect the first candidate's finish_reason. Raise classified errors
    immediately for known failure modes so we don't burn three round-trips
    (JSON x2 + plain-text fallback) on what's a hard upstream signal from
    the first call. STOP is a success; SAFETY / RECITATION etc. is permanent;
    bare MAX_TOKENS with no text is transient."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return
    cand = candidates[0]
    reason = getattr(cand, "finish_reason", None)
    name = getattr(reason, "name", None) or str(reason or "")
    name_u = name.upper()
    if name_u in ("STOP", "MODEL_LENGTH", "OTHER", "FINISH_REASON_UNSPECIFIED", ""):
        return
    if name_u in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "SPII", "BLOCKLIST"):
        raise _GeminiBlocked(name_u)
    if name_u in ("MAX_TOKENS", "LENGTH"):
        text = getattr(response, "text", "") or ""
        if not text.strip():
            raise _GeminiTruncatedEmpty(name_u)


class GeminiTranslator(BaseTranslator):
    name = "gemini"
    model_id = GEMINI_TRANSLATOR_MODEL  # default; instance __init__ may override
    # Forced serial. Gemini's quota technically tolerates parallel calls, but
    # the user cares about accidentally burning API tokens — keeping a single
    # in-flight chapter caps worst-case spend at one chapter's worth of input
    # + output, no matter what kicks off a sweep.
    max_parallel = 1

    def __init__(self, provider: Provider | None = None) -> None:
        # When a Provider is passed, its model_id and (env-resolved) secret
        # take precedence over the legacy GEMINI_* globals. This is what
        # makes "Novel A uses gemini-3-pro-preview, Novel B uses
        # gemini-3-flash-preview" work — the cache key in BaseTranslator
        # reads self.model_id so different model_ids land in different cache
        # buckets automatically.
        if provider is not None:
            # When a Provider is explicit, we DO NOT fall back to GEMINI_API_KEY.
            # A bad/missing secret_ref must surface as a configuration error so
            # the user fixes the provider row instead of accidentally running
            # under the legacy global key. /api/providers/{id}/test enforces
            # the same rule; the backend should match.
            api_key = resolve_secret(provider)
            self.model_id = provider.model_id or GEMINI_TRANSLATOR_MODEL
            if not api_key:
                raise RuntimeError(
                    f"Provider {provider.name!r} (gemini) has no resolvable "
                    f"API key. Set the env var named in its secret_ref "
                    f"({provider.secret_ref!r}) or update the provider row "
                    f"in /api/providers."
                )
        else:
            api_key = GEMINI_API_KEY
            self.model_id = GEMINI_TRANSLATOR_MODEL
            if not api_key:
                raise RuntimeError(
                    "Gemini API key is not set. Configure a Gemini provider "
                    "in /settings, or set GEMINI_API_KEY in .env for the "
                    "legacy default path."
                )
        self._client = genai.Client(api_key=api_key)

    async def _complete(self, prompt: str) -> str:
        # JSON mode dropped 2026-05-22: response_mime_type=application/json
        # forces the body into one escaped string, which degrades prose. The
        # delimited envelope keeps the chapter body unescaped.
        # self.system_instruction is set per call by BaseTranslator.translate_chapter
        # from the (genre, custom_brief) the queue worker supplies.
        config = types.GenerateContentConfig(
            system_instruction=self.system_instruction,
            temperature=0.3,
        )
        return await self._call_gemini(prompt, config)

    async def _complete_plain(self, prompt: str) -> str:
        return await self._call_gemini(
            prompt, types.GenerateContentConfig(temperature=0.3)
        )

    async def _call_gemini(
        self, prompt: str, config: types.GenerateContentConfig
    ) -> str:
        last_exc: BaseException | None = None
        for attempt in range(len(BACKOFF_SCHEDULE) + 1):
            try:
                # Bounded so a hung connection can't block the serial queue
                # worker indefinitely. On timeout asyncio.TimeoutError is
                # raised, which _is_transient treats as retryable.
                response = await asyncio.wait_for(
                    self._client.aio.models.generate_content(
                        model=self.model_id,
                        contents=prompt,
                        config=config,
                    ),
                    timeout=GEMINI_REQUEST_TIMEOUT,
                )
                # Classify safety / MAX_TOKENS before returning. Without this,
                # an empty-bodied safety block routes through parse_response →
                # retry → plain-text fallback, burning 3 round-trips for no
                # gain.
                _check_finish_reason(response)
                _log_usage(response)
                # Plumb usage into the BaseTranslator accumulator so the
                # queue worker can persist tokens + compute cost.
                meta = getattr(response, "usage_metadata", None)
                if meta is not None:
                    self._emit_usage(
                        input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
                        output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
                        cached_input_tokens=getattr(
                            meta, "cached_content_token_count", 0
                        ) or 0,
                    )
                return response.text or ""
            except _GeminiBlocked as e:
                # Hard refusal from Gemini. Surface as a TransientTranslator
                # so the chapter ends up in `status='error'` with a message
                # the user can act on, but never retry — the model will say
                # no again.
                raise TransientTranslatorError(
                    f"Gemini blocked the response (finish_reason={e}). "
                    "This usually means the chapter triggered a safety / "
                    "recitation filter. Try editing the source slightly or "
                    "switch to the claude_cli backend."
                ) from e
            except Exception as e:
                if not _is_transient(e):
                    raise
                last_exc = e
                if attempt >= len(BACKOFF_SCHEDULE):
                    break
                delay = BACKOFF_SCHEDULE[attempt]
                logger.warning(
                    "Gemini transient error (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    len(BACKOFF_SCHEDULE) + 1,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
        code = getattr(last_exc, "code", None)
        status = getattr(last_exc, "status", None)
        pretty = (
            f"Gemini temporarily unavailable ({status or code or 'transient error'}). "
            "The chapter is unchanged — try Retranslate later."
        )
        raise TransientTranslatorError(pretty) from last_exc


async def probe_gemini(
    provider: Provider | None = None, *, role: str = "translator"
) -> str:
    """Cheap startup round-trip for the Gemini backend.

    Returns "ok" on success and "warn" on a transient failure (network blip,
    rate limit, 5xx) so a flaky network does not block boot. Raises
    RuntimeError on a permanent misconfiguration (bad key / unknown model) so
    the user fixes it before serving. Mirrors `probe_deepseek`; the caller in
    main.py records the returned state in LAST_PROBE_STATE.

    When `provider` is set, the probe targets the provider's resolved secret
    and model (matching what the queue worker will call), with the legacy
    GEMINI_* globals as the last-resort fallback.
    """
    api_key = (resolve_secret(provider) if provider is not None else None) or GEMINI_API_KEY
    if not api_key:
        if provider is not None:
            raise RuntimeError(
                f"Default provider {provider.name!r} is type 'gemini' but its "
                f"secret_ref {provider.secret_ref!r} is unset. Set the env var "
                f"or update the provider's secret_ref in /api/providers."
            )
        raise RuntimeError(
            "Gemini probe: GEMINI_API_KEY is unset. Configure a Gemini provider "
            "in /settings or set GEMINI_API_KEY in .env."
        )
    gemini_model = (
        provider.model_id if provider is not None else None
    ) or GEMINI_TRANSLATOR_MODEL

    client = genai.Client(api_key=api_key)
    try:
        await client.aio.models.generate_content(
            model=gemini_model,
            contents="ok",
            config=types.GenerateContentConfig(max_output_tokens=1),
        )
    except genai_errors.APIError as e:
        if _is_transient(e):
            logger.warning(
                "Gemini %s probe TRANSIENT failure for model %r: %s. Starting "
                "anyway — first real call will retry.",
                role, gemini_model, e,
            )
            return "warn"
        raise RuntimeError(
            f"Gemini {role} probe failed for model {gemini_model!r}: {e}. "
            "Check the API key and the model name."
        ) from e
    except Exception as e:
        logger.warning(
            "Gemini %s probe network failure for model %r: %s. Starting anyway.",
            role, gemini_model, e,
        )
        return "warn"
    logger.info("Gemini %s probe ok (model=%s)", role, gemini_model)
    return "ok"
