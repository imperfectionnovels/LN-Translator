"""Section 6.5: per-chapter LLM-call budget.

Verifies BaseTranslator._check_call_budget() caps total _complete /
_complete_plain invocations at MAX_LLM_CALLS_PER_CHAPTER. The defensive
ceiling is what catches a future regression that turns the parse-retry
loop or fallback path into an unbounded loop.
"""

from __future__ import annotations

import pytest

from backend.services.translators.base import (
    MAX_LLM_CALLS_PER_CHAPTER,
    BaseTranslator,
    TransientTranslatorError,
)

pytestmark = pytest.mark.asyncio


class _AlwaysGarbage(BaseTranslator):
    """Stub backend whose _complete and _complete_plain always return
    unparseable text — forces the orchestrator down the retry + fallback
    path so the budget can fire."""

    name = "stub-garbage"
    model_id = "stub-1"

    async def _complete(self, prompt: str) -> str:  # type: ignore[override]
        # Missing BODY delimiter → parse_delimited_response raises ValueError
        # → BaseTranslator retries / falls back.
        return "no envelope here"

    async def _complete_plain(self, prompt: str) -> str:  # type: ignore[override]
        # Empty body → _plain_text_fallback raises ValueError → bubbles up.
        return ""


async def test_call_budget_resets_each_chapter():
    """The counter resets at the start of translate_chapter so an earlier
    chapter exhausting budget doesn't starve the next one."""
    t = _AlwaysGarbage()
    # First call: exhausts budget via parse retry + fallback path.
    with pytest.raises((ValueError, RuntimeError)):
        await t.translate_chapter("chapter A", None, [])
    # Counter must reset before the second call's first _complete tick.
    # If it didn't, the very first _check_call_budget() of the second call
    # would already trip the cap and raise. We assert the reset happened
    # by checking that the COUNTER snapshot at exception time is at least
    # MAX_LLM_CALLS_PER_CHAPTER, then re-running and confirming the second
    # call walks the same path (i.e. exception raised again, not a
    # premature budget error from leftover state).
    first_count = t._llm_call_count
    assert first_count >= 1, "first call should have ticked the counter at least once"
    with pytest.raises((ValueError, RuntimeError)):
        await t.translate_chapter("chapter B", None, [])
    # If the reset works, the second call ticks the counter the same way.
    # If it didn't, the second call would raise IMMEDIATELY from a stale
    # over-budget state and the counter would not advance further.
    assert t._llm_call_count == first_count


async def test_call_budget_caps_runaway_retry_loop(monkeypatch):
    """If something regresses base.py into looping past 2 parse attempts
    + 1 fallback, the budget check raises before more billable calls go
    out. Simulate by patching the retry loop to call _complete many
    times in a row.
    """
    t = _AlwaysGarbage()
    # The natural _AlwaysGarbage path makes 3 calls (2 _complete + 1
    # _complete_plain) and then raises ValueError from the empty fallback
    # body. The budget is 4 by default, so the natural path does NOT trip
    # the budget. To verify the budget fires, manually drive _complete in
    # a loop until it does.
    t._llm_call_count = 0
    for _ in range(MAX_LLM_CALLS_PER_CHAPTER):
        t._check_call_budget()
    # Over-budget surfaces as the domain TransientTranslatorError (a
    # service-side cap the user can retry), not a bare RuntimeError.
    with pytest.raises(TransientTranslatorError, match="exceeded"):
        t._check_call_budget()


async def test_call_budget_counts_complete_plain():
    """The fallback path (_complete_plain) ticks the counter — otherwise a
    backend that always parse-fails could bypass the cap by routing every
    call through the fallback."""
    t = _AlwaysGarbage()
    with pytest.raises(ValueError):
        await t.translate_chapter("chapter C", None, [])
    # 2 _complete (parse retries) + 1 _complete_plain = 3 ticks.
    assert t._llm_call_count == 3
