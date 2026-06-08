"""Direct tests for the shared OpenAI-compatible translator base.

Two modules under test, both shared by all 10 OpenAI-SDK vendor subclasses:

  * `_openai_errors.is_transient_openai_error` / `request_with_backoff`, the
    transient-vs-permanent classifier and the backoff-retry scaffolding.
  * `openai_compatible.OpenAICompatibleTranslator`, `_build_kwargs` envelope
    construction plus the `_call` request/usage/finish-reason loop.

No network: the `openai.AsyncOpenAI` client is replaced by a tiny duck-typed
fake whose `chat.completions.create` pops queued responses or raises queued
exceptions. `asyncio.sleep` is monkeypatched to a no-op so backoff is instant.
The envelope round-trip is checked against the real `parse_delimited_response`.
"""

from __future__ import annotations

import asyncio
import types

import httpx
import openai
import pytest

from backend.models import TokenUsage
from backend.services.translators import _openai_errors
from backend.services.translators._openai_errors import (
    is_transient_openai_error,
    request_with_backoff,
)
from backend.services.translators.base import (
    BACKOFF_SCHEDULE,
    TransientTranslatorError,
    parse_delimited_response,
)
from backend.services.translators.openai_compatible import (
    DEFAULT_REQUEST_TIMEOUT,
    OpenAICompatibleTranslator,
)

# ---------------------------------------------------------------------------
# Synthetic openai-SDK exceptions (no network, constructed from httpx stubs).
# ---------------------------------------------------------------------------


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.example.com/v1/chat/completions")


def _status_error(code: int) -> openai.APIStatusError:
    resp = httpx.Response(code, request=_request())
    return openai.APIStatusError("status", response=resp, body=None)


def _rate_limit_error() -> openai.RateLimitError:
    return openai.RateLimitError(
        "rate limited", response=httpx.Response(429, request=_request()), body=None
    )


def _auth_error() -> openai.AuthenticationError:
    return openai.AuthenticationError(
        "bad key", response=httpx.Response(401, request=_request()), body=None
    )


# ===========================================================================
# is_transient_openai_error
# ===========================================================================


def test_known_transient_sdk_errors_are_transient() -> None:
    """RateLimit / timeout / connection / 5xx SDK error types all retry."""
    assert is_transient_openai_error(_rate_limit_error()) is True
    assert is_transient_openai_error(
        openai.APITimeoutError(request=_request())
    ) is True
    assert is_transient_openai_error(
        openai.APIConnectionError(message="down", request=_request())
    ) is True
    assert is_transient_openai_error(_status_error(503)) is True


def test_status_error_threshold_boundaries() -> None:
    """Only 408, 429, and >=500 are retried; a 400/401/404 is permanent."""
    assert is_transient_openai_error(_status_error(408)) is True
    assert is_transient_openai_error(_status_error(429)) is True
    assert is_transient_openai_error(_status_error(500)) is True
    # Permanent client errors must NOT be retried.
    assert is_transient_openai_error(_status_error(400)) is False
    assert is_transient_openai_error(_status_error(404)) is False


def test_auth_and_value_errors_are_not_transient() -> None:
    """Authentication and plain ValueErrors are permanent failures."""
    assert is_transient_openai_error(_auth_error()) is False
    assert is_transient_openai_error(ValueError("no choices")) is False
    assert is_transient_openai_error(RuntimeError("boom")) is False


def test_transport_and_stdlib_network_errors_are_transient() -> None:
    """Raw httpx transport blips and stdlib ConnectionError/TimeoutError retry."""
    assert is_transient_openai_error(httpx.ConnectError("refused")) is True
    assert is_transient_openai_error(httpx.ReadTimeout("slow")) is True
    assert is_transient_openai_error(asyncio.TimeoutError()) is True
    assert is_transient_openai_error(ConnectionError("reset")) is True


# ===========================================================================
# request_with_backoff
# ===========================================================================


@pytest.fixture
def _no_sleep(monkeypatch):
    async def _instant(_delay):
        return None

    monkeypatch.setattr(_openai_errors.asyncio, "sleep", _instant)


def _factory(last_exc):
    """transient_error_factory used in the retry tests: records the last_exc."""
    return TransientTranslatorError(f"exhausted: {type(last_exc).__name__}")


@pytest.mark.asyncio
async def test_backoff_returns_first_success_without_retry(_no_sleep) -> None:
    """A call that succeeds immediately is issued exactly once."""
    calls = {"n": 0}

    async def _make_call():
        calls["n"] += 1
        return "ok"

    out = await request_with_backoff(
        _make_call, backoff=BACKOFF_SCHEDULE, name="t",
        transient_error_factory=_factory,
    )
    assert out == "ok"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_backoff_retries_transient_then_succeeds(_no_sleep) -> None:
    """Two transient errors then a success: three issued calls, the body of
    the third returns."""
    outcomes = [_rate_limit_error(), _status_error(500), "recovered"]
    calls = {"n": 0}

    async def _make_call():
        idx = calls["n"]
        calls["n"] += 1
        item = outcomes[idx]
        if isinstance(item, BaseException):
            raise item
        return item

    out = await request_with_backoff(
        _make_call, backoff=BACKOFF_SCHEDULE, name="t",
        transient_error_factory=_factory,
    )
    assert out == "recovered"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_backoff_permanent_error_propagates_first_attempt(_no_sleep) -> None:
    """A non-transient error is raised on the first attempt, untouched by the
    factory, and no retry is issued."""
    calls = {"n": 0}

    async def _make_call():
        calls["n"] += 1
        raise _auth_error()

    with pytest.raises(openai.AuthenticationError):
        await request_with_backoff(
            _make_call, backoff=BACKOFF_SCHEDULE, name="t",
            transient_error_factory=_factory,
        )
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_backoff_exhaustion_raises_from_factory(_no_sleep) -> None:
    """Persistent transient errors exhaust len(backoff)+1 attempts and then
    raise whatever the factory builds, chained from the last exception."""
    attempts = len(BACKOFF_SCHEDULE) + 1
    calls = {"n": 0}

    async def _make_call():
        calls["n"] += 1
        raise _rate_limit_error()

    with pytest.raises(TransientTranslatorError) as exc_info:
        await request_with_backoff(
            _make_call, backoff=BACKOFF_SCHEDULE, name="t",
            transient_error_factory=_factory,
        )
    assert calls["n"] == attempts
    # The factory saw the last exception (a RateLimitError) and __cause__ chains it.
    assert "RateLimitError" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, openai.RateLimitError)


# ===========================================================================
# OpenAICompatibleTranslator, _build_kwargs (pure, no client)
# ===========================================================================


def _bare_translator(model_id: str = "test-model") -> OpenAICompatibleTranslator:
    """Build a translator WITHOUT __init__ so no real client / API key is
    required. We set only the attributes the methods under test read."""
    t = OpenAICompatibleTranslator.__new__(OpenAICompatibleTranslator)
    t.model_id = model_id
    t.name = "compat"
    t._provider_name = "test-provider"
    t._client = None
    t._llm_call_count = 0
    t._usage_accumulator = TokenUsage()
    t.system_instruction = ""
    return t


def test_build_kwargs_includes_system_and_user_messages() -> None:
    """With a system prompt set, the messages list has system then user, the
    model is forwarded, and temperature rides along."""
    t = _bare_translator("m1")
    kwargs = t._build_kwargs(
        model="m1", system_prompt="be a translator",
        user_prompt="translate this", temperature=0.3, max_tokens=None,
    )
    assert kwargs["model"] == "m1"
    assert kwargs["temperature"] == 0.3
    assert kwargs["messages"][0] == {"role": "system", "content": "be a translator"}
    assert kwargs["messages"][1] == {"role": "user", "content": "translate this"}


def test_build_kwargs_omits_system_when_none_and_caps_tokens() -> None:
    """No system prompt => only a user message; max_tokens is set only when
    provided."""
    t = _bare_translator()
    no_sys = t._build_kwargs(
        model="m", system_prompt=None, user_prompt="u",
        temperature=0.3, max_tokens=512,
    )
    assert len(no_sys["messages"]) == 1
    assert no_sys["messages"][0]["role"] == "user"
    assert no_sys["max_tokens"] == 512

    without_cap = t._build_kwargs(
        model="m", system_prompt=None, user_prompt="u",
        temperature=0.3, max_tokens=None,
    )
    assert "max_tokens" not in without_cap


# ===========================================================================
# OpenAICompatibleTranslator, _call (stubbed async client)
# ===========================================================================


def _fake_usage(prompt_tokens: int, completion_tokens: int, cached: int = 0):
    details = types.SimpleNamespace(cached_tokens=cached) if cached else None
    return types.SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_details=details,
    )


def _fake_response(content: str, *, finish_reason: str = "stop", usage=None):
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message, finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice], usage=usage)


class _FakeCompletions:
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


_ENVELOPE = "TITLE_EN: Chapter One\n=====BODY=====\nHe drew his blade.\n=====TERMS=====\n[]"


@pytest.mark.asyncio
async def test_call_returns_envelope_parseable_by_base(_no_sleep) -> None:
    """The raw string _call returns is the delimited envelope the shared
    parser consumes, round-trip it to prove the contract end to end."""
    t = _bare_translator()
    t._client = _fake_client([_fake_response(_ENVELOPE)])
    out = await t._call(
        user_prompt="u", system_prompt="s", temperature=0.3, label="translate",
    )
    assert out == _ENVELOPE
    parsed = parse_delimited_response(out)
    assert parsed.title_en == "Chapter One"
    assert parsed.translated_text == "He drew his blade."


@pytest.mark.asyncio
async def test_call_emits_mapped_usage(_no_sleep) -> None:
    """prompt_tokens -> input, completion_tokens -> output,
    prompt_tokens_details.cached_tokens -> cached, summed into the accumulator."""
    t = _bare_translator()
    usage = _fake_usage(prompt_tokens=900, completion_tokens=600, cached=120)
    t._client = _fake_client([_fake_response(_ENVELOPE, usage=usage)])
    await t._call(
        user_prompt="u", system_prompt="s", temperature=0.3, label="translate",
    )
    acc = t._usage_accumulator
    assert acc.input_tokens == 900
    assert acc.output_tokens == 600
    assert acc.cached_input_tokens == 120


@pytest.mark.asyncio
async def test_call_length_finish_reason_raises_transient(_no_sleep) -> None:
    """A truncated response (finish_reason == 'length') raises a transient
    error instead of committing a partial body, and is not retried."""
    t = _bare_translator()
    t._client = _fake_client([_fake_response("half", finish_reason="length")])
    with pytest.raises(TransientTranslatorError, match="truncated"):
        await t._call(
            user_prompt="u", system_prompt="s", temperature=0.3, label="translate",
        )
    assert len(t._client._completions.calls) == 1


@pytest.mark.asyncio
async def test_call_no_choices_raises_value_error(_no_sleep) -> None:
    """An empty choices list is a hard failure (ValueError), surfaced after the
    backoff loop returns the raw response."""
    t = _bare_translator()
    empty = types.SimpleNamespace(choices=[], usage=None)
    t._client = _fake_client([empty])
    with pytest.raises(ValueError, match="no choices"):
        await t._call(
            user_prompt="u", system_prompt="s", temperature=0.3, label="translate",
        )


@pytest.mark.asyncio
async def test_call_retries_transient_then_succeeds(_no_sleep) -> None:
    """A rate-limit on the first create() followed by success returns the body;
    create() is issued exactly twice."""
    t = _bare_translator()
    t._client = _fake_client([_rate_limit_error(), _fake_response(_ENVELOPE)])
    out = await t._call(
        user_prompt="u", system_prompt="s", temperature=0.3, label="translate",
    )
    assert out == _ENVELOPE
    assert len(t._client._completions.calls) == 2


@pytest.mark.asyncio
async def test_call_exhaustion_raises_transient_unavailable(_no_sleep) -> None:
    """Persistent transient errors end in a TransientTranslatorError mentioning
    the backend name and the 'unchanged' chapter, after all attempts."""
    t = _bare_translator()
    attempts = len(BACKOFF_SCHEDULE) + 1
    t._client = _fake_client([_status_error(503) for _ in range(attempts)])
    with pytest.raises(TransientTranslatorError, match="unavailable"):
        await t._call(
            user_prompt="u", system_prompt="s", temperature=0.3, label="translate",
        )
    assert len(t._client._completions.calls) == attempts


def test_class_level_defaults() -> None:
    """The shared base pins the literary defaults every subclass inherits."""
    assert OpenAICompatibleTranslator.TEMPERATURE == 0.3
    assert OpenAICompatibleTranslator.MAX_OUTPUT_TOKENS is None
    assert OpenAICompatibleTranslator.DEFAULT_BASE_URL is None
    assert OpenAICompatibleTranslator.max_parallel == 1
    assert DEFAULT_REQUEST_TIMEOUT == 300.0


def test_init_without_provider_raises() -> None:
    """The base has no env-var fallback: a None provider is a hard error."""
    with pytest.raises(RuntimeError, match="requires an explicit Provider"):
        OpenAICompatibleTranslator(provider=None)
