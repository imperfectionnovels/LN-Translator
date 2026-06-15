"""Per-provider thinking-effort for the claude_agent backend.

effort=high makes Sonnet 4.6 spiral into runaway extended thinking (250k+ output
tokens/chapter) that blows the call timeout. Effort is therefore resolved
per-provider from `provider.params["effort"]`, falling back to the global
CLAUDE_AGENT_TRANSLATOR_EFFORT default — so a Sonnet provider can run lower than
the Opus-tuned global default without changing the shipped default.
"""

from __future__ import annotations

import backend.services.translators.claude_agent as ca
from backend.config import CLAUDE_AGENT_TRANSLATOR_EFFORT
from backend.services.providers import Provider


def _prov(params: dict) -> Provider:
    return Provider(
        id=1,
        name="Claude Agent SDK",
        provider_type="claude_agent",
        base_url=None,
        model_id="claude-sonnet-4-6",
        params=params,
        secret_ref=None,  # avoid keyring/env lookup in resolve_secret
    )


def test_effort_from_provider_params_overrides_global():
    t = ca.ClaudeAgentTranslator(_prov({"effort": "low"}))
    assert t._effort == "low"
    # cache key must reflect it so a low-effort run can't collide with a
    # high-effort cache entry for the same model.
    assert "thinklow" in t.cache_identity()


def test_effort_case_and_whitespace_normalized():
    t = ca.ClaudeAgentTranslator(_prov({"effort": "  LOW "}))
    assert t._effort == "low"


def test_invalid_effort_falls_back_to_global():
    t = ca.ClaudeAgentTranslator(_prov({"effort": "bogus"}))
    assert t._effort == CLAUDE_AGENT_TRANSLATOR_EFFORT


def test_absent_params_use_global_default():
    assert ca.ClaudeAgentTranslator(_prov({}))._effort == CLAUDE_AGENT_TRANSLATOR_EFFORT
    # No-provider construction (legacy/global path) also uses the global default.
    assert ca.ClaudeAgentTranslator(None)._effort == CLAUDE_AGENT_TRANSLATOR_EFFORT
