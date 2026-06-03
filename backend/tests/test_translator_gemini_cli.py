"""Characterization tests for the Gemini CLI translator.

Mirrors test_translator_codex_cli: stubs the subprocess seam so no real
`gemini` binary runs, and pins the retry + error-classification contract.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.services.translators import gemini_cli as mod
from backend.services.translators.base import (
    BACKOFF_SCHEDULE,
    TransientTranslatorError,
)
from backend.services.translators.gemini_cli import (
    GeminiCliError,
    GeminiCliTranslator,
)

pytestmark = pytest.mark.asyncio


def _stub_subprocess(monkeypatch, *, result=None, raises=None, capture=None):
    monkeypatch.setattr(mod, "resolve_binary", lambda _b: "gemini")
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
    assert await GeminiCliTranslator()._call("p") == "Body.\n"


async def test_auth_failure_is_permanent(monkeypatch):
    _stub_subprocess(monkeypatch, result=(1, "", "please login first"))
    with pytest.raises(GeminiCliError, match="not authenticated"):
        await GeminiCliTranslator()._call("p")


async def test_quota_is_transient(monkeypatch):
    _stub_subprocess(monkeypatch, result=(1, "", "RESOURCE_EXHAUSTED: quota exceeded"))
    with pytest.raises(TransientTranslatorError, match="quota"):
        await GeminiCliTranslator()._call("p")


async def test_empty_stdout_is_error(monkeypatch):
    _stub_subprocess(monkeypatch, result=(0, "", ""))
    with pytest.raises(GeminiCliError, match="empty stdout"):
        await GeminiCliTranslator()._call("p")


async def test_model_flag_forwarded(monkeypatch):
    captured: list = []
    _stub_subprocess(monkeypatch, result=(0, "ok", ""), capture=captured)
    t = GeminiCliTranslator()
    t.model_id = "gemini-2.5-flash"
    await t._call("p")
    assert "-m" in captured[0] and "gemini-2.5-flash" in captured[0]


async def test_transient_oserror_retries_then_gives_up(monkeypatch):
    calls = _stub_subprocess(monkeypatch, raises=TimeoutError("slow"))

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    with pytest.raises(TransientTranslatorError, match="temporarily unavailable"):
        await GeminiCliTranslator()._call("p")
    assert calls["n"] == len(BACKOFF_SCHEDULE) + 1
