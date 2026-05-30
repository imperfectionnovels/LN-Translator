"""OpenAI translator request-shape tests.

The OpenAI reasoning models (o-series + GPT-5 family) reject a non-default
`temperature`, rename `max_tokens` to `max_completion_tokens`, and accept
`reasoning_effort`. `OpenAITranslator._build_kwargs` special-cases them so a
GPT-5 chapter actually completes instead of 400-ing on temperature or wedging
the serial queue at default reasoning effort. The classic GPT-4o / 4.1 chat
models must keep the original shape. These assert the dict directly — no
network call.
"""

from __future__ import annotations

import os

import pytest

from backend.services.providers import Provider
from backend.services.translators.openai import OpenAITranslator

_SECRET_ENV = "LN_TEST_OPENAI_KEY"


def _provider(model_id: str) -> Provider:
    # A unique env-var secret_ref keeps resolve_secret deterministic (the name
    # won't exist in the dev keyring, so it falls through to the env var).
    os.environ[_SECRET_ENV] = "sk-test-not-a-real-key"
    return Provider(
        id=1,
        name=f"openai-{model_id}",
        provider_type="openai",
        base_url=None,
        model_id=model_id,
        secret_ref=_SECRET_ENV,
    )


def _kwargs(model_id: str, *, max_tokens: int | None = None) -> dict:
    translator = OpenAITranslator(_provider(model_id))
    return translator._build_kwargs(
        model=model_id,
        system_prompt="you are a translator",
        user_prompt="translate this",
        temperature=0.3,
        max_tokens=max_tokens,
    )


@pytest.mark.parametrize("model_id", ["gpt-5", "gpt-5-mini", "o3", "o3-mini", "o4-mini"])
def test_reasoning_models_drop_temperature_and_set_effort(model_id: str) -> None:
    kwargs = _kwargs(model_id)
    # A non-default temperature is a hard 400 on these models — must be absent.
    assert "temperature" not in kwargs
    # reasoning_effort kept low so a long chapter doesn't hit the request timeout.
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["model"] == model_id


def test_reasoning_model_maps_cap_to_max_completion_tokens() -> None:
    kwargs = _kwargs("gpt-5", max_tokens=4096)
    assert kwargs["max_completion_tokens"] == 4096
    assert "max_tokens" not in kwargs


@pytest.mark.parametrize("model_id", ["gpt-4o", "gpt-4.1", "gpt-4o-mini"])
def test_classic_models_keep_temperature_and_max_tokens(model_id: str) -> None:
    kwargs = _kwargs(model_id, max_tokens=2048)
    assert kwargs["temperature"] == 0.3
    assert kwargs["max_tokens"] == 2048
    assert "reasoning_effort" not in kwargs
    assert "max_completion_tokens" not in kwargs


def test_classic_model_without_cap_omits_token_fields() -> None:
    kwargs = _kwargs("gpt-4o", max_tokens=None)
    assert "max_tokens" not in kwargs
    assert "max_completion_tokens" not in kwargs
    assert kwargs["temperature"] == 0.3
