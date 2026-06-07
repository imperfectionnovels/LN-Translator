"""anthropic_api must send the system prompt as a structured block with
cache_control so the static system instruction is prompt-cached across a
novel's chapters. A regression to a bare `system=<string>` would silently
disable caching (the usage code still reads cache_read_input_tokens, so the
loss would be invisible without this guard).

`anthropic` is an OPTIONAL provider dependency and is not installed in the
test env, so we inject a minimal fake module; the provider only uses it lazily
inside __init__ / _is_transient.
"""

import sys
import types

import pytest

from backend.services.providers import Provider


def _install_fake_anthropic(monkeypatch):
    fake = types.ModuleType("anthropic")
    fake.AsyncAnthropic = lambda **kw: object()
    for name in (
        "APIConnectionError", "APITimeoutError", "RateLimitError",
        "InternalServerError", "APIStatusError",
    ):
        setattr(fake, name, type(name, (Exception,), {}))
    monkeypatch.setitem(sys.modules, "anthropic", fake)


def _make_translator(monkeypatch):
    _install_fake_anthropic(monkeypatch)
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-test")
    from backend.services.translators.anthropic_api import AnthropicApiTranslator

    provider = Provider(
        id=1, name="t", provider_type="anthropic_api", base_url=None,
        model_id="claude-opus-4-5", secret_ref="TEST_ANTHROPIC_KEY",
    )
    tr = AnthropicApiTranslator(provider)
    captured: dict = {}

    class _Resp:
        usage = types.SimpleNamespace(
            input_tokens=10, output_tokens=5, cache_read_input_tokens=0
        )
        stop_reason = "end_turn"
        content = [types.SimpleNamespace(text="hello")]

    class _Messages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _Resp()

    tr._client = types.SimpleNamespace(messages=_Messages())
    return tr, captured


@pytest.mark.asyncio
async def test_system_prompt_carries_ephemeral_cache_control(monkeypatch):
    tr, captured = _make_translator(monkeypatch)
    tr.system_instruction = "SYSTEM PROMPT TEXT"

    out = await tr._complete("user prompt")

    assert out == "hello"
    system = captured["system"]
    assert isinstance(system, list) and len(system) == 1
    assert system[0]["type"] == "text"
    assert system[0]["text"] == "SYSTEM PROMPT TEXT"
    assert system[0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_plain_fallback_sends_no_system(monkeypatch):
    tr, captured = _make_translator(monkeypatch)
    await tr._complete_plain("user prompt")
    assert "system" not in captured
