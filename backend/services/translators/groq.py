"""Groq translator. OpenAI-compatible /chat/completions at api.groq.com/openai/v1.

Groq is a fast-inference proxy that runs open-weight models (Llama, Mixtral,
etc.) on custom hardware — useful when latency matters more than quality.
"""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleTranslator


class GroqTranslator(OpenAICompatibleTranslator):
    name = "groq"
    DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
