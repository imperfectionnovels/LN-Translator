"""xAI Grok translator. Speaks the OpenAI-compatible /chat/completions
shape at api.x.ai/v1.
"""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleTranslator


class XAITranslator(OpenAICompatibleTranslator):
    name = "xai"
    DEFAULT_BASE_URL = "https://api.x.ai/v1"
