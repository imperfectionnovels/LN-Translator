"""Static lint on the reader's JS modules to catch a recurring TDZ class.

Two prior boot breakages had the same shape:

  commit 6646cc7 — `lastChapter` referenced at module-top-level before its
                    `let` declaration. Throws TDZ at script-parse time.
  commit ????    — `toggleSource?.addEventListener(...)` at module-top-level
                    before `const toggleSource = document.getElementById(...)`.
                    Throws TDZ at script-parse time.

Both bugs froze the reader BEFORE loadIndex() ever ran — the user sees
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
check on the concatenation — which catches both within-file and cross-file
(later module's top-level statement referencing an earlier module's binding is
fine; the reverse is the bug) TDZ violations.

If a NEW recurrence has a different shape (e.g. `IDENT.disabled = ...`,
`apply*(IDENT)`), broaden the regex below. Don't generalize preemptively —
false positives on a static lint are worse than the occasional broadening.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
READER_HTML = FRONTEND / "reader.html"


def _reader_module_order() -> list[Path]:
    """The reader-*.js files in the exact order reader.html loads them.

    Parsing the HTML (rather than hardcoding) keeps the lint honest: it tests
    what the page actually executes, and a reordered/added module is covered
    automatically.
    """
    html = READER_HTML.read_text(encoding="utf-8")
    srcs = re.findall(r'<script\s+src="/static/js/(reader[\w-]*\.js)', html)
    assert srcs, "no reader*.js script tags found in reader.html"
    paths = [FRONTEND / "js" / s for s in srcs]
    for p in paths:
        assert p.exists(), f"reader.html loads {p.name} but the file is missing"
    return paths


def _concatenated_reader_src() -> str:
    """Concatenate the reader modules in load order, mirroring how the browser
    builds one shared global scope across the deferred classic scripts."""
    parts = []
    for p in _reader_module_order():
        text = p.read_text(encoding="utf-8")
        # Preserve relative line/scope semantics; a trailing newline between
        # files matches the browser treating each as its own top-level program
        # executed in sequence.
        parts.append(text if text.endswith("\n") else text + "\n")
    return "".join(parts)


def test_module_top_level_addEventListener_targets_are_declared_first():
    src = _concatenated_reader_src()

    # Map every `const NAME = ...` / `let NAME = ...` declared at module-top
    # level (column 0) to its line number. Indented declarations inside
    # function bodies are scoped to those functions and TDZ-safe relative
    # to top-level code, so they don't go in this map.
    decl_pattern = re.compile(
        r"^(?:const|let)\s+(\w+)\s*=",
        re.MULTILINE,
    )
    decls: dict[str, int] = {}
    for m in decl_pattern.finditer(src):
        name = m.group(1)
        line_no = src.count("\n", 0, m.start()) + 1
        decls.setdefault(name, line_no)

    # Find module-top-level `IDENT?.addEventListener(...)` or
    # `IDENT.addEventListener(...)` (column-0 only — anything indented is
    # inside a function body or a block and runs later, not at parse time).
    use_pattern = re.compile(
        r"^(\w+)\??\.addEventListener\(",
        re.MULTILINE,
    )
    offenders: list[tuple[str, int, int]] = []
    for m in use_pattern.finditer(src):
        name = m.group(1)
        use_line = src.count("\n", 0, m.start()) + 1
        if name in decls and decls[name] > use_line:
            offenders.append((name, use_line, decls[name]))

    assert not offenders, (
        "Module-top-level addEventListener references a `const`/`let` "
        "declared later in the concatenated reader modules — TDZ "
        "ReferenceError at boot. Hoist the declaration into reader-core.js "
        "(the first-loaded module owns the shared DOM handles), or load the "
        "referencing module after the one that declares it.\n"
        + "\n".join(
            f"  `{n}`: used at concat-line {ul}, declared at concat-line {dl}"
            for n, ul, dl in offenders
        )
    )


def test_boot_resume_prefers_db_reading_position():
    """The boot resume path must prefer the durable DB position
    (novelMeta.last_read_chapter_num) over the localStorage breadcrumb so
    reopening the app lands on the last-read chapter even when WebView2 has
    discarded localStorage. Guards against a regression that reverts to a
    localStorage-only read."""
    src = _concatenated_reader_src()
    assert "novelMeta?.last_read_chapter_num" in src, (
        "boot resume no longer reads novelMeta.last_read_chapter_num — the "
        "durable DB position must be preferred over the localStorage breadcrumb"
    )
