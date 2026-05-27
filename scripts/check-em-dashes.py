"""Fail when an em-dash or en-dash appears in user-visible code.

Scope is the surfaces the user actually reads:
  frontend/, backend/routes/, backend/models.py, backend/prompts/

Heuristic exemption: a line that contains a CJK character within 4 chars
of the dash is assumed to be Chinese text (translated passage, test
fixture, or a sample using fullwidth dashes) and is skipped.

Run from repo root:
    python scripts/check-em-dashes.py

Exits 0 when clean, 1 otherwise (with file:line:col reports).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TARGETS = [
    ROOT / "frontend",
    ROOT / "backend" / "routes",
    ROOT / "backend" / "models.py",
    ROOT / "backend" / "prompts",
]
EXTS = {".py", ".js", ".html", ".css", ".md"}
SKIP_DIRS = {"node_modules", "__pycache__", "fixtures", "vendor", ".venv", "venv"}

EM = "—"  # em-dash
EN = "–"  # en-dash


def _is_cjk_neighbor(line: str, col: int) -> bool:
    """True when a CJK char sits within 4 chars of position `col`.

    Used to exempt legitimate Chinese text (test fixtures, sample passages,
    Chinese punctuation runs) from the dash check.
    """
    window = line[max(0, col - 4) : col + 5]
    return any("一" <= ch <= "鿿" for ch in window)


def _scan(path: Path) -> list[tuple[Path, int, int, str, str]]:
    """Return (path, line_num, col_num, char, line_text) for each hit."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    hits = []
    for ln, line in enumerate(text.splitlines(), 1):
        # Per-line exemption marker. Used in the prompts where a literal
        # em-dash is the worked example (e.g. cut-off speech `"You X—"`).
        if "noqa: em-dash" in line:
            continue
        for col, ch in enumerate(line):
            if ch not in (EM, EN):
                continue
            if _is_cjk_neighbor(line, col):
                continue
            hits.append((path, ln, col, ch, line))
    return hits


def _gather_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    out = []
    for p in target.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix not in EXTS:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def main() -> int:
    # Windows defaults stdout to cp1252 for redirected output, which can't
    # encode the line-preview characters we surface (em-dash itself, plus
    # any emoji or CJK that happens to share the line). Force UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    all_hits = []
    for target in TARGETS:
        for path in _gather_files(target):
            all_hits.extend(_scan(path))

    if not all_hits:
        print("EM-DASH CHECK PASS")
        return 0

    for path, ln, col, ch, line in all_hits:
        rel = path.relative_to(ROOT).as_posix()
        kind = "em-dash" if ch == EM else "en-dash"
        preview = line.strip()
        if len(preview) > 100:
            preview = preview[:97] + "..."
        print(f"{rel}:{ln}:{col + 1}: {kind}: {preview}")

    em_count = sum(1 for _, _, _, ch, _ in all_hits if ch == EM)
    en_count = len(all_hits) - em_count
    print()
    print(f"{em_count} em-dash, {en_count} en-dash in user-visible code.")
    print("Replace with comma, colon, period, parentheses, or middle-dot (·).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
