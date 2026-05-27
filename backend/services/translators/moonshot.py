"""Moonshot Kimi translator. Uses Moonshot's OpenAI-compatible
/chat/completions endpoint at https://api.moonshot.cn/v1.
"""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleTranslator


class MoonshotTranslator(OpenAICompatibleTranslator):
    name = "moonshot"
    DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
