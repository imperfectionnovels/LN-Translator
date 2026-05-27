"""OpenRouter translator. Aggregator that proxies to many vendor APIs under
one key, using the OpenAI-compatible /chat/completions shape.

`model_id` here is the namespaced form OpenRouter expects:
`<vendor>/<model>` — e.g. `anthropic/claude-opus-4-7`, `openai/gpt-5`.
"""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleTranslator


class OpenRouterTranslator(OpenAICompatibleTranslator):
    name = "openrouter"
    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

    def _build_kwargs(self, **kw) -> dict:
        # OpenRouter accepts the standard OpenAI shape; the only thing it
        # benefits from is the `HTTP-Referer` / `X-Title` headers for
        # attribution. The openai SDK passes those through if set on the
        # client, but the default base behavior is fine.
        return super()._build_kwargs(**kw)
