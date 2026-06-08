"""PostToolUse guard: block disallowed dash glyphs in user-visible authored text.

The project rule (auto-memory `no-em-dashes`) forbids em/en/bar dashes in UI
strings, prompts, comments, and commits. This hook enforces it automatically for
the two highest-risk authored surfaces, the genre/translator prompts and the
frontend, so a stray dash cannot slip in unnoticed during an Edit/Write.

Reads the Claude Code PostToolUse JSON payload on stdin, looks at the text the
tool just authored (the Edit's `new_string` or the Write's `content`, not the
whole file), and if the target is under `backend/prompts/` or `frontend/` and
that authored text contains a dash glyph, prints the offending lines to stderr
and exits 2 (which surfaces the feedback to the model to fix). Anything else is a
silent pass (exit 0). Scoping to the authored text means a stray dash the model
writes is still caught, but pre-existing comment or regex glyphs already in a
file (and the deterministic dash-handling code in backend/services, which carries
the glyphs as literals on purpose) do not retroactively block every later edit.
"""

from __future__ import annotations

import json
import sys

_DASHES = "—–―"  # em, en, horizontal bar


def main() -> int:
    try:
        # Read stdin as raw bytes and decode UTF-8 explicitly. On Windows the
        # default stdin codec is cp1252, which misdecodes the UTF-8 bytes of CJK
        # source text in the prompt examples: e.g. 门 (E9 97 A8) has a 0x97 byte
        # that cp1252 maps to U+2014 (em-dash), falsely tripping the guard.
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return 0
    tool_input = payload.get("tool_input") or {}
    fp = tool_input.get("file_path") or ""
    norm = fp.replace("\\", "/")
    if not ("/backend/prompts/" in norm or "/frontend/" in norm):
        return 0
    if not norm.endswith((".md", ".js", ".html", ".css", ".txt")):
        return 0
    # Inspect only the text this call authored (Edit.new_string / Write.content),
    # never the pre-existing file body. A dash the model just wrote is caught;
    # comment or regex glyphs already in the file do not block unrelated edits.
    authored = tool_input.get("new_string")
    if authored is None:
        authored = tool_input.get("content")
    if not authored:
        return 0
    bad = [
        (i + 1, ln.rstrip())
        for i, ln in enumerate(authored.splitlines())
        if any(c in ln for c in _DASHES)
    ]
    if not bad:
        return 0
    sys.stderr.write(f"EM-DASH GUARD: disallowed dash glyph in authored edit to {fp}\n")
    for n, ln in bad[:10]:
        sys.stderr.write(f"  +{n}: {ln[:120]}\n")
    sys.stderr.write(
        "Replace with comma, colon, period, parentheses, or a middle-dot.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
