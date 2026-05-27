"""Mistral translator. OpenAI-compatible /chat/completions at api.mistral.ai/v1."""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleTranslator


class MistralTranslator(OpenAICompatibleTranslator):
    name = "mistral"
    DEFAULT_BASE_URL = "https://api.mistral.ai/v1"
