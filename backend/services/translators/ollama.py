"""Ollama translator. Talks to a locally-running Ollama server through its
OpenAI-compatible /v1/chat/completions endpoint (default
http://localhost:11434/v1).

No API key required — Ollama is a local process. The base class's auth check
would normally raise on an empty `secret_ref`, so we override `__init__` to
construct the SDK client with a placeholder key.
"""

from __future__ import annotations

import logging

import openai

from backend.services.providers import Provider

from .openai_compatible import (
    DEFAULT_REQUEST_TIMEOUT,
    OpenAICompatibleTranslator,
)

logger = logging.getLogger(__name__)


class OllamaTranslator(OpenAICompatibleTranslator):
    name = "ollama"
    DEFAULT_BASE_URL = "http://localhost:11434/v1"

    def __init__(self, provider: Provider | None = None) -> None:
        # Bypass the api-key check in the base class — Ollama doesn't require
        # one (the SDK still needs *some* string for the Authorization header
        # construction; "ollama" is the conventional placeholder).
        if provider is None:
            raise RuntimeError(
                "OllamaTranslator requires an explicit Provider row "
                "— configure one via /settings."
            )
        base_url = provider.base_url or self.DEFAULT_BASE_URL
        if not base_url:
            raise RuntimeError(
                f"Provider {provider.name!r} (ollama) has no base_url set. "
                "Edit the provider and point it at your Ollama server."
            )
        self.model_id = provider.model_id
        self._client = openai.AsyncOpenAI(
            api_key="ollama",
            base_url=base_url,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        self._provider_name = provider.name
