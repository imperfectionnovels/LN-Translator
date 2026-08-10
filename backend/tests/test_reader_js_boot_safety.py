"""Static lint on the reader's and editor's JS modules to catch a recurring
TDZ class.

Two prior boot breakages had the same shape:

  commit 6646cc7: `lastChapter` referenced at module-top-level before its
                    `let` declaration. Throws TDZ at script-parse time.
  commit ????:    `toggleSource?.addEventListener(...)` at module-top-level
                    before `const toggleSource = document.getElementById(...)`.
                    Throws TDZ at script-parse time.

Both bugs froze the reader BEFORE loadIndex() ever ran; the user sees
skeleton TOC rows forever, no chapter content. `?.` does not save you:
optional-chaining short-circuits on null/undefined, but TDZ throws on
the binding LOOKUP, before the operator runs.

The pattern is: a feature block introduces a DOM handle right next to
its handler wiring, but JS top-level execution is strictly sequential
with TDZ for `const`/`let`. If the wiring sits ABOVE the declaration,
boot dies.

reader.js was split into ordered modules (reader-core.js, reader-toc.js, ...).
They are plain classic scripts sharing one global scope and executing in the
order reader.html lists them, so the cross-file picture is exactly the
concatenation in load order. This lint reads the `reader*.js` script order
straight from reader.html, concatenates the files in that order, and runs the
check on the concatenation, which catches both within-file and cross-file
(later module's top-level statement referencing an earlier module's binding is
fine; the reverse is the bug) TDZ violations.

editor.html uses the identical convention (editor-core.js, editor-assist.js,
editor-tools.js) with the identical hazard, so the same lint is parametrized
over both pages below. Each page is parsed for its own script order straight
from its own HTML, so a reordering or an added module on either page is
caught automatically without touching this file.

term-marks.js is deliberately excluded from both concatenations: it is a
self-contained shared module with no page globals (see its own header
comment), loaded before either page's ordered modules, and outside the
shared-scope contract this lint is checking. The exclusion is automatic:
the script-tag regex only matches files whose name starts with the page's
own prefix ("reader" or "editor"), and term-marks.js starts with neither.

If a NEW recurrence has a different shape (e.g. `IDENT.disabled = ...`,
`apply*(IDENT)`), broaden the regex below. Don't generalize preemptively;
false positives on a static lint are worse than the occasional broadening.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
READER_HTML = FRONTEND / "reader.html"
EDITOR_HTML = FRONTEND / "editor.html"

# (page HTML, script-name prefix) pairs sharing the ordered-classic-script,
# one-global-scope convention. Add a new pair here when a third page adopts
# the split-module pattern; everything else derives from it.
_PAGES = [
    pytest.param(READER_HTML, "reader", id="reader"),
    pytest.param(EDITOR_HTML, "editor", id="editor"),
]


def _module_order(html_path: Path, prefix: str) -> list[Path]:
    """The `{prefix}*.js` files in the exact order `html_path` loads them.

    Parsing the HTML (rather than hardcoding) keeps the lint honest: it tests
    what the page actually executes, and a reordered/added module is covered
    automatically. Matching on the page-specific prefix is also what keeps
    term-marks.js (and any other shared, non-page-owned script) out of the
    concatenation: its filename does not start with the prefix, so it never
    matches.
    """
    html = html_path.read_text(encoding="utf-8")
    srcs = re.findall(
        r'<script\s+src="/static/js/(' + re.escape(prefix) + r'[\w-]*\.js)', html
    )
    assert srcs, f"no {prefix}*.js script tags found in {html_path.name}"
    paths = [FRONTEND / "js" / s for s in srcs]
    for p in paths:
        assert p.exists(), f"{html_path.name} loads {p.name} but the file is missing"
    return paths


def _concatenated_src(html_path: Path, prefix: str) -> str:
    """Concatenate a page's ordered modules in load order, mirroring how the
    browser builds one shared global scope across the deferred classic
    scripts."""
    parts = []
    for p in _module_order(html_path, prefix):
        text = p.read_text(encoding="utf-8")
        # Preserve relative line/scope semantics; a trailing newline between
        # files matches the browser treating each as its own top-level program
        # executed in sequence.
        parts.append(text if text.endswith("\n") else text + "\n")
    return "".join(parts)


# Map every `const NAME = ...` / `let NAME = ...` declared at module-top
# level (column 0) to its line number. Indented declarations inside function
# bodies are scoped to those functions and TDZ-safe relative to top-level
# code, so they don't go in this map.
_DECL_PATTERN = re.compile(
    r"^(?:const|let)\s+(\w+)\s*=",
    re.MULTILINE,
)

# Module-top-level `IDENT?.addEventListener(...)` or `IDENT.addEventListener(...)`
# (column-0 only; anything indented is inside a function body or a block and
# runs later, not at parse time).
_USE_PATTERN = re.compile(
    r"^(\w+)\??\.addEventListener\(",
    re.MULTILINE,
)


def _find_tdz_offenders(src: str) -> list[tuple[str, int, int]]:
    """Pure discriminator: given already-concatenated source, return every
    `(name, use_line, decl_line)` where a module-top-level addEventListener
    references a const/let declared LATER in the source. Kept separate from
    file I/O so the detection logic itself can be exercised directly against
    synthetic source in tests, not only against the real modules."""
    decls: dict[str, int] = {}
    for m in _DECL_PATTERN.finditer(src):
        name = m.group(1)
        line_no = src.count("\n", 0, m.start()) + 1
        decls.setdefault(name, line_no)

    offenders: list[tuple[str, int, int]] = []
    for m in _USE_PATTERN.finditer(src):
        name = m.group(1)
        use_line = src.count("\n", 0, m.start()) + 1
        if name in decls and decls[name] > use_line:
            offenders.append((name, use_line, decls[name]))
    return offenders


@pytest.mark.parametrize("html_path, prefix", _PAGES)
def test_module_top_level_addEventListener_targets_are_declared_first(html_path, prefix):
    src = _concatenated_src(html_path, prefix)
    offenders = _find_tdz_offenders(src)

    assert not offenders, (
        f"[{html_path.name}] Module-top-level addEventListener references a "
        "`const`/`let` declared later in the concatenated "
        f"{prefix}*.js modules, TDZ ReferenceError at boot. Hoist the "
        f"declaration into {prefix}-core.js (the first-loaded module owns "
        "the shared DOM handles), or load the referencing module after the "
        "one that declares it.\n"
        + "\n".join(
            f"  `{n}`: used at concat-line {ul}, declared at concat-line {dl}"
            for n, ul, dl in offenders
        )
    )


def test_find_tdz_offenders_flags_forward_reference():
    """Self-test of the discriminator against synthetic source: a
    module-top-level addEventListener that references a binding declared
    later must be flagged, whether or not it uses optional chaining."""
    src = (
        "toggleSource?.addEventListener('click', onToggle);\n"
        "const toggleSource = document.getElementById('toggle-source');\n"
    )
    offenders = _find_tdz_offenders(src)
    assert offenders == [("toggleSource", 1, 2)]


def test_find_tdz_offenders_allows_backward_reference():
    """A declaration that comes before its use (the normal, correct shape)
    must not be flagged."""
    src = (
        "const toggleSource = document.getElementById('toggle-source');\n"
        "toggleSource.addEventListener('click', onToggle);\n"
    )
    assert _find_tdz_offenders(src) == []


def test_find_tdz_offenders_ignores_indented_declarations():
    """A `const`/`let` declared inside a function body is scoped to that
    function and TDZ-safe relative to top-level code; it must not be treated
    as a forward-reference hazard for a same-named top-level use."""
    src = (
        "widget.addEventListener('click', () => {});\n"
        "function setup() {\n"
        "  const widget = document.getElementById('widget');\n"
        "  return widget;\n"
        "}\n"
    )
    assert _find_tdz_offenders(src) == []


def test_boot_resume_prefers_db_reading_position():
    """The boot resume path must prefer the durable DB position
    (novelMeta.last_read_chapter_num) over the localStorage breadcrumb so
    reopening the app lands on the last-read chapter even when WebView2 has
    discarded localStorage. Guards against a regression that reverts to a
    localStorage-only read. Reader-only: the editor has no resume breadcrumb."""
    src = _concatenated_src(READER_HTML, "reader")
    assert "novelMeta?.last_read_chapter_num" in src, (
        "boot resume no longer reads novelMeta.last_read_chapter_num; the "
        "durable DB position must be preferred over the localStorage breadcrumb"
    )
