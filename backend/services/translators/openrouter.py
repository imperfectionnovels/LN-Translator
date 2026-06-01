"""OpenRouter translator. Aggregator that proxies to many vendor APIs under
one key, using the OpenAI-compatible /chat/completions shape.

`model_id` here is the namespaced form OpenRouter expects:
`<vendor>/<model>` — e.g. `anthropic/claude-opus-4-7`, `openai/gpt-5`.
"""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleTranslator


class OpenRouterTranslator(OpenAICompatibleTranslator):
    # OpenRouter accepts the standard OpenAI request shape verbatim, so this
    # subclass only pins the name + base URL and inherits the base
    # `_build_kwargs` / `_call` loop unchanged. (Attribution via the optional
    # `HTTP-Referer` / `X-Title` headers is a client-level concern, not a
    # per-request field, so it would not live in `_build_kwargs` anyway.)
    name = "openrouter"
    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
