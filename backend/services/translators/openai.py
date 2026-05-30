"""OpenAI translator. Thin subclass — defaults its base URL to OpenAI's
public endpoint and inherits all the chat-completions plumbing from
`OpenAICompatibleTranslator`.

Auth: API key (Bearer). Set the provider's `secret_ref` to the env var
holding the key (default suggestion: `OPENAI_API_KEY`).

OpenAI's reasoning models (the o-series and the GPT-5 family) speak a
different chat-completions dialect than the classic GPT-4o/4.1 chat models:
they reject a non-default `temperature` (only the default `1` is accepted),
they rename `max_tokens` to `max_completion_tokens`, and they expose
`reasoning_effort`. The base `_build_kwargs` sends the classic shape, which on
a reasoning model either 400s on the temperature or runs the request long
enough at default effort to wedge the serial translate queue behind the 300s
timeout. The override below special-cases those models so gpt-5 / o3 actually
complete; gpt-4o / gpt-4.1 keep the classic shape untouched.
"""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleTranslator


class OpenAITranslator(OpenAICompatibleTranslator):
    name = "openai"
    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    # Model-id prefixes for OpenAI reasoning models. `o1`/`o3`/`o4` cover the
    # o-series; `gpt-5` covers the GPT-5 family (gpt-5, gpt-5-mini, ...).
    _REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")

    def _is_reasoning_model(self, model: str) -> bool:
        return (model or "").lower().startswith(self._REASONING_PREFIXES)

    def _build_kwargs(
        self,
        *,
        model: str,
        system_prompt: str | None,
        user_prompt: str,
        temperature: float,
        max_tokens: int | None,
    ) -> dict:
        kwargs = super()._build_kwargs(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if self._is_reasoning_model(model):
            # Reasoning models only accept the default temperature (1); sending
            # 0.3 is a hard 400. Drop it and let the API default apply.
            kwargs.pop("temperature", None)
            # These models renamed the output cap to max_completion_tokens.
            if "max_tokens" in kwargs:
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
            # Translating prose does not need deep reasoning; "low" keeps the
            # per-chapter latency well under the request timeout so a long
            # chapter cannot wedge the serial queue. ("low" is accepted by both
            # the o-series and the gpt-5 family.)
            kwargs.setdefault("reasoning_effort", "low")
        return kwargs
