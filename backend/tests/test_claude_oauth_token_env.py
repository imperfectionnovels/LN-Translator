"""Subscription OAuth-token wiring for the Claude backends.

Background: on 2026-06-15 Anthropic moved subscription-plan Agent SDK / `claude -p`
usage onto a separate monthly Agent-SDK credit, gated behind a subscription token
from `claude setup-token` (exposed as CLAUDE_CODE_OAUTH_TOKEN). The claude_cli and
claude_agent backends resolve that token from the provider's keychain secret and
inject it into the CLI subprocess env. They must also never let an ANTHROPIC_API_KEY
leak into that env, or the call would silently bill to pay-as-you-go API credits
instead of the subscription (auth precedence ranks the API key above subscription
OAuth). These tests pin both behaviors.
"""

from __future__ import annotations

from backend.services.providers import Provider
from backend.services.translators._subprocess_utils import (
    claude_sdk_env_overrides,
    claude_subprocess_env,
)


def test_claude_subprocess_env_injects_token_and_strips_api_keys(monkeypatch):
    # claude_cli path: Popen env= fully REPLACES the child env, so we hand it a
    # complete env with the API-key vars removed and the token injected.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gw-should-be-stripped")
    env = claude_subprocess_env("oat-token-123")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oat-token-123"
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    # A full env copy is required for Popen(env=...): PATH/HOME must survive so
    # the CLI can be found and ~/.claude credentials resolve.
    assert "PATH" in env


def test_claude_subprocess_env_no_token_still_strips_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    env = claude_subprocess_env(None)
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env  # nothing injected without a token


def test_claude_sdk_env_overrides_token_only():
    # claude_agent path: the SDK MERGES options.env over the inherited env, so we
    # pass a minimal override (token only) and rely on auth precedence (the token
    # outranks any inherited API key). Empty when no token.
    assert claude_sdk_env_overrides("tok") == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
    assert claude_sdk_env_overrides(None) == {}
    assert claude_sdk_env_overrides("") == {}


def test_catalog_claude_types_carry_subscription_token_ref():
    from backend.services.translator_catalog import get_type, to_api_payload

    for t in ("claude_agent", "claude_cli"):
        entry = get_type(t)
        assert entry is not None
        assert entry.subscription_token_ref == "CLAUDE_CODE_OAUTH_TOKEN", t
    # Other subscription types log in via their own CLI; no token field.
    assert get_type("codex_cli").subscription_token_ref is None
    assert get_type("gemini_cli").subscription_token_ref is None
    # api_key types never carry it (they use secret_ref_hint).
    assert get_type("anthropic_api").subscription_token_ref is None
    # Serialized for the Settings form to consume.
    payload = {e["type"]: e for e in to_api_payload()}
    assert payload["claude_cli"]["subscription_token_ref"] == "CLAUDE_CODE_OAUTH_TOKEN"
    assert payload["codex_cli"]["subscription_token_ref"] is None


def _provider(secret_ref):
    return Provider(
        id=1,
        name="Claude",
        provider_type="claude_cli",
        base_url=None,
        model_id="claude-sonnet-4-6",
        secret_ref=secret_ref,
    )


def test_claude_backends_store_resolved_oauth_token(monkeypatch):
    import backend.services.translators.claude_agent as ca
    import backend.services.translators.claude_cli as cc

    monkeypatch.setattr(cc, "resolve_secret", lambda p: "tok-cli" if p and p.secret_ref else None)
    monkeypatch.setattr(ca, "resolve_secret", lambda p: "tok-agent" if p and p.secret_ref else None)

    p = _provider("CLAUDE_CODE_OAUTH_TOKEN")
    assert cc.ClaudeCliTranslator(p)._oauth_token == "tok-cli"
    assert ca.ClaudeAgentTranslator(p)._oauth_token == "tok-agent"

    p0 = _provider(None)
    assert cc.ClaudeCliTranslator(p0)._oauth_token is None
    assert ca.ClaudeAgentTranslator(p0)._oauth_token is None
    # No provider at all (legacy/global construction) → no token.
    assert cc.ClaudeCliTranslator(None)._oauth_token is None
    assert ca.ClaudeAgentTranslator(None)._oauth_token is None
