"""Direct tests for the shared CLI-subprocess translator base.

`SubprocessCliTranslator` owns the identical parts of the codex_cli / gemini_cli
/ opencode wrappers: the one-at-a-time semaphore, the SYSTEM/USER prompt
framing, and the BACKOFF_SCHEDULE transient-retry loop. The only per-backend
difference is `_run` (argv + error classification), which subclasses implement.

These drive a tiny concrete subclass whose `_run` returns or raises queued
outcomes, with `asyncio.sleep` monkeypatched to a no-op so backoff is instant.
No real binary is invoked.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.services.providers import Provider
from backend.services.translators._cli_base import SubprocessCliTranslator
from backend.services.translators.base import (
    BACKOFF_SCHEDULE,
    TransientTranslatorError,
)


class _StubCliError(Exception):
    """Stand-in for a subclass's permanent_error type."""


class _StubCli(SubprocessCliTranslator):
    """Concrete subclass that records each prompt passed to `_run` and replays
    a queued list of outcomes (a str is returned, an exception is raised)."""

    name = "stubcli"
    permanent_error = _StubCliError
    unavailable_message = "stub CLI temporarily unavailable, retry later"

    def __init__(self, provider=None, *, outcomes=None) -> None:
        super().__init__(provider=provider)
        self._outcomes = list(outcomes or [])
        self.run_prompts: list[str] = []

    async def _run(self, prompt: str) -> str:
        self.run_prompts.append(prompt)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def _no_sleep(monkeypatch):
    async def _instant(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


def _provider(model_id: str | None = "claude-x") -> Provider:
    return Provider(
        id=3,
        name="stub-cli",
        provider_type="codex_cli",
        base_url=None,
        model_id=model_id or "",
        params={},
        secret_ref=None,
        is_default=False,
        last_tested_at=None,
        created_at="",
        updated_at="",
    )


# ===========================================================================
# Construction / model wiring
# ===========================================================================


def test_provider_model_id_is_adopted() -> None:
    """A provider with a model_id sets it on the instance; the per-call
    semaphore is created with capacity 1 (force-serial)."""
    t = _StubCli(provider=_provider("gpt-5.5"))
    assert t.model_id == "gpt-5.5"
    assert isinstance(t._semaphore, asyncio.Semaphore)
    assert t._semaphore._value == 1


def test_no_provider_keeps_class_default_model() -> None:
    """With no provider, model_id stays the class default (empty BaseTranslator
    value) and a semaphore is still created."""
    t = _StubCli()
    assert t.model_id == ""
    assert isinstance(t._semaphore, asyncio.Semaphore)


def test_class_level_defaults() -> None:
    """The base ships a generic permanent_error and an unavailable message that
    subclasses override."""
    assert SubprocessCliTranslator.permanent_error is Exception
    assert "temporarily unavailable" in SubprocessCliTranslator.unavailable_message
    assert SubprocessCliTranslator.unavailable_message != ""


# ===========================================================================
# Prompt framing (_complete / _complete_plain)
# ===========================================================================


@pytest.mark.asyncio
async def test_complete_frames_system_and_user(_no_sleep) -> None:
    """`_complete` wraps the prompt in the SYSTEM INSTRUCTIONS / USER REQUEST
    frame, injecting the per-call system_instruction."""
    t = _StubCli(outcomes=["translated"])
    t.system_instruction = "You are a literary translator."
    out = await t._complete("Render this chapter.")
    assert out == "translated"
    sent = t.run_prompts[0]
    assert "SYSTEM INSTRUCTIONS:\nYou are a literary translator." in sent
    assert "USER REQUEST:\nRender this chapter." in sent


@pytest.mark.asyncio
async def test_complete_plain_passes_prompt_through(_no_sleep) -> None:
    """`_complete_plain` sends the bare prompt, no SYSTEM/USER framing, for
    the plain-text fallback path."""
    t = _StubCli(outcomes=["plain out"])
    t.system_instruction = "should not appear"
    out = await t._complete_plain("just translate this")
    assert out == "plain out"
    sent = t.run_prompts[0]
    assert sent == "just translate this"
    assert "SYSTEM INSTRUCTIONS" not in sent


# ===========================================================================
# Retry loop (_call_with_retry)
# ===========================================================================


@pytest.mark.asyncio
async def test_happy_path_single_run(_no_sleep) -> None:
    """A successful `_run` returns its value with exactly one invocation."""
    t = _StubCli(outcomes=["the body"])
    out = await t._call("p")
    assert out == "the body"
    assert len(t.run_prompts) == 1


@pytest.mark.asyncio
async def test_permanent_error_is_not_retried(_no_sleep) -> None:
    """The subclass's permanent_error propagates on the first attempt and is
    never retried."""
    t = _StubCli(outcomes=[_StubCliError("bad model"), "unreached"])
    with pytest.raises(_StubCliError, match="bad model"):
        await t._call("p")
    # Only the first outcome was consumed, no retry.
    assert len(t.run_prompts) == 1


@pytest.mark.asyncio
async def test_transient_oserror_retries_then_gives_up(_no_sleep) -> None:
    """Persistent OSError exhausts BACKOFF_SCHEDULE then surfaces a
    TransientTranslatorError carrying the subclass's unavailable_message,
    chained from the last OSError."""
    attempts = len(BACKOFF_SCHEDULE) + 1
    t = _StubCli(outcomes=[OSError("connection reset") for _ in range(attempts)])
    with pytest.raises(TransientTranslatorError, match="temporarily unavailable"):
        await t._call("p")
    assert len(t.run_prompts) == attempts
    # The last OSError is preserved as the cause for diagnostics.


@pytest.mark.asyncio
async def test_transient_then_success(_no_sleep) -> None:
    """Two transient failures (timeout then connection) then a success: three
    invocations, the third's value returns."""
    t = _StubCli(
        outcomes=[asyncio.TimeoutError(), ConnectionError("blip"), "recovered"]
    )
    out = await t._call("p")
    assert out == "recovered"
    assert len(t.run_prompts) == 3


@pytest.mark.asyncio
async def test_unavailable_message_chains_last_exception(_no_sleep) -> None:
    """On exhaustion the raised TransientTranslatorError uses the subclass
    message and __cause__ is the final transient exception."""
    attempts = len(BACKOFF_SCHEDULE) + 1
    t = _StubCli(outcomes=[ConnectionError("down") for _ in range(attempts)])
    with pytest.raises(TransientTranslatorError) as exc_info:
        await t._call("p")
    assert str(exc_info.value) == "stub CLI temporarily unavailable, retry later"
    assert isinstance(exc_info.value.__cause__, ConnectionError)


@pytest.mark.asyncio
async def test_base_run_is_not_implemented() -> None:
    """The base `_run` is abstract-by-convention: calling it raises
    NotImplementedError so a subclass that forgets to override fails loudly."""
    t = SubprocessCliTranslator()
    with pytest.raises(NotImplementedError):
        await t._run("anything")
