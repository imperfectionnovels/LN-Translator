"""Shared base class for translators that speak the OpenAI Chat Completions
shape — POST `<base_url>/chat/completions` with a Bearer-token API key.

Covers OpenAI itself, xAI Grok, Mistral, OpenRouter, Qwen, Zhipu GLM, Moonshot
Kimi, Groq, Ollama's `/v1` proxy, and the generic catch-all
`openai_compatible` type. Each subclass is typically <20 lines — set `name`,
declare `DEFAULT_BASE_URL`, and that's it.

Deliberately NOT used by `deepseek.py`. DeepSeek is a single-pass translator
that overrides `translate_chapter` end-to-end (its body rides in the same
delimited envelope), so it stays a standalone class and this shared base can
keep its happy path narrow.

What this base provides:
- `_complete` and `_complete_plain` (the two abstract hooks `BaseTranslator`
  declares).
- HTTP plumbing through the `openai.AsyncOpenAI` async SDK (Bearer auth,
  configurable base_url, configurable timeout).
- Exponential backoff on transient errors (429 / 5xx / network).
- Token-usage emit so the per-chapter token columns keep working.

What subclasses can override:
- `DEFAULT_BASE_URL` — used when the Provider doesn't pin one.
- `TEMPERATURE` — translation temperature for `_complete`. Defaults to 0.3.
- `MAX_OUTPUT_TOKENS` — soft cap on output tokens. Defaults to None (let the
  model decide).
- `_build_kwargs(model, system, user)` — for vendors that need extra fields.
"""

from __future__ import annotations

import logging
import time

import openai

from backend.services.providers import Provider, resolve_secret

from ._openai_errors import request_with_backoff
from .base import (
    BACKOFF_SCHEDULE,
    BaseTranslator,
    TransientTranslatorError,
)

logger = logging.getLogger(__name__)

# Per-request timeout. 5 minutes covers slow long-context calls without
# letting a hung connection wedge the serial queue forever.
DEFAULT_REQUEST_TIMEOUT = 300.0


class OpenAICompatibleTranslator(BaseTranslator):
    """Subclass-friendly translator for any vendor exposing an OpenAI-style
    `/chat/completions` endpoint.

    Subclass contract: set `name` and (optionally) `DEFAULT_BASE_URL`,
    `TEMPERATURE`, `MAX_OUTPUT_TOKENS`. The Provider's `model_id`, `base_url`,
    and `secret_ref` flow in through `__init__`. The Provider is required —
    these backends have no legacy env-var fallback (unlike `gemini` /
    `deepseek` / `claude_cli`, which predate the providers table).
    """

    # Override in subclass. None means "the Provider must supply a base_url"
    # — useful for the generic `openai_compatible` type where there is no
    # sensible default.
    DEFAULT_BASE_URL: str | None = None

    # Translation temperature. 0.3 matches every other backend's literary-prose
    # default. Subclasses can lower it for stricter, less-creative output.
    TEMPERATURE: float = 0.3

    # Optional soft cap on output tokens. Most vendors accept None (= no cap
    # set on our side; the API's own limit applies). Subclasses can lower it
    # for chat-tuned smaller models that benefit from explicit ceilings.
    MAX_OUTPUT_TOKENS: int | None = None

    # Force-serial. The process-global queue lock already enforces this; the
    # class-level constant just makes the contract visible to the route
    # layer.
    max_parallel = 1

    def __init__(self, provider: Provider | None = None) -> None:
        if provider is None:
            # OpenAI-compatible backends were introduced AFTER the providers
            # table became the source of truth. They have no legacy env-var
            # fallback path. The factory raises here rather than the queue
            # worker getting a None client mid-translate.
            raise RuntimeError(
                f"{type(self).__name__} requires an explicit Provider row "
                "— configure one via /settings."
            )
        api_key = resolve_secret(provider)
        if not api_key:
            raise RuntimeError(
                f"Provider {provider.name!r} ({provider.provider_type}) has "
                f"no resolvable API key. Set the env var named in its "
                f"secret_ref ({provider.secret_ref!r}) or store it via the "
                f"settings UI's Set API Key button."
            )
        base_url = provider.base_url or self.DEFAULT_BASE_URL
        if not base_url:
            raise RuntimeError(
                f"Provider {provider.name!r} ({provider.provider_type}) "
                "has no base_url set and the backend declares no default. "
                "Edit the provider and set a Base URL."
            )
        self.model_id = provider.model_id
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        # Stash for log lines. Useful when the user has three OpenAI-compatible
        # providers and needs to know which one a log entry came from.
        self._provider_name = provider.name

    async def _complete(self, prompt: str) -> str:
        # self.system_instruction is set per-call by BaseTranslator.translate_chapter
        # from the resolved (genre, custom_brief). Pass it as the system message.
        return await self._call(
            user_prompt=prompt,
            system_prompt=self.system_instruction,
            temperature=self.TEMPERATURE,
            label="translate",
        )

    async def _complete_plain(self, prompt: str) -> str:
        # Plain-text fallback: no system instruction (the user prompt
        # carries the full request), same temperature.
        return await self._call(
            user_prompt=prompt,
            system_prompt=None,
            temperature=self.TEMPERATURE,
            label="fallback",
        )

    def _build_kwargs(
        self,
        *,
        model: str,
        system_prompt: str | None,
        user_prompt: str,
        temperature: float,
        max_tokens: int | None,
    ) -> dict:
        """Hook for subclasses that need extra request fields. The default
        is the lowest-common-denominator OpenAI-compatible shape — works
        with every vendor in the catalog."""
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        kwargs: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return kwargs

    async def _call(
        self,
        *,
        user_prompt: str,
        system_prompt: str | None,
        temperature: float,
        label: str,
    ) -> str:
        self._check_call_budget()
        kwargs = self._build_kwargs(
            model=self.model_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=self.MAX_OUTPUT_TOKENS,
        )

        def _exhausted(last_exc: BaseException | None) -> Exception:
            status = getattr(last_exc, "status_code", None)
            return TransientTranslatorError(
                f"{self.name} temporarily unavailable "
                f"({status or 'transient error'}). "
                "The chapter is unchanged, try Retranslate later."
            )

        t0 = time.perf_counter()
        # Shared transient-retry loop (with DeepSeek). It owns only the backoff
        # scaffolding; the response processing below stays here. A non-transient
        # error (the "no choices" ValueError, the truncation
        # TransientTranslatorError) raised below was never retried inside the
        # loop, so running it after the loop returns is behavior-equivalent.
        response = await request_with_backoff(
            lambda: self._client.chat.completions.create(**kwargs),
            backoff=BACKOFF_SCHEDULE,
            name=self.name,
            transient_error_factory=_exhausted,
        )
        choices = response.choices or []
        if not choices:
            raise ValueError(f"{self.name} returned no choices")
        choice = choices[0]
        # Plumb usage so the per-chapter token columns keep working. The OpenAI
        # schema's `prompt_tokens_details.cached_tokens` shows up when the
        # vendor advertises prompt caching (OpenAI itself, OpenRouter via the
        # underlying provider). Coerce missing fields to 0: vendor schemas vary
        # on which sub-objects are present.
        usage = getattr(response, "usage", None)
        if usage is not None:
            prompt_details = getattr(usage, "prompt_tokens_details", None)
            cached = (
                (getattr(prompt_details, "cached_tokens", None) or 0)
                if prompt_details else 0
            )
            self._emit_usage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                cached_input_tokens=cached,
            )
        if choice.finish_reason == "length":
            # Truncated output: retrying yields the same truncation. Surface a
            # clean error rather than committing a partial chapter.
            raise TransientTranslatorError(
                f"{self.name} response truncated at the token limit "
                f"(label={label}). The chapter is unchanged. "
                "Retranslate later or pick a model with a larger "
                "context window."
            )
        logger.info(
            "%s %s call (%s): %.1fs",
            self.name, label, self._provider_name, time.perf_counter() - t0,
        )
        return choice.message.content or ""
