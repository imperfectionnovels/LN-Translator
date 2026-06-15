"""Error classification for claude_agent's AssistantMessage.error path.

A dropped API socket mid-request surfaces as AssistantMessage.error="unknown"
with the real text ("API Error: The socket connection was closed unexpectedly")
in a TextBlock. That is transient and must RETRY (raised as ConnectionError so
the backoff loop in _call_sdk_with_retry catches it), not hard-fail the chapter
and dead-stop the queue. Auth/billing stay permanent; rate_limit stays a
no-retry transient (wait for the window). The captured detail must ride along so
the failure is diagnosable instead of a bare "unknown".
"""

from __future__ import annotations

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

import backend.services.translators.claude_agent as ca
from backend.services.translators.base import TransientTranslatorError
from backend.services.translators.claude_agent import ClaudeAgentError


def _msg(error: str, text: str = "", stop_reason: str | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)] if text else [],
        model="claude-sonnet-4-6",
        error=error,
        stop_reason=stop_reason,
    )


def _t() -> ca.ClaudeAgentTranslator:
    return ca.ClaudeAgentTranslator(None)


def test_socket_drop_unknown_is_retryable_connectionerror_with_detail():
    msg = _msg("unknown", "API Error: The socket connection was closed unexpectedly", "stop_sequence")
    with pytest.raises(ConnectionError) as ei:
        _t()._raise_assistant_error(msg)
    s = str(ei.value)
    # The real cause and stop_reason are preserved (not a bare "unknown").
    assert "socket connection was closed" in s
    assert "stop_reason=stop_sequence" in s


def test_server_error_is_retryable():
    with pytest.raises(ConnectionError):
        _t()._raise_assistant_error(_msg("server_error"))


def test_connection_drop_text_overrides_otherwise_permanent_label():
    # A normally-permanent label still retries when the text is a connection drop.
    with pytest.raises(ConnectionError):
        _t()._raise_assistant_error(_msg("invalid_request", "API Error: socket hang up"))


def test_auth_and_billing_are_permanent():
    with pytest.raises(ClaudeAgentError):
        _t()._raise_assistant_error(_msg("authentication_failed"))
    with pytest.raises(ClaudeAgentError):
        _t()._raise_assistant_error(_msg("billing_error"))


def test_rate_limit_is_transient():
    with pytest.raises(TransientTranslatorError):
        _t()._raise_assistant_error(_msg("rate_limit"))


def test_invalid_request_without_connection_text_stays_permanent():
    with pytest.raises(ClaudeAgentError):
        _t()._raise_assistant_error(_msg("invalid_request", "model produced an invalid request"))
