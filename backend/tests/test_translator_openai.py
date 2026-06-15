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
import types

import httpx
import openai
import pytest

from backend.models import TokenUsage
from backend.services.providers import Provider
from backend.services.translators import _openai_errors
from backend.services.translators.base import TransientTranslatorError
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


# ============================================================================
# _call retry / backoff / usage loop
# ============================================================================
#
# The retry+backoff+usage loop in OpenAICompatibleTranslator._call is the
# highest-fan-in untested path: all 10 OpenAI-compatible vendor subclasses
# inherit it verbatim. These tests drive one concrete subclass
# (OpenAITranslator) with a STUBBED async client so no network call happens,
# and monkeypatch asyncio.sleep to a no-op so the backoff schedule doesn't
# actually wait.


def _bare_translator(model_id: str = "gpt-4o") -> OpenAITranslator:
    """Build an OpenAITranslator WITHOUT running __init__ (which would create
    a real openai.AsyncOpenAI client and demand a resolvable API key). We set
    only the attributes _call touches: model_id, _client, _provider_name, plus
    the per-chapter counters BaseTranslator normally resets in
    translate_chapter."""
    t = OpenAITranslator.__new__(OpenAITranslator)
    t.model_id = model_id
    t._provider_name = "test-provider"
    t._client = None  # the test sets a fake before calling _call
    t._llm_call_count = 0
    t._usage_accumulator = TokenUsage()
    t.system_instruction = ""
    return t


def _fake_usage(prompt_tokens: int, completion_tokens: int, cached: int = 0):
    """A duck-typed usage object matching the attributes _call reads."""
    details = types.SimpleNamespace(cached_tokens=cached) if cached else None
    return types.SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_details=details,
    )


def _fake_response(content: str, *, finish_reason: str = "stop", usage=None):
    """A duck-typed chat.completions response: .choices[0].message.content,
    .choices[0].finish_reason, .usage."""
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message, finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice], usage=usage)


class _FakeCompletions:
    """Stub for client.chat.completions — `create` pops queued outcomes.

    Each queued item is either a response object (returned) or an exception
    instance (raised). Records every call's kwargs for assertions."""

    def __init__(self, outcomes: list) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _fake_client(outcomes: list) -> types.SimpleNamespace:
    completions = _FakeCompletions(outcomes)
    chat = types.SimpleNamespace(completions=completions)
    return types.SimpleNamespace(chat=chat, _completions=completions)


def _rate_limit_error() -> openai.RateLimitError:
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.RateLimitError(
        "rate limited", response=httpx.Response(429, request=req), body=None
    )


def _auth_error() -> openai.AuthenticationError:
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.AuthenticationError(
        "bad key", response=httpx.Response(401, request=req), body=None
    )


@pytest.fixture
def _no_sleep(monkeypatch):
    """Neutralize backoff so the retry loop runs instantly. The retry loop
    now lives in the shared _openai_errors.request_with_backoff helper, so
    patch the sleep there."""
    async def _instant(_delay):
        return None
    monkeypatch.setattr(_openai_errors.asyncio, "sleep", _instant)


@pytest.mark.asyncio
async def test_call_retries_transient_then_succeeds(_no_sleep) -> None:
    """A RateLimitError (transient) on the first attempt followed by a
    success on retry returns the body — and the second call's kwargs prove
    the request was re-issued."""
    t = _bare_translator()
    body = "TITLE_EN: X\n=====BODY=====\nHello.\n=====TERMS=====\n[]"
    t._client = _fake_client([_rate_limit_error(), _fake_response(body)])

    out = await t._call(
        user_prompt="translate this",
        system_prompt="sys",
        temperature=0.3,
        label="translate",
    )
    assert out == body
    # Exactly two create() calls: the failed attempt + the successful retry.
    assert len(t._client._completions.calls) == 2


@pytest.mark.asyncio
async def test_call_non_transient_raises_immediately(_no_sleep) -> None:
    """An AuthenticationError is permanent — it must propagate on the first
    attempt and NOT be retried (only one create() call)."""
    t = _bare_translator()
    t._client = _fake_client([_auth_error(), _fake_response("unreached")])

    with pytest.raises(openai.AuthenticationError):
        await t._call(
            user_prompt="translate this",
            system_prompt="sys",
            temperature=0.3,
            label="translate",
        )
    # The success outcome was never consumed — no retry happened.
    assert len(t._client._completions.calls) == 1


@pytest.mark.asyncio
async def test_call_length_finish_reason_raises_transient(_no_sleep) -> None:
    """A response truncated at the token limit (finish_reason == 'length')
    raises TransientTranslatorError rather than committing a partial body.
    Truncation is not retried (retrying yields the same truncation)."""
    t = _bare_translator()
    body = "TITLE_EN: X\n=====BODY=====\nHalf a chap"
    t._client = _fake_client([_fake_response(body, finish_reason="length")])

    with pytest.raises(TransientTranslatorError):
        await t._call(
            user_prompt="translate this",
            system_prompt="sys",
            temperature=0.3,
            label="translate",
        )
    assert len(t._client._completions.calls) == 1


@pytest.mark.asyncio
async def test_call_emits_usage_with_mapped_token_counts(_no_sleep) -> None:
    """_emit_usage maps prompt_tokens → input, completion_tokens → output,
    and prompt_tokens_details.cached_tokens → cached. Assert the
    accumulator totals after a successful call."""
    t = _bare_translator()
    body = "TITLE_EN: X\n=====BODY=====\nHello.\n=====TERMS=====\n[]"
    usage = _fake_usage(prompt_tokens=1200, completion_tokens=800, cached=300)
    t._client = _fake_client([_fake_response(body, usage=usage)])

    out = await t._call(
        user_prompt="translate this",
        system_prompt="sys",
        temperature=0.3,
        label="translate",
    )
    assert out == body
    acc = t._usage_accumulator
    assert acc.input_tokens == 1200
    assert acc.output_tokens == 800
    assert acc.cached_input_tokens == 300


@pytest.mark.asyncio
async def test_call_exhausts_retries_then_raises_transient(_no_sleep) -> None:
    """Persistent transient errors across the whole backoff schedule end in a
    TransientTranslatorError (the chapter-unchanged surface), after
    len(BACKOFF_SCHEDULE)+1 total attempts."""
    from backend.services.translators.base import BACKOFF_SCHEDULE

    t = _bare_translator()
    attempts = len(BACKOFF_SCHEDULE) + 1
    t._client = _fake_client([_rate_limit_error() for _ in range(attempts)])

    with pytest.raises(TransientTranslatorError):
        await t._call(
            user_prompt="translate this",
            system_prompt="sys",
            temperature=0.3,
            label="translate",
        )
    assert len(t._client._completions.calls) == attempts
