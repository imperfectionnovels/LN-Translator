"""Direct tests for the Claude SDK/CLI error-string classifier.

`_claude_errors.classify` is a pure substring matcher that maps an opaque
Claude error string into one of 'rate_limit', 'auth', or 'unknown'. Both the
Agent SDK and the CLI wrapper route their stderr/result envelopes through it,
so its precedence (rate-limit wins over auth) and case-insensitivity are
load-bearing. These feed representative strings and assert the bucket.
"""

from __future__ import annotations

import pytest

from backend.services.translators import _claude_errors
from backend.services.translators._claude_errors import classify


@pytest.mark.parametrize(
    "text",
    [
        "You have hit your limit, please try again later",
        "Error: usage limit reached for this account",
        "HTTP 429 rate limit exceeded",
        "ratelimited: slow down",
        "Claude API quota exhausted",
        "Too many requests, back off",
        "You've reached your 5-hour limit",
        "5 hour limit hit",
        "Please try again later.",
    ],
)
def test_rate_limit_strings_classify_as_rate_limit(text: str) -> None:
    """Every cap/throttle phrase the CLI emits buckets to 'rate_limit' so the
    caller surfaces it as a re-queueable TransientTranslatorError."""
    result = classify(text)
    assert result == "rate_limit"
    assert result != "auth"
    assert result != "unknown"


@pytest.mark.parametrize(
    "text",
    [
        "Please run /login to continue",
        "Error: not authenticated",
        "authentication required",
        "Please log in to use Claude",
        "Invalid API key provided",
        "401 Unauthorized",
    ],
)
def test_auth_strings_classify_as_auth(text: str) -> None:
    """Login / bad-key phrases bucket to 'auth', a permanent failure the user
    must fix locally, so it is NOT treated as a transient retry."""
    result = classify(text)
    assert result == "auth"
    assert result != "rate_limit"
    assert result != "unknown"


@pytest.mark.parametrize(
    "text",
    [
        "Some completely unrelated traceback",
        "Connection reset by peer",
        "",
        "translation completed successfully",
    ],
)
def test_unrecognized_strings_classify_as_unknown(text: str) -> None:
    """Anything outside the two known pattern sets falls through to 'unknown'
    so the caller does not mislabel an arbitrary failure."""
    result = classify(text)
    assert result == "unknown"
    assert result not in ("rate_limit", "auth")


def test_rate_limit_takes_precedence_over_auth() -> None:
    """Order matters: some CLI versions mention /login inside a cap message.
    The user's real problem is the cap, so rate-limit must win even when auth
    substrings co-occur in the same string."""
    text = "You hit your limit. Please run /login if this persists."
    # The string contains BOTH a rate-limit phrase ("hit your limit") and an
    # auth phrase ("/login"); the classifier must pick rate_limit.
    assert "hit your limit" in text.lower()
    assert "/login" in text.lower()
    assert classify(text) == "rate_limit"


def test_classification_is_case_insensitive() -> None:
    """Upstream casing varies (UPPERCASE banners, Title-Case prose). The
    classifier lowercases first, so the same phrase classifies identically
    regardless of case."""
    assert classify("USAGE LIMIT REACHED") == "rate_limit"
    assert classify("Not Authenticated") == "auth"
    assert classify("Usage Limit") == classify("usage limit")


def test_none_and_empty_are_unknown() -> None:
    """A None or empty error string is coerced to '' and buckets to 'unknown'
    rather than raising, the classifier must never crash the worker."""
    assert classify(None) == "unknown"  # type: ignore[arg-type]
    assert classify("") == "unknown"
    # Whitespace-only is also unknown (no pattern can match it).
    assert classify("   \n\t  ") == "unknown"


def test_pattern_tables_are_disjoint_and_nonempty() -> None:
    """Sanity-pin the module's two pattern tables: both are populated and no
    substring appears in both lists (which would make precedence meaningless)."""
    rate = set(_claude_errors._RATE_LIMIT_PATTERNS)
    auth = set(_claude_errors._AUTH_PATTERNS)
    assert len(rate) > 0
    assert len(auth) > 0
    assert rate.isdisjoint(auth)
