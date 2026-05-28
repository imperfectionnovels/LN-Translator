"""Translator backend selection.

`get_translator(provider)` dispatches by `provider.provider_type` and returns
a cached `BaseTranslator` instance per provider id. The backend reads its
model_id, base_url, and secret from the threaded-in Provider, which is what
makes per-novel model selection work.

`translator_factory()` is a backward-compat shim that returns the default
provider's backend; the startup probe and any tests that skip the provider
table still go through it. New code should call `get_translator` with an
explicit Provider.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from backend.config import TRANSLATOR_BACKEND
from backend.services.providers import Provider

from .base import BaseTranslator

logger = logging.getLogger(__name__)


# Dispatch table: provider_type → (module path, class name). Lazy-imported
# inside _instantiate so a missing optional dependency (e.g. the `anthropic`
# SDK for anthropic_api) only blows up when the user actually tries to use
# that type, not at server boot. The set of keys here MUST match
# `services.translator_catalog.all_type_keys()` — the catalog-parity test
# enforces this.
_DISPATCH: dict[str, tuple[str, str]] = {
    # Subscription / CLI subprocess (no API key — auth out-of-band).
    "claude_agent": ("backend.services.translators.claude_agent", "ClaudeAgentTranslator"),
    "claude_cli":   ("backend.services.translators.claude_cli",   "ClaudeCliTranslator"),
    "codex_cli":    ("backend.services.translators.codex_cli",    "CodexCliTranslator"),
    "gemini_cli":   ("backend.services.translators.gemini_cli",   "GeminiCliTranslator"),
    "opencode":     ("backend.services.translators.opencode",     "OpenCodeTranslator"),
    # API-key backends.
    "anthropic_api":     ("backend.services.translators.anthropic_api",     "AnthropicApiTranslator"),
    "gemini":            ("backend.services.translators.gemini",            "GeminiTranslator"),
    "openai":            ("backend.services.translators.openai",            "OpenAITranslator"),
    "deepseek":          ("backend.services.translators.deepseek",          "DeepSeekTranslator"),
    "xai":               ("backend.services.translators.xai",               "XAITranslator"),
    "mistral":           ("backend.services.translators.mistral",           "MistralTranslator"),
    "openrouter":        ("backend.services.translators.openrouter",        "OpenRouterTranslator"),
    "qwen":              ("backend.services.translators.qwen",              "QwenTranslator"),
    "zhipu":             ("backend.services.translators.zhipu",             "ZhipuTranslator"),
    "moonshot":          ("backend.services.translators.moonshot",          "MoonshotTranslator"),
    "groq":              ("backend.services.translators.groq",              "GroqTranslator"),
    "openai_compatible": ("backend.services.translators.openai_compatible_generic", "OpenAICompatibleGenericTranslator"),
    # Local.
    "ollama": ("backend.services.translators.ollama", "OllamaTranslator"),
    "google_translate_free": (
        "backend.services.translators.google_translate_free",
        "GoogleTranslateFreeTranslator",
    ),
}


def _instantiate(provider: Provider | None) -> BaseTranslator:
    """Construct a backend instance for the given Provider.

    When `provider` is None, falls back to a legacy `TRANSLATOR_BACKEND`-driven
    instantiation (used by `translator_factory()` for the startup probe and
    by `translate_chapter` when no provider is configured anywhere). When
    provided, the Provider is threaded into the backend's constructor so the
    backend can read its own model_id, base_url, and secret.
    """
    provider_type = provider.provider_type if provider is not None else TRANSLATOR_BACKEND
    spec = _DISPATCH.get(provider_type)
    if spec is None:
        raise RuntimeError(
            f"unknown provider_type {provider_type!r}; supported: "
            f"{sorted(_DISPATCH)}"
        )
    module_path, class_name = spec
    import importlib
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    logger.info("instantiating translator backend: %s", provider_type)
    return cls(provider=provider)


# Cache keyed by provider id. Different providers (even of the same type) get
# their own backend instance so per-provider client config (model_id, api key,
# base_url) diverges cleanly. The route layer drops entries via
# invalidate_provider_cache when a provider row changes.
_PROVIDER_CACHE: dict[int, BaseTranslator] = {}


def get_translator(provider: Provider) -> BaseTranslator:
    cached = _PROVIDER_CACHE.get(provider.id)
    if cached is not None:
        return cached
    instance = _instantiate(provider)
    _PROVIDER_CACHE[provider.id] = instance
    return instance


def invalidate_provider_cache(provider_id: int | None = None) -> None:
    """Drop a single provider (or all) from the cache. Call when a provider
    row is updated or deleted so the next request rebuilds with new config."""
    if provider_id is None:
        _PROVIDER_CACHE.clear()
        return
    _PROVIDER_CACHE.pop(provider_id, None)


@lru_cache(maxsize=1)
def translator_factory() -> BaseTranslator:
    """Backward-compat shim. Returns a backend matching `TRANSLATOR_BACKEND`
    from env, with no Provider — backends fall back to module-level config
    globals. Used by the startup probe and legacy tests that skip the
    provider table. New code should call `get_translator` with an explicit
    Provider instead.
    """
    return _instantiate(None)
