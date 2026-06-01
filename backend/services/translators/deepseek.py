"""DeepSeek-backed translator. Uses the OpenAI-compatible API at
api.deepseek.com — calls go through the official `openai` async SDK with a
base_url override. DeepSeek does automatic prompt caching server-side, so the
glossary-heavy prefix gets cached transparently across chapters and the
per-chapter cost drops to a fraction of a cent on cache hits.

Quality model — this backend deliberately does NOT use JSON mode. DeepSeek's
free-form generation is markedly better than its `response_format=json_object`
output for long literary prose: JSON mode forces the whole chapter body into
one escaped string, which degrades the writing and raises the odds of
truncation / dropped passages. Instead the chapter body is emitted as raw text
inside a delimited envelope (see `DELIMITED_OUTPUT_INSTRUCTION` /
`_parse_deepseek_response`).

DeepSeek runs as a single-pass translator, like every other backend: one LLM
call per chapter on the provider's model. A user who wants a second polish pass
configures a per-novel refinement provider (`novels.refinement_provider_id` ->
`services/refiner.py`), which can point at DeepSeek or any other provider.
"""

from __future__ import annotations

import logging
import time

import openai

from backend.config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_MAX_OUTPUT_TOKENS,
    DEEPSEEK_REQUEST_TIMEOUT,
    DEEPSEEK_TRANSLATOR_MODEL,
    DEEPSEEK_TRANSLATOR_TEMPERATURE,
)
from backend.models import GlossaryEntry, TranslationResult
from backend.services import llm_cache
from backend.services.providers import Provider, resolve_secret

from ._openai_errors import is_transient_openai_error as _is_transient
from ._openai_errors import request_with_backoff
from .base import (
    BaseTranslator,
    TransientTranslatorError,
    parse_delimited_response,
)

logger = logging.getLogger(__name__)

_DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Transient-error backoff for DeepSeek calls. One retry only: a per-request
# timeout already bounds each attempt (DEEPSEEK_REQUEST_TIMEOUT), and a
# transient failure that doesn't clear on a single retry won't clear on three
# — better to error the chapter quickly than stretch the pipeline by ~16 min.
_DEEPSEEK_BACKOFF = (5.0,)

# Delimited free-form response envelope. The body sits as raw text between
# delimiters — no JSON escaping — which is what keeps DeepSeek's prose quality
# intact. Picked to be extremely unlikely to occur in real translated prose.
_BODY_DELIM = "=====BODY====="
_TERMS_DELIM = "=====TERMS====="

# DeepSeek shares the genre-aware system instruction with every other backend
# via BaseTranslator.translate_chapter, which sets self.system_instruction
# from (genre, custom_brief) before invoking the translation call. Use
# self.system_instruction everywhere a constant was tempting.


def _log_deepseek_usage(label: str, response: object) -> None:
    """Log per-call token usage for a DeepSeek response.

    The key signal when a short chapter unexpectedly hits the max_tokens
    ceiling is `completion_tokens_details.reasoning_tokens` — on a
    reasoning-capable model the chain-of-thought counts toward the same output
    budget as the visible answer, so a high reasoning count with a small
    visible body means the model is over-thinking, not that the chapter is
    long. `cached_tokens` shows DeepSeek's automatic prompt-cache hit on the
    glossary-heavy prefix."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    prompt = getattr(usage, "prompt_tokens", None) or 0
    completion = getattr(usage, "completion_tokens", None) or 0
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = (getattr(details, "reasoning_tokens", None) or 0) if details else 0
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    cached = (getattr(prompt_details, "cached_tokens", None) or 0) if prompt_details else 0
    logger.info(
        "deepseek %s usage: prompt=%d (cached=%d), completion=%d (reasoning=%d)",
        label, prompt, cached, completion, reasoning,
    )


def _parse_deepseek_response(
    raw: str, *, expect_terms: bool = True
) -> TranslationResult:
    """Parse the delimited free-form envelope into a TranslationResult.

    Delegates to the shared `base.parse_delimited_response` so the envelope
    shape (TITLE_EN / =====BODY===== / =====TERMS=====) lives in exactly one
    place. Raises ValueError when the BODY delimiter or the body text is
    missing, so the caller's retry-then-fallback path engages; a missing or
    malformed TERMS block is tolerated (new_terms -> []). `expect_terms=False`
    keeps the body but discards any parsed new terms (used by the polish
    pass)."""
    result = parse_delimited_response(raw)
    if not expect_terms:
        return result.model_copy(update={"new_terms": []})
    return result


class DeepSeekTranslator(BaseTranslator):
    name = "deepseek"
    model_id = DEEPSEEK_TRANSLATOR_MODEL
    # Forced serial. Matches every other backend in this project — the queue's
    # process-global lock enforces serial regardless of what we put here, and
    # the user is paying per-token so we don't speculatively parallelize.
    max_parallel = 1

    def __init__(self, provider: Provider | None = None) -> None:
        # When a Provider is passed, its model_id, base_url, and env-resolved
        # secret take precedence over the legacy DEEPSEEK_* globals. Two
        # providers of provider_type=deepseek can point at different models
        # (deepseek-chat vs deepseek-reasoner) and the LLM-cache key picks up
        # the difference via self.model_id.
        if provider is not None:
            # Explicit Provider: do NOT fall back to DEEPSEEK_API_KEY. Bad
            # provider config must surface as an error rather than silently
            # running under the legacy global key.
            api_key = resolve_secret(provider)
            self.model_id = provider.model_id or DEEPSEEK_TRANSLATOR_MODEL
            base_url = provider.base_url or _DEEPSEEK_BASE_URL
            if not api_key:
                raise RuntimeError(
                    f"Provider {provider.name!r} (deepseek) has no resolvable "
                    f"API key. Set the env var named in its secret_ref "
                    f"({provider.secret_ref!r}) or update the provider row."
                )
        else:
            api_key = DEEPSEEK_API_KEY
            self.model_id = DEEPSEEK_TRANSLATOR_MODEL
            base_url = _DEEPSEEK_BASE_URL
            if not api_key:
                raise RuntimeError(
                    "DeepSeek API key is not set. Configure a DeepSeek provider "
                    "in /settings, or set DEEPSEEK_API_KEY in .env for the "
                    "legacy default path."
                )
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=DEEPSEEK_REQUEST_TIMEOUT,
        )

    def cache_identity(self) -> str:
        """Beyond name + model, fold in the only other setting that changes the
        *output* for identical chapter inputs: the translation temperature.
        Without this, changing DEEPSEEK_TRANSLATOR_TEMPERATURE would return a
        cached result produced under the old setting. (DEEPSEEK_MAX_OUTPUT_TOKENS
        is left out: it never changes a non-truncated result, and truncated
        results are never cached.)"""
        return f"{self.name}:{self.model_id}:t{DEEPSEEK_TRANSLATOR_TEMPERATURE:g}"

    async def translate_chapter(
        self,
        chapter_zh: str,
        title_zh: str | None,
        glossary: list[GlossaryEntry],
        previous_context: str | None = None,
        style_edits: list[tuple[str, str]] | None = None,
        use_cache: bool = True,
        style_note: str | None = None,
        genre: str | None = None,
        custom_brief: str | None = None,
        free_draft: str | None = None,
        source_language: str | None = None,
    ) -> TranslationResult:
        """DeepSeek-specific flow: a single free-form delimited translation
        pass, with a parse-retry and a plain-text fallback."""
        # DeepSeek overrides translate_chapter entirely (single-pass envelope),
        # so it can't reuse the base loop — but the prologue is identical, so
        # it shares BaseTranslator._begin_chapter: counter/usage reset, genre-
        # aware system-instruction stash, prompt build, and cache-key derive.
        # The base build_prompt already defaults output_instruction to
        # DELIMITED_OUTPUT_INSTRUCTION, so the prompt (and cache key) match what
        # the explicit call produced before.
        prompt, cache_key = self._begin_chapter(
            chapter_zh, title_zh, glossary, previous_context, style_edits,
            style_note=style_note, genre=genre, custom_brief=custom_brief,
            free_draft=free_draft,
        )
        # use_cache=False (an explicit Retranslate) skips the read but still
        # stores the fresh result below, so the cache stays warm afterward.
        if use_cache:
            cached = llm_cache.load_translation(cache_key)
            if cached is not None:
                logger.info("deepseek translator cache HIT (key %s…)", cache_key[:12])
                return cached
            logger.info("deepseek translator cache MISS (key %s…)", cache_key[:12])
        else:
            logger.info(
                "deepseek translator cache SKIP (force_retranslate, key %s…)",
                cache_key[:12],
            )

        result, used_fallback = await self._translate_once(
            prompt, chapter_zh, title_zh
        )

        # Skip caching a degraded result. The plain-text fallback drops
        # new_terms; freezing it in the cache would make every later
        # Retranslate return the degraded text and never re-attempt the
        # envelope. Cache WITHOUT usage so cache hits don't replay token counts
        # from the original call (matches base.py behavior).
        if not used_fallback:
            llm_cache.store_translation(cache_key, result)
        elif not result.degraded:
            # A fallback draft — flag it so the reader can surface a
            # degraded-translation banner.
            result = result.model_copy(update={"degraded": True})
        return self._attach_usage(result)

    async def _translate_once(
        self, prompt: str, chapter_zh: str, title_zh: str | None
    ) -> tuple[TranslationResult, bool]:
        """Run the translation. Retry once on a malformed envelope, then fall
        back to the base plain-text translation. Returns (result, used_fallback)."""
        for attempt in range(2):
            try:
                raw = await self._call_deepseek(prompt, label="translate")
                return _parse_deepseek_response(raw), False
            except ValueError as e:
                # Covers a malformed envelope AND a non-transient ValueError
                # from the API call (e.g. "no choices"). A
                # TransientTranslatorError (transient exhaustion or a truncated
                # response) is not a ValueError, so it propagates and errors the
                # chapter instead.
                logger.warning(
                    "deepseek call/parse failed (attempt %d): %s",
                    attempt + 1,
                    e,
                )
        logger.warning("deepseek falling back to plain-text translation")
        return await self._plain_text_fallback(chapter_zh, title_zh), True

    # `translate_chapter` above drives the real flow. These two hooks satisfy
    # BaseTranslator's ABC; `_complete_plain` also backs the plain-text
    # fallback in `_translate_once`. Both run on the provider's model with the
    # standard token cap (the _call_deepseek defaults).
    async def _complete(self, prompt: str) -> str:
        return await self._call_deepseek(prompt, label="fallback")

    async def _complete_plain(self, prompt: str) -> str:
        # Identical to `_complete`: DeepSeek runs the same single-pass call for
        # both the ABC's structured hook and the plain-text fallback. Delegate
        # so a future label/temperature change only has to be made once.
        return await self._complete(prompt)

    async def _call_deepseek(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        label: str = "translate",
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        # Per-chapter budget gate. The single translation pass plus one
        # parse-retry and a plain-text fallback reach at most 3 calls, well
        # under MAX_LLM_CALLS_PER_CHAPTER. Going over is the regression signal
        # — surface it as a clean error rather than let the loop drift.
        self._check_call_budget()
        # Default the system instruction to whatever translate_chapter set for
        # this call (genre-aware).
        if system is None:
            system = self.system_instruction
        if temperature is None:
            temperature = DEEPSEEK_TRANSLATOR_TEMPERATURE
        if model is None:
            model = self.model_id
        if max_tokens is None:
            max_tokens = DEEPSEEK_MAX_OUTPUT_TOKENS
        kwargs: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        def _exhausted(last_exc: BaseException | None) -> Exception:
            status = getattr(last_exc, "status_code", None)
            return TransientTranslatorError(
                f"DeepSeek temporarily unavailable "
                f"({status or 'transient error'}). "
                "The chapter is unchanged, try Retranslate later."
            )

        t0 = time.perf_counter()
        # Shared transient-retry loop (with OpenAICompatibleTranslator). It owns
        # only the backoff scaffolding; the response processing below stays
        # here. A non-transient error (ValueError, the truncation
        # TransientTranslatorError) raised below was never retried inside the
        # loop, so running it after the loop returns is behavior-equivalent.
        response = await request_with_backoff(
            lambda: self._client.chat.completions.create(**kwargs),
            backoff=_DEEPSEEK_BACKOFF,
            name="DeepSeek",
            transient_error_factory=_exhausted,
        )
        choices = response.choices or []
        if not choices:
            raise ValueError("DeepSeek returned no choices")
        choice = choices[0]
        # Log usage regardless of outcome: on a truncation this is the only
        # place the reasoning/visible token split is visible.
        _log_deepseek_usage(label, response)
        # Plumb usage into the BaseTranslator accumulator. DeepSeek's
        # OpenAI-compatible API doesn't expose a cached-input concept;
        # cached_input_tokens stays 0 here.
        usage = getattr(response, "usage", None)
        if usage is not None:
            self._emit_usage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            )
        if choice.finish_reason == "length":
            # Output hit the token cap: the body is cut off mid-text. Retrying
            # yields the same truncation, so fail loudly with a clear message
            # rather than committing a partial chapter. TransientTranslatorError
            # is not transient per _is_transient, so it is never retried.
            raise TransientTranslatorError(
                f"DeepSeek {label} pass output was cut off at the token "
                f"limit ({max_tokens} tokens). The chapter "
                "is unchanged. If the chapter is genuinely long, raise "
                "DEEPSEEK_MAX_OUTPUT_TOKENS; if it is short, the model is "
                "looping or over-reasoning, check the "
                f"'deepseek {label} usage' log line for the "
                "reasoning/completion token split. Then Retranslate."
            )
        logger.info(
            "deepseek %s call: %.1fs", label, time.perf_counter() - t0
        )
        return choice.message.content or ""


async def _probe_deepseek_model(
    client: openai.AsyncOpenAI, model: str, *, role: str
) -> None:
    """Cheap round-trip for one model. Raises RuntimeError on a permanent
    misconfiguration (bad key / unknown model); returns quietly on a transient
    error so a flaky network doesn't block server boot."""
    try:
        await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ok"}],
            max_tokens=1,
        )
    except openai.AuthenticationError as e:
        raise RuntimeError(
            f"DeepSeek auth failed: {e}. Check DEEPSEEK_API_KEY in .env."
        ) from e
    except openai.NotFoundError as e:
        raise RuntimeError(
            f"DeepSeek {role} model {model!r} not found: {e}. Check the model "
            "name in .env — see https://api-docs.deepseek.com for the current "
            "model list."
        ) from e
    except openai.BadRequestError as e:
        # 400 here usually means the model name is invalid (DeepSeek returns
        # 400 instead of 404 for unknown models in some cases).
        raise RuntimeError(
            f"DeepSeek rejected probe for {role} model {model!r}: {e}. "
            "Check the model name in .env."
        ) from e
    except Exception as e:
        if _is_transient(e):
            logger.warning(
                "DeepSeek probe TRANSIENT failure for %s model %r: %s. Starting "
                "anyway — first real call will retry.",
                role, model, e,
            )
            return
        raise RuntimeError(
            f"DeepSeek probe failed for {role} model {model!r}: {e}."
        ) from e
    logger.info("DeepSeek probe ok (%s model=%s)", role, model)


async def probe_deepseek(provider: Provider | None = None) -> None:
    """Cheap startup round-trip. Fails loudly on bad key / unknown model so
    misconfiguration surfaces at boot, not on the first user-triggered
    translation. Transient errors are logged and let the server start.

    When `provider` is set, the probe targets the provider's secret/model/
    base_url (matching what the queue worker will actually call), NOT the
    legacy DEEPSEEK_* globals. A bad provider configuration must fail boot
    instead of being masked by an env var that happens to be set.
    """
    if provider is not None:
        api_key = resolve_secret(provider)
        if not api_key:
            raise RuntimeError(
                f"Default provider {provider.name!r} (deepseek) has no "
                f"resolvable API key. Set the env var named in its "
                f"secret_ref ({provider.secret_ref!r}) or update the row "
                f"in /api/providers."
            )
        base_url = provider.base_url or _DEEPSEEK_BASE_URL
        model = provider.model_id or DEEPSEEK_TRANSLATOR_MODEL
    else:
        if not DEEPSEEK_API_KEY:
            raise RuntimeError(
                "TRANSLATOR_BACKEND=deepseek but DEEPSEEK_API_KEY is empty. "
                "Set DEEPSEEK_API_KEY in .env or configure a DeepSeek "
                "provider in /settings."
            )
        api_key = DEEPSEEK_API_KEY
        base_url = _DEEPSEEK_BASE_URL
        model = DEEPSEEK_TRANSLATOR_MODEL
    client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
    await _probe_deepseek_model(client, model, role="translator")
