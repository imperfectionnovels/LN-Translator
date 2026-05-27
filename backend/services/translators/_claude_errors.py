"""Shared classifier for Claude (Agent SDK and CLI) error strings.

Both the SDK and CLI surface upstream failures as opaque strings — the SDK
inside `ProcessError.stderr` / `ResultMessage.errors`, the CLI inside the
subprocess's stdout/stderr envelope. The patterns we look for are the same
in both cases (the CLI is the underlying binary the SDK shells out to),
so a single substring classifier lives here and is imported by
`claude_agent.py` and `claude_cli.py`.

Returns one of: 'rate_limit', 'auth', 'unknown'.
"""

from __future__ import annotations

# Substrings in stderr/result that mean the CLI hit a usage cap. The user
# can't fix this by retrying immediately, but the 5-hour window does reset,
# so callers surface it as TransientTranslatorError so the existing "reset
# transient errors and re-queue" path on /start picks it up.
_RATE_LIMIT_PATTERNS = (
    "usage limit",
    "rate limit",
    "rate-limit",
    "ratelimited",
    "quota",
    "too many requests",
    "5-hour limit",
    "5 hour limit",
    "try again later",
    "limit reached",
    "hit your limit",
)

# Substrings that mean the user isn't logged in. Permanent failure until
# they fix it locally — no point retrying.
_AUTH_PATTERNS = (
    "please run /login",
    "not authenticated",
    "authentication required",
    "please log in",
    "/login",
    "log in to use",
    "invalid api key",
    "unauthorized",
)


def classify(text: str) -> str:
    """Inspect a Claude SDK/CLI error string. Returns 'rate_limit', 'auth',
    or 'unknown'. Order matters: rate-limit responses on some CLI versions
    also mention auth, but the user's actual problem is the cap, not the
    login."""
    lower = (text or "").lower()
    for pat in _RATE_LIMIT_PATTERNS:
        if pat in lower:
            return "rate_limit"
    for pat in _AUTH_PATTERNS:
        if pat in lower:
            return "auth"
    return "unknown"
