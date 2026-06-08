"""Direct tests for the Ollama translator subclass.

`OllamaTranslator` is a thin `OpenAICompatibleTranslator` subclass with one
real behavioral difference: it overrides `__init__` to skip the API-key check
(Ollama is a keyless local server) while still requiring a base_url, defaulting
to the local Ollama `/v1` endpoint. These pin its identity attributes and the
no-key construction path without touching the network.
"""

from __future__ import annotations

import pytest

from backend.services.providers import Provider
from backend.services.translators.ollama import OllamaTranslator
from backend.services.translators.openai_compatible import OpenAICompatibleTranslator


def _provider(*, base_url=None, model_id="llama3.3:70b") -> Provider:
    return Provider(
        id=7,
        name="my-ollama",
        provider_type="ollama",
        base_url=base_url,
        model_id=model_id,
        params={},
        secret_ref=None,
        is_default=False,
        last_tested_at=None,
        created_at="",
        updated_at="",
    )


def test_identity_attributes() -> None:
    """Catalog-visible identity: name and the local default endpoint, and it
    is genuinely an OpenAI-compatible subclass (inherits _build_kwargs / _call)."""
    assert OllamaTranslator.name == "ollama"
    assert OllamaTranslator.DEFAULT_BASE_URL == "http://localhost:11434/v1"
    assert issubclass(OllamaTranslator, OpenAICompatibleTranslator)
    # The shared serial contract is inherited unchanged.
    assert OllamaTranslator.max_parallel == 1


def test_construct_with_default_base_url_no_api_key() -> None:
    """A keyless provider (secret_ref=None) constructs fine, the base class's
    api-key guard is bypassed, and falls back to the default base_url."""
    t = OllamaTranslator(_provider(model_id="qwen2.5:72b"))
    assert t.model_id == "qwen2.5:72b"
    assert t._provider_name == "my-ollama"
    # The SDK client was built with the conventional placeholder key (the whole
    # reason Ollama overrides __init__), not None and not the missing secret_ref.
    assert t._client is not None
    assert t._client.api_key == "ollama"
    assert str(t._client.base_url).startswith("http://localhost:11434")


def test_provider_base_url_overrides_default() -> None:
    """An explicit base_url on the provider wins over DEFAULT_BASE_URL so a
    user can point at a remote Ollama host."""
    t = OllamaTranslator(_provider(base_url="http://gpu-box:11434/v1"))
    assert str(t._client.base_url).startswith("http://gpu-box:11434")
    assert t.model_id == "llama3.3:70b"


def test_none_provider_raises() -> None:
    """Ollama still requires an explicit Provider row, None is a hard error."""
    with pytest.raises(RuntimeError, match="requires an explicit Provider"):
        OllamaTranslator(provider=None)


def test_missing_base_url_raises() -> None:
    """If neither the provider nor the default supplies a base_url it raises, constructed here by stubbing the class default to None for one call."""

    class _NoDefault(OllamaTranslator):
        DEFAULT_BASE_URL = None

    with pytest.raises(RuntimeError, match="no base_url"):
        _NoDefault(_provider(base_url=None))
