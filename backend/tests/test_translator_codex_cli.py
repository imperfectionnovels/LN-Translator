"""Characterization tests for the Codex CLI translator.

The CLI subprocess backends had zero direct coverage despite bespoke
retry + error-classification logic. This pins CodexCliTranslator's
contract by stubbing the subprocess layer (resolve_binary / build_argv /
run_subprocess) so no real `codex` binary is invoked:

  * happy path returns stdout verbatim
  * non-zero exit with auth text -> permanent CodexCliError
  * non-zero exit with rate-limit text -> TransientTranslatorError
  * zero exit but empty stdout -> CodexCliError
  * --model flag is forwarded only for a concrete model_id
  * transient OSError retries per BACKOFF_SCHEDULE then gives up as
    TransientTranslatorError

Same shape applies to the sibling gemini_cli / opencode wrappers.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.services.translators import codex_cli as mod
from backend.services.translators.base import (
    BACKOFF_SCHEDULE,
    TransientTranslatorError,
)
from backend.services.translators.codex_cli import CodexCliError, CodexCliTranslator

pytestmark = pytest.mark.asyncio


def _stub_subprocess(monkeypatch, *, result=None, raises=None, capture=None):
    """Stub the codex_cli subprocess seam. `result` is (rc, stdout, stderr)
    returned by run_subprocess; `raises` is an exception (or list, one per
    call) raised instead. `capture` (a list) records the argv each call."""
    monkeypatch.setattr(mod, "resolve_binary", lambda _b: "codex")
    monkeypatch.setattr(mod, "build_argv", lambda args: args)

    calls = {"n": 0}
    raise_seq = raises if isinstance(raises, list) else None

    async def _fake_run(args, *, stdin_text, timeout_seconds):
        if capture is not None:
            capture.append(args)
        calls["n"] += 1
        if raise_seq is not None:
            idx = calls["n"] - 1
            if idx < len(raise_seq) and raise_seq[idx] is not None:
                raise raise_seq[idx]
        elif raises is not None:
            raise raises
        return result

    monkeypatch.setattr(mod, "run_subprocess", _fake_run)
    return calls


async def test_happy_path_returns_stdout(monkeypatch):
    _stub_subprocess(monkeypatch, result=(0, "Translated body.\n", ""))
    t = CodexCliTranslator()
    out = await t._call("the prompt")
    assert out == "Translated body.\n"


async def test_auth_failure_is_permanent(monkeypatch):
    _stub_subprocess(monkeypatch, result=(1, "", "Error: not logged in"))
    t = CodexCliTranslator()
    with pytest.raises(CodexCliError, match="not authenticated"):
        await t._call("p")


async def test_rate_limit_is_transient(monkeypatch):
    _stub_subprocess(monkeypatch, result=(1, "", "HTTP 429 rate limit exceeded"))
    t = CodexCliTranslator()
    with pytest.raises(TransientTranslatorError, match="rate limit"):
        await t._call("p")


async def test_empty_stdout_is_error(monkeypatch):
    _stub_subprocess(monkeypatch, result=(0, "   ", "some warning"))
    t = CodexCliTranslator()
    with pytest.raises(CodexCliError, match="empty stdout"):
        await t._call("p")


async def test_model_flag_forwarded_for_concrete_model(monkeypatch):
    captured: list = []
    _stub_subprocess(monkeypatch, result=(0, "ok", ""), capture=captured)
    from backend.services.providers import Provider

    provider = Provider(
        id=1, name="codex", provider_type="codex_cli", base_url=None,
        model_id="gpt-5.5", params={}, secret_ref=None, is_default=False,
        last_tested_at=None, created_at="", updated_at="",
    )
    t = CodexCliTranslator(provider=provider)
    await t._call("p")
    assert "--model" in captured[0]
    assert "gpt-5.5" in captured[0]


async def test_default_model_omits_flag(monkeypatch):
    captured: list = []
    _stub_subprocess(monkeypatch, result=(0, "ok", ""), capture=captured)
    t = CodexCliTranslator()
    t.model_id = "default"
    await t._call("p")
    assert "--model" not in captured[0]


async def test_transient_oserror_retries_then_gives_up(monkeypatch):
    # All attempts fail transiently; with sleeps neutralized the loop should
    # exhaust BACKOFF_SCHEDULE and surface TransientTranslatorError.
    calls = _stub_subprocess(monkeypatch, raises=OSError("connection reset"))

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    t = CodexCliTranslator()
    with pytest.raises(TransientTranslatorError, match="temporarily unavailable"):
        await t._call("p")
    # len(BACKOFF_SCHEDULE) + 1 attempts were made before giving up.
    assert calls["n"] == len(BACKOFF_SCHEDULE) + 1


async def test_transient_then_success(monkeypatch):
    # Two transient failures, then a success on the third attempt.
    seq = [OSError("blip"), ConnectionError("blip"), None]
    _stub_subprocess(monkeypatch, result=(0, "recovered", ""), raises=seq)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    t = CodexCliTranslator()
    out = await t._call("p")
    assert out == "recovered"
