"""Regression guard for the claude_agent max_turns bug (2026-06-04).

Extended thinking ("effort") splits a chapter response across more than one SDK
turn (a thinking turn, then the text turn), so a hard max_turns=1 made the SDK
abort thinking models mid-response with "Reached maximum number of turns (1)".
That silently broke the entire claude_agent backend on opus-4-5 (it never
completed a chapter), while opus-4-8 happened to finish within one turn and
slipped through. The fix raises the cap; allowed_tools=[] at the call site keeps
the extra turns safe (no tool loop is possible). Do not set it back to 1.
"""

from backend.services.translators import claude_agent


def test_max_turns_leaves_room_for_thinking():
    # Needs > 1 so a thinking turn plus the text turn both fit. The call site
    # passes allowed_tools=[], so this headroom can never become a tool loop:
    # the model still stops at its own end_turn well before the cap.
    assert claude_agent._MAX_TURNS > 1
