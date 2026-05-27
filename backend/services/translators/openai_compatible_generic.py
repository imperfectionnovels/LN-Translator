"""Generic OpenAI-compatible translator. The catch-all for any vendor not
already in the catalog — the user supplies `base_url` and `model_id` directly
and we treat the endpoint as a standard /chat/completions.

No `DEFAULT_BASE_URL`: the Provider must set one explicitly, otherwise the
base class raises at construction time.
"""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleTranslator


class OpenAICompatibleGenericTranslator(OpenAICompatibleTranslator):
    name = "openai_compatible"
    DEFAULT_BASE_URL = None
