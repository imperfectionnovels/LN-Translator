"""Fail when a page's JS dereferences an element ID that its HTML never defines.

The recurring "stale element ID after an HTML refactor" bug class (see
docs/gotchas.md): an `.html` page is restructured, its companion `js/*.js`
keeps referencing the old IDs. Two failure modes:

  1. UNGUARDED `document.getElementById("x").prop` where `x` was renamed/removed
     throws `TypeError: ... of null` at module load and aborts the WHOLE script,
     so every handler below it never binds and the page looks totally dead. This
     is the severe one. It is also unambiguous: an unguarded deref of an ID that
     is neither in the HTML nor created by the JS WILL crash. This gate hard-
     fails on it (exit 1).

  2. GUARDED `if (el) ...` / `el?.prop` of an absent ID silently no-ops. That is
     sometimes a dead feature and sometimes deliberate (e.g. reader.js removes
     legacy "download-*-source" links that only exist in a user's CACHED old
     HTML). Distinguishing the two statically is not reliable, so guarded misses
     are NOT a hard failure. Pass --warn-guarded to list them for a manual audit.

Why this exists as a script (the docs/gotchas.md bash one-liner never became a
gate): the one-liner flags every looked-up ID absent from the HTML, including
the ~11 IDs the JS creates dynamically (`el.id = "x"`, `id="x"` inside template
strings, `setAttribute("id", ...)`). That ~85% false-positive rate forced a
manual "filter out IDs the JS creates" step. This script recognizes dynamic
creation and the guarded/unguarded distinction, so the hard gate is false-
positive-free and safe to run in CI and pre-commit.

Mapping: each `frontend/js/<name>.js` is scored against the union of `id="..."`
across every `frontend/*.html` that `<script src>`-includes it, plus the IDs the
JS itself creates. Shared JS (api.js, utils.js, ...) is scored against the union
of all pages that load it (lenient: errs toward false-negatives, never false-
positives, which is the right bias for a hard gate). IDs built with template
interpolation (`getElementById(`row-${i}`)`) are skipped: not statically
resolvable.

Run from repo root:
    python scripts/check_element_ids.py                 # scans frontend/
    python scripts/check_element_ids.py frontend        # explicit
    python scripts/check_element_ids.py --warn-guarded  # also list guarded misses

Exits 0 when no unguarded misses, 1 otherwise (with file:line reports).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# `<script src="/static/js/<name>.js">` or `src="js/<name>.js"`.
_SCRIPT_SRC_RE = re.compile(r"""src\s*=\s*["'][^"']*?/?js/([\w-]+)\.js""")
# Any `id="x"` / `id='x'` in HTML or in JS template strings. Also matches
# `el.id = "x"` (the `.id =` creation form). Over-matching (e.g. data-id) only
# adds to the "created/available" set, which can never cause a false hard-fail.
_ID_ATTR_RE = re.compile(r"""\bid\s*=\s*["']([\w-]+)["']""")
_SET_ATTR_ID_RE = re.compile(
    r"""setAttribute\(\s*["']id["']\s*,\s*["']([\w-]+)["']\s*\)"""
)
# A lookup, capturing the id and what immediately follows the `)`.
_GET_BY_ID_RE = re.compile(
    r"""getElementById\(\s*["']([\w-]+)["']\s*\)(\s*\??\s*[.\[])?"""
)
_QUERY_ID_RE = re.compile(
    r"""querySelector\(\s*["']#([\w-]+)["']\s*\)(\s*\??\s*[.\[])?"""
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _html_index(frontend: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return (js_name -> set(html ids it can see), js_name -> set(html paths))."""
    js_to_ids: dict[str, set[str]] = {}
    js_to_htmls: dict[str, set[str]] = {}
    for html in sorted(frontend.glob("*.html")):
        text = _read(html)
        ids = set(_ID_ATTR_RE.findall(text))
        for js_name in set(_SCRIPT_SRC_RE.findall(text)):
            js_to_ids.setdefault(js_name, set()).update(ids)
            js_to_htmls.setdefault(js_name, set()).add(html.name)
    return js_to_ids, js_to_htmls


def _created_ids(js_text: str) -> set[str]:
    """IDs the JS creates dynamically (so a lookup of them is not drift)."""
    created = set(_ID_ATTR_RE.findall(js_text))  # `id="x"` in template strings, `.id = "x"`
    created.update(_SET_ATTR_ID_RE.findall(js_text))
    return created


def _is_unguarded(follow: str | None) -> bool:
    """A lookup is unguarded when its result is dereferenced with a bare `.`/`[`.

    `getElementById("x").href` -> crash if absent.  `getElementById("x")?.foo`,
    `const el = getElementById("x")`, and a standalone call are all guarded
    (no crash at the call site).
    """
    if not follow:
        return False
    return "?" not in follow


def _scan_js(js_path: Path, available: set[str]) -> tuple[list[str], list[str]]:
    """Return (unguarded_misses, guarded_misses) as 'rel:line #id' strings."""
    text = _read(js_path)
    available = available | _created_ids(text)
    rel = js_path.relative_to(ROOT).as_posix()
    unguarded: list[str] = []
    guarded: list[str] = []
    for rx in (_GET_BY_ID_RE, _QUERY_ID_RE):
        for m in rx.finditer(text):
            eid = m.group(1)
            if eid in available:
                continue
            entry = f"{rel}:{_line_of(text, m.start())} #{eid}"
            if _is_unguarded(m.group(2)):
                unguarded.append(entry)
            else:
                guarded.append(entry)
    return unguarded, guarded


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    warn_guarded = "--warn-guarded" in sys.argv[1:]
    frontend = Path(args[0]) if args else (ROOT / "frontend")
    if not frontend.is_absolute():
        frontend = ROOT / frontend
    js_dir = frontend / "js"
    if not js_dir.is_dir():
        print(f"ELEMENT-ID CHECK: no js/ under {frontend}", file=sys.stderr)
        return 1

    js_to_ids, _ = _html_index(frontend)

    all_unguarded: list[str] = []
    all_guarded: list[str] = []
    for js_path in sorted(js_dir.glob("*.js")):
        name = js_path.stem
        if name not in js_to_ids:
            continue  # not loaded by any page (e.g. a vendor/util never <script>-ed)
        unguarded, guarded = _scan_js(js_path, js_to_ids[name])
        all_unguarded.extend(unguarded)
        all_guarded.extend(guarded)

    if warn_guarded and all_guarded:
        print("Guarded lookups of IDs absent from HTML (dead feature OR intentional")
        print("legacy/cached-HTML cleanup -- review, not a failure):")
        for entry in all_guarded:
            print(f"  {entry}")
        print()

    if not all_unguarded:
        print("ELEMENT-ID CHECK PASS")
        return 0

    print("UNGUARDED lookups of IDs absent from HTML (will throw at page load and")
    print("kill the whole script). Restore the id in the HTML, rename the lookup,")
    print("or null-guard it (`const el = getElementById(...); if (el) ...`):")
    for entry in all_unguarded:
        print(f"  {entry}")
    print()
    print(f"{len(all_unguarded)} unguarded element-ID miss(es).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
