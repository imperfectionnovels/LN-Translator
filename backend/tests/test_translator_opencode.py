"""Characterization tests for the OpenCode translator.

Mirrors test_translator_codex_cli: stubs the subprocess seam so no real
`opencode` binary runs, and pins the retry + error-classification contract.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.services.translators import opencode as mod
from backend.services.translators.base import (
    BACKOFF_SCHEDULE,
    TransientTranslatorError,
)
from backend.services.translators.opencode import OpenCodeError, OpenCodeTranslator

pytestmark = pytest.mark.asyncio


def _stub_subprocess(monkeypatch, *, result=None, raises=None, capture=None):
    monkeypatch.setattr(mod, "resolve_binary", lambda _b: "opencode")
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
    _stub_subprocess(monkeypatch, result=(0, "Body.\n", ""))
    assert await OpenCodeTranslator()._call("p") == "Body.\n"


async def test_auth_failure_is_permanent(monkeypatch):
    _stub_subprocess(monkeypatch, result=(1, "", "not authenticated for provider"))
    with pytest.raises(OpenCodeError, match="not authenticated"):
        await OpenCodeTranslator()._call("p")


async def test_rate_limit_is_transient(monkeypatch):
    _stub_subprocess(monkeypatch, result=(1, "", "upstream 429 rate limit"))
    with pytest.raises(TransientTranslatorError, match="rate limit"):
        await OpenCodeTranslator()._call("p")


async def test_empty_stdout_is_error(monkeypatch):
    _stub_subprocess(monkeypatch, result=(0, "  \n ", ""))
    with pytest.raises(OpenCodeError, match="empty stdout"):
        await OpenCodeTranslator()._call("p")


async def test_namespaced_model_flag_forwarded(monkeypatch):
    captured: list = []
    _stub_subprocess(monkeypatch, result=(0, "ok", ""), capture=captured)
    t = OpenCodeTranslator()
    t.model_id = "github-copilot/gpt-5"
    await t._call("p")
    assert "--model" in captured[0] and "github-copilot/gpt-5" in captured[0]


async def test_transient_oserror_retries_then_gives_up(monkeypatch):
    calls = _stub_subprocess(monkeypatch, raises=ConnectionError("reset"))

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    with pytest.raises(TransientTranslatorError, match="temporarily unavailable"):
        await OpenCodeTranslator()._call("p")
    assert calls["n"] == len(BACKOFF_SCHEDULE) + 1
