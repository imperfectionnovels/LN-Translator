"""Model-family thinking-effort policy for the claude_agent backend.

Sonnet/Haiku spiral into runaway extended thinking at high effort (250k+ output
tokens/chapter, blowing the call timeout), so they default to "low"; Opus/Fable/
Mythos calibrate deep thinking well, so they default to "high". An explicit
provider params["effort"] overrides the family default; unknown models fall back
to the global CLAUDE_AGENT_TRANSLATOR_EFFORT.
"""

from __future__ import annotations

import backend.services.translators.claude_agent as ca
from backend.config import CLAUDE_AGENT_TRANSLATOR_EFFORT
from backend.services.providers import Provider


def _prov(model_id: str = "claude-sonnet-4-6", params: dict | None = None) -> Provider:
    return Provider(
        id=1,
        name="Claude Agent SDK",
        provider_type="claude_agent",
        base_url=None,
        model_id=model_id,
        params=params or {},
        secret_ref=None,  # avoid keyring/env lookup in resolve_secret
    )


def test_sonnet_and_haiku_default_to_low():
    assert ca.ClaudeAgentTranslator(_prov("claude-sonnet-4-6"))._effort == "low"
    assert ca.ClaudeAgentTranslator(_prov("claude-haiku-4-5-20251001"))._effort == "low"


def test_opus_fable_mythos_default_to_high():
    assert ca.ClaudeAgentTranslator(_prov("claude-opus-4-8"))._effort == "high"
    assert ca.ClaudeAgentTranslator(_prov("claude-opus-4-6"))._effort == "high"
    assert ca.ClaudeAgentTranslator(_prov("claude-fable-5"))._effort == "high"
    assert ca.ClaudeAgentTranslator(_prov("claude-mythos-5"))._effort == "high"


def test_explicit_params_override_wins_over_family():
    # Power-user override beats the family default, both directions.
    assert ca.ClaudeAgentTranslator(_prov("claude-sonnet-4-6", {"effort": "high"}))._effort == "high"
    assert ca.ClaudeAgentTranslator(_prov("claude-opus-4-8", {"effort": "low"}))._effort == "low"
    # cache key reflects the effort so runs can't collide across levels.
    assert "thinkhigh" in ca.ClaudeAgentTranslator(_prov("claude-sonnet-4-6", {"effort": "high"})).cache_identity()


def test_invalid_override_falls_back_to_family():
    assert ca.ClaudeAgentTranslator(_prov("claude-sonnet-4-6", {"effort": "bogus"}))._effort == "low"
    assert ca.ClaudeAgentTranslator(_prov("claude-opus-4-8", {"effort": "bogus"}))._effort == "high"


def test_override_normalized_case_and_whitespace():
    assert ca.ClaudeAgentTranslator(_prov("claude-opus-4-8", {"effort": "  LOW "}))._effort == "low"


def test_unknown_model_uses_global_default():
    assert ca.ClaudeAgentTranslator(_prov("some-unknown-model"))._effort == CLAUDE_AGENT_TRANSLATOR_EFFORT


def test_no_provider_uses_class_model_family():
    # No provider -> class-default model_id; effort follows its family.
    t = ca.ClaudeAgentTranslator(None)
    assert t._effort == ca._model_family_effort(ca.ClaudeAgentTranslator.model_id)
