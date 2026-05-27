"""Alibaba Qwen translator. Uses DashScope's OpenAI-compatible endpoint.

International endpoint:
    https://dashscope-intl.aliyuncs.com/compatible-mode/v1

China-mainland endpoint:
    https://dashscope.aliyuncs.com/compatible-mode/v1

The Provider's `base_url` decides which one to use.
"""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleTranslator


class QwenTranslator(OpenAICompatibleTranslator):
    name = "qwen"
    DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
