"""OpenAI translator. Thin subclass — defaults its base URL to OpenAI's
public endpoint and inherits all the chat-completions plumbing from
`OpenAICompatibleTranslator`.

Auth: API key (Bearer). Set the provider's `secret_ref` to the env var
holding the key (default suggestion: `OPENAI_API_KEY`).
"""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleTranslator


class OpenAITranslator(OpenAICompatibleTranslator):
    name = "openai"
    DEFAULT_BASE_URL = "https://api.openai.com/v1"
