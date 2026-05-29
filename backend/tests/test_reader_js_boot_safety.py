"""Static lint on frontend/js/reader.js to catch a recurring TDZ class.

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

This test checks the most common bug-prone shape:

  At module-top level (column 0), any `IDENT?.addEventListener(`
  or `IDENT.addEventListener(` must reference an IDENT that was
  declared (`const IDENT = ...` or `let IDENT = ...`) earlier in
  the file.

If a NEW recurrence has a different shape (e.g. `IDENT.disabled = ...`,
`apply*(IDENT)`), broaden the regex below. Don't generalize
preemptively — false positives on a static lint are worse than the
occasional broadening.
"""
import re
from pathlib import Path


READER_JS = Path(__file__).resolve().parents[2] / "frontend" / "js" / "reader.js"


def test_module_top_level_addEventListener_targets_are_declared_first():
    src = READER_JS.read_text(encoding="utf-8")

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
        "declared later in the file — TDZ ReferenceError at boot. "
        "Hoist the declaration into the top DOM-handles block.\n"
        + "\n".join(
            f"  `{n}`: used at line {ul}, declared at line {dl}"
            for n, ul, dl in offenders
        )
    )


def test_boot_resume_prefers_db_reading_position():
    """The boot resume path must prefer the durable DB position
    (novelMeta.last_read_chapter_num) over the localStorage breadcrumb so
    reopening the app lands on the last-read chapter even when WebView2 has
    discarded localStorage. Guards against a regression that reverts to a
    localStorage-only read."""
    src = READER_JS.read_text(encoding="utf-8")
    assert "novelMeta?.last_read_chapter_num" in src, (
        "boot resume no longer reads novelMeta.last_read_chapter_num — the "
        "durable DB position must be preferred over the localStorage breadcrumb"
    )
