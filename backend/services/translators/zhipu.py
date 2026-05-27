"""Zhipu GLM translator. Uses Zhipu's OpenAI-compatible /chat/completions
endpoint at https://open.bigmodel.cn/api/paas/v4.
"""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleTranslator


class ZhipuTranslator(OpenAICompatibleTranslator):
    name = "zhipu"
    DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
