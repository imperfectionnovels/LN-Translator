"""PostToolUse guard: block disallowed dash glyphs in user-visible authored text.

The project rule (auto-memory `no-em-dashes`) forbids em/en/bar dashes in UI
strings, prompts, comments, and commits. This hook enforces it automatically for
the two highest-risk authored surfaces, the genre/translator prompts and the
frontend, so a stray dash cannot slip in unnoticed during an Edit/Write.

Reads the Claude Code PostToolUse JSON payload on stdin, looks at the edited
file, and if it is under `backend/prompts/` or `frontend/` and contains a dash
glyph, prints the offending lines to stderr and exits 2 (which surfaces the
feedback to the model to fix). Anything else is a silent pass (exit 0). It never
touches the deterministic dash-handling code in backend/services, which contains
the glyphs as literals on purpose.
"""

from __future__ import annotations

import json
import sys

_DASHES = "—–―"  # em, en, horizontal bar


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    fp = (payload.get("tool_input") or {}).get("file_path") or ""
    norm = fp.replace("\\", "/")
    if not ("/backend/prompts/" in norm or "/frontend/" in norm):
        return 0
    if not norm.endswith((".md", ".js", ".html", ".css", ".txt")):
        return 0
    try:
        with open(fp, encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception:
        return 0
    bad = [
        (i + 1, ln.rstrip())
        for i, ln in enumerate(lines)
        if any(c in ln for c in _DASHES)
    ]
    if not bad:
        return 0
    sys.stderr.write(f"EM-DASH GUARD: disallowed dash glyph in {fp}\n")
    for n, ln in bad[:10]:
        sys.stderr.write(f"  line {n}: {ln[:120]}\n")
    sys.stderr.write(
        "Replace with comma, colon, period, parentheses, or a middle-dot.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
