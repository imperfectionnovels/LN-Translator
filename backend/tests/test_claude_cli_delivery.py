"""claude_cli must deliver the literary brief as a REAL system prompt.

Regression guard for the 2026-06-01 fix: previously the genre brief was folded
into the user message ("SYSTEM INSTRUCTIONS:\\n...\\nUSER REQUEST:\\n...") so the
model ran under Claude Code's coding-assistant persona. It now ships via
--system-prompt-file (replaces the persona) with the bare prompt on stdin, run
from a neutral cwd so the project CLAUDE.md is not auto-discovered.
"""

from __future__ import annotations

import json
import subprocess
import tempfile

from backend.services.translators.claude_cli import ClaudeCliTranslator

_SENTINEL_BRIEF = "You are a literary novelist. SENTINEL_BRIEF_MARKER."


class _FakeProc:
    def __init__(self, captured: dict) -> None:
        self._captured = captured
        self.returncode = 0

    def communicate(self, input=None, timeout=None):  # noqa: A002 - mirror stdlib signature
        self._captured["stdin"] = input
        envelope = json.dumps(
            {"type": "result", "result": "OK BODY", "is_error": False}
        )
        return envelope.encode("utf-8"), b""


def _patch_popen(monkeypatch) -> dict:
    captured: dict = {}

    def _fake_popen(args, **kwargs):
        captured["args"] = list(args)
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProc(captured)

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    return captured


def _system_prompt_file_arg(args: list[str]) -> str:
    assert "--system-prompt-file" in args, args
    return args[args.index("--system-prompt-file") + 1]


async def test_complete_ships_brief_as_system_prompt_file(monkeypatch):
    captured = _patch_popen(monkeypatch)
    t = ClaudeCliTranslator()
    t.system_instruction = _SENTINEL_BRIEF

    out = await t._complete("USER PROMPT BODY")

    assert out == "OK BODY"
    # Brief rides as a --system-prompt-file whose file holds the brief verbatim.
    path = _system_prompt_file_arg(captured["args"])
    with open(path, encoding="utf-8") as fh:
        assert "SENTINEL_BRIEF_MARKER" in fh.read()
    # The user prompt is piped bare — no demotion prefix.
    stdin = captured["stdin"].decode("utf-8")
    assert stdin == "USER PROMPT BODY"
    assert "SYSTEM INSTRUCTIONS" not in stdin
    # Neutral cwd so `claude` doesn't auto-load the project's CLAUDE.md.
    assert captured["cwd"] == tempfile.gettempdir()


async def test_complete_plain_also_ships_system_prompt(monkeypatch):
    """The refiner's editor role reaches the CLI via complete_editor_pass, which
    stashes the editor instruction on self.system_instruction before calling
    _complete_plain — so the plain path must deliver it as a system prompt too."""
    captured = _patch_popen(monkeypatch)
    t = ClaudeCliTranslator()
    t.system_instruction = _SENTINEL_BRIEF

    out = await t._complete_plain("EDIT THIS DRAFT")

    assert out == "OK BODY"
    path = _system_prompt_file_arg(captured["args"])
    with open(path, encoding="utf-8") as fh:
        assert "SENTINEL_BRIEF_MARKER" in fh.read()
    assert captured["stdin"].decode("utf-8") == "EDIT THIS DRAFT"


async def test_no_system_prompt_file_when_instruction_empty(monkeypatch):
    captured = _patch_popen(monkeypatch)
    t = ClaudeCliTranslator()
    t.system_instruction = ""

    await t._complete_plain("BARE PROMPT")

    assert "--system-prompt-file" not in captured["args"]


async def test_tools_and_mcp_disabled(monkeypatch):
    """Regression guard (2026-06-02): under Claude Code's default tool access the
    model could emit a tool_use, which with --max-turns 1 leaves no turn to feed
    the result back and hard-fails as subtype=error_max_turns / stop_reason=
    tool_use. The translator never needs a tool, so the CLI must run with every
    built-in tool stripped (--tools "") and no settings / MCP loaded
    (--setting-sources "" + --strict-mcp-config), mirroring claude_agent."""
    captured = _patch_popen(monkeypatch)
    t = ClaudeCliTranslator()
    t.system_instruction = _SENTINEL_BRIEF

    await t._complete("第一章。")

    args = captured["args"]
    # --tools is immediately followed by "" (disable all built-in tools).
    assert "--tools" in args, args
    assert args[args.index("--tools") + 1] == "", args
    # No user/project settings and no MCP servers spawned.
    assert "--setting-sources" in args, args
    assert args[args.index("--setting-sources") + 1] == "", args
    assert "--strict-mcp-config" in args, args
