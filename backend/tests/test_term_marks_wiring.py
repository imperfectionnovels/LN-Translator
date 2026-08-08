"""Term-marks wiring pins (2026-08-07 glossary/editor/reader integration).

Structural pins for the shared frontend/js/term-marks.js module and its
consumption by the reader and the CAT editor: glossary terms render as
inline "term" spans on both surfaces (a WTR-Lab-style click-to-revise
highlight), built by ONE matcher so the two surfaces agree on what counts
as a term hit. Static string/regex checks over the frontend files only; no
browser, no DOM. Each test pins one policy:

  1. Load order: the shared module loads (deferred) BEFORE each page's own
     module chain, on both pages.
  2. Self-containment contract: the module owns its own escaping and touches
     no page global and no DOM API, which is WHY it can load unmodified on
     both pages and sits outside the reader*.js boot-safety concat.
  3. Pattern engine: alias-aware slash-split, cached on array identity,
     longest-first alternation, EN lookaround boundaries + case-insensitive,
     the raw/sanitized apostrophe split, the tag-split technique that keeps
     already-sanitized HTML intact.
 3b. Matcher BEHAVIOR (bugs I1/I2, 2026-08-08), executed in node when node
     is on PATH and skipped otherwise: apostrophe terms must match on both
     haystacks and resolve to the RIGHT entry, non-word-edge aliases must
     match at all, and ordinary aliases must be untouched by either fix.
  4. Reader threading: renderers thread the built pattern through, the
     toggle persists to localStorage, and the g-chord conflict fix (plain
     `g` retired; the palette's `g g` owns glossary nav) is pinned by the
     ABSENCE of the old branch.
  5. Editor threading: marks render in both grid cells but never enter the
     contenteditable surface (startEdit reseeds from the plain segment
     field); the source-side click handler is scoped to .seg-src only.
  6. Position handoff (Block 5): reader <-> editor paragraph/segment
     round-trip, both directions.
  7. Dead-end fixes (Block 6): the missing-locked tier keeps its term id,
     assist glossary chips are real buttons, the glossary page links into
     the editor and no longer hard-codes ch=1.
  8. Cross-tab bus (Block 2): the shared BroadcastChannel helpers + honest
     toast copy, referenced by both pages.
  9. ?v cache-bust discipline for a file that was deleted and re-created
     during the CAT pivot (the re-created-file poisoning rule: never reuse
     a ?v value git history already used for that file).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
JS = FRONTEND / "js"
CSS = FRONTEND / "css"


def _script_srcs(html: str) -> list[str]:
    """Every /static/js/... script src in document order, ?v= included."""
    return re.findall(r'<script[^>]*\ssrc="/static/js/([^"]+)"', html)


def _index_of_prefix(srcs: list[str], prefix: str) -> int:
    for i, s in enumerate(srcs):
        if s.startswith(prefix):
            return i
    raise AssertionError(f"no script src starting with {prefix!r} found in {srcs!r}")


def test_term_marks_loads_before_page_modules_on_both_pages():
    reader_html = (FRONTEND / "reader.html").read_text(encoding="utf-8")
    editor_html = (FRONTEND / "editor.html").read_text(encoding="utf-8")

    # One shared, unmodified module: both pages must reference the SAME
    # ?v= (a cache-bust on one page without the other would mean the two
    # surfaces could disagree about which build of the matcher is live),
    # deferred like the rest of the ordered chains. The ?v value itself
    # isn't pinned to a literal integer here; see the dedicated version-
    # discipline test below for the "never reuse a used value" rule.
    reader_m = re.search(r'<script src="/static/js/term-marks\.js\?v=(\d+)" defer></script>', reader_html)
    editor_m = re.search(r'<script src="/static/js/term-marks\.js\?v=(\d+)" defer></script>', editor_html)
    assert reader_m, "reader.html must load the shared term-marks.js deferred"
    assert editor_m, "editor.html must load the shared term-marks.js deferred"
    assert reader_m.group(1) == editor_m.group(1), (
        "reader.html and editor.html must reference the SAME term-marks.js "
        "?v= (it is one shared, unmodified module)"
    )

    reader_srcs = _script_srcs(reader_html)
    editor_srcs = _script_srcs(editor_html)
    assert _index_of_prefix(reader_srcs, "term-marks.js") < _index_of_prefix(reader_srcs, "reader-core.js"), (
        "term-marks.js must load before reader-core.js: renderChapterBody "
        "reads TermMarks at render time"
    )
    assert _index_of_prefix(editor_srcs, "term-marks.js") < _index_of_prefix(editor_srcs, "editor-core.js"), (
        "term-marks.js must load before editor-core.js: rowHtml reads "
        "TermMarks at render time"
    )


def test_term_marks_is_self_contained():
    """Self-containment is WHY the module can sit outside the reader*.js
    boot-safety concat (test_reader_js_boot_safety.py discovers reader*.js
    only) and load unmodified on both pages: it must never read a page
    global or touch the DOM."""
    src = (JS / "term-marks.js").read_text(encoding="utf-8")
    assert "const TermMarks" in src
    # The module's own header comment NAMES these globals to explain that it
    # avoids them ("touches no page global (glossaryCache, currentData,
    # novelId, escapeHtml)..."); strip that block comment first so the pin
    # checks the CODE, not the prose describing the code.
    code = src.split("*/", 1)[1] if "*/" in src else src
    for forbidden in ("glossaryCache", "currentData", "novelId", "escapeHtml(", "getElementById"):
        assert forbidden not in code, (
            f"term-marks.js must stay self-contained (own esc(), no DOM): "
            f"found {forbidden!r} in the code, which would break sharing it "
            f"unmodified between the reader and the editor"
        )


def test_term_marks_pattern_engine():
    src = (JS / "term-marks.js").read_text(encoding="utf-8")
    # Alias-aware: slash-split both term_zh and term_en before building the
    # alternation, so "筑基 / 築基" style entries match either surface form.
    assert "function aliases(s)" in src
    assert 'split("/")' in src
    # Cached on the entries ARRAY REFERENCE identity (not deep-equality):
    # every producer replaces the array wholesale, so this is safe and cheap.
    assert "_cacheKey === entries" in src
    # Longest-first alternation ACROSS entries (not per-entry), so a long
    # alias of one entry can beat a short alias of another.
    assert "enList.sort((a, b) => b.length - a.length);" in src
    assert "zhList.sort((a, b) => b.length - a.length);" in src
    # EN side: boundary-anchored and case-insensitive. The anchors are
    # explicit LOOKAROUNDS, not \b (bug I2, 2026-08-08): \b inverts its test
    # at a non-word edge, so it demands that the neighboring character BE a
    # word character, which prose never supplies. Any alias whose first or
    # last character sits outside [A-Za-z0-9_] ("Su Nü", "talent(s)", an
    # alias ending in ".") could therefore never fire, silently.
    assert "(?<![A-Za-z0-9_])" in src
    assert "(?![A-Za-z0-9_])" in src
    assert "\\b(" not in src, (
        "the EN union must not go back to \\b anchors: they cannot match an "
        "alias with a non-word first/last character (bug I2)"
    )
    assert '"gi"' in src
    # Apostrophe tolerance (bug I1, 2026-08-08). One alias faces three
    # surfaces: the stored straight ', the curly U+2019 prose actually uses,
    # and the &#39; entity marked writes into the sanitized haystack. TWO
    # compiled EN variants, built from the SAME sorted alias list so
    # longest-first ordering is identical on either haystack; the entity form
    # stays OUT of the raw variant because raw chapter text can legitimately
    # contain the literal characters "&#39;".
    assert "const APOS_RAW =" in src
    assert "const APOS_HTML =" in src
    assert "&#39;" in src
    assert "function aliasFragment(a, aposAlt)" in src
    assert "const en = enList.length ? enRegex(APOS_RAW) : null;" in src
    assert "const enHtml = enList.length ? enRegex(APOS_HTML) : null;" in src
    assert "pattern.enHtml || pattern.en" in src, (
        "wrapSanitizedHtml must run the entity-aware EN variant"
    )
    # Lookup consistency: the byEn KEY and the MATCHED surface both normalize
    # through one helper, so a hit spelled with a curly apostrophe or with
    # &#39; resolves to the entry whose term_en used the straight one. Without
    # this the longest alias matches and then fails to look up, and the
    # shorter sibling ("Saint" under "Saint's Plunder") takes the mark.
    assert "function enKey(s)" in src
    assert "const k = enKey(a);" in src
    assert "pattern.byEn.get(enKey(matched))" in src
    # The recovered tag-split technique: split on tags, regex only the text
    # pieces, so <strong>/<em>/<br> markup from marked+DOMPurify survives.
    assert "split(/(<[^>]+>)/)" in src
    # The escaping asymmetry stays: raw path escapes on the way out, the
    # already-escaped path inserts the matched run verbatim.
    assert "out.push(entry ? span(entry, esc(matched)) : esc(matched));" in src
    assert "out.push(entry ? span(entry, matched) : matched);" in src
    # Same exported API, still cached on array identity.
    assert "return { buildPattern, wrapText, wrapSanitizedHtml, invalidate };" in src


# --- Matcher behavior (bugs I1 / I2, 2026-08-08) -------------------------
#
# The rest of this module is static string pins, which is the right tool for
# "is the wiring still hooked up". It is the WRONG tool for "does the union
# actually match this string", and the two bugs fixed on 2026-08-08 were both
# invisible to a grep: a term never matched, or matched and resolved to the
# WRONG entry (which points the revise dialog at a different glossary row, and
# a rename from there rewrites the wrong term novel-wide).
#
# So these run the real module in node. term-marks.js is self-contained by
# contract (no imports, no page globals, no DOM), which is exactly what makes
# it loadable in a bare JS engine: `new Function(src + "return TermMarks")`.
# node is NOT assumed present. When it is missing these skip and the static
# pins above still hold the shape of the fix.

_ENTRIES = [
    {"id": 1, "term_zh": "圣", "term_en": "Saint", "category": "concept"},
    {"id": 2, "term_zh": "圣掠", "term_en": "Saint's Plunder", "category": "technique"},
    {"id": 3, "term_zh": "素女", "term_en": "Su Nü", "category": "person"},
    {"id": 4, "term_zh": "天赋", "term_en": "talent(s)", "category": "concept"},
    {"id": 5, "term_zh": "金丹", "term_en": "Golden Core", "category": "concept"},
]


def _run_term_marks(tmp_path: Path, body: str) -> list[str]:
    """Load the real term-marks.js in node and print one JSON line per case.

    `body` is JS with `TM` (the module) and `P` (a pattern built over
    _ENTRIES) in scope, and calls `emit(value)` per assertion subject.
    """
    node = shutil.which("node")
    if not node:  # pragma: no cover - environment dependent
        pytest.skip("node not on PATH; the static pattern-engine pins still apply")
    harness = tmp_path / "harness.js"
    harness.write_text(
        "const fs = require('fs');\n"
        f"const src = fs.readFileSync({json.dumps(str(JS / 'term-marks.js'))}, 'utf8');\n"
        "const TM = new Function(src + '\\nreturn TermMarks;')();\n"
        f"const ENTRIES = {json.dumps(_ENTRIES, ensure_ascii=False)};\n"
        "const P = TM.buildPattern(ENTRIES);\n"
        "const emit = v => console.log(JSON.stringify(v));\n"
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(harness)],
        # DEVNULL, not inherit: pytest's capture replaces stdin with a handle
        # Windows refuses to duplicate into a child (WinError 6).
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def test_apostrophe_term_matches_sanitized_haystack_and_wins_over_shorter_sibling(tmp_path):
    """Bug I1. marked escapes ' to &#39;, so the sanitized haystack never
    contained the character the union was built from: "Saint's Plunder" could
    not match in the reader, and longest-first ordering then handed the mark
    to the shorter sibling "Saint" (id 1). Wrong tooltip, and clicking opened
    the revise dialog on the wrong glossary entry."""
    out = _run_term_marks(tmp_path, """
        emit(TM.wrapSanitizedHtml('<p>He drew Saint&#39;s Plunder again.</p>', P));
        emit(TM.wrapSanitizedHtml('<p>The Saint watched.</p>', P));
        emit(TM.wrapSanitizedHtml('<p>A &amp; B</p>', P));
    """)
    marked, sibling, amp = out

    assert 'data-entry-id="2"' in marked, (
        "the apostrophe term must match the &#39; haystack and resolve to "
        "entry 2 (Saint's Plunder), not to its shorter sibling"
    )
    assert 'data-entry-id="1"' not in marked
    assert ">Saint&#39;s Plunder</span>" in marked, (
        "the sanitized path must re-insert the matched run VERBATIM: it is "
        "already escaped, so re-escaping would produce &amp;#39;"
    )
    # The shorter sibling still matches on its own, so this is a fix, not a
    # suppression of the "Saint" entry.
    assert 'data-entry-id="1"' in sibling
    # And the path still never double-escapes.
    assert amp == "<p>A &amp; B</p>"


def test_curly_apostrophe_prose_matches_straight_apostrophe_term(tmp_path):
    """Bug I1, raw side. Glossary rows store the straight ', prose normally
    carries the curly U+2019, so an apostrophe term missed the editor's
    segment cells and the reader's raw path too."""
    out = _run_term_marks(tmp_path, """
        emit(TM.wrapText('He drew Saint\\u2019s Plunder again.', 'en', P));
        emit(TM.wrapText("He drew Saint's Plunder again.", 'en', P));
    """)
    curly, straight = out
    for rendered in (curly, straight):
        assert 'data-entry-id="2"' in rendered
        assert 'data-entry-id="1"' not in rendered
    # The rendered span keeps the PROSE's own apostrophe, not the glossary's.
    assert ">Saint’s Plunder</span>" in curly
    assert ">Saint&#39;s Plunder</span>" in straight, (
        "the raw path still escapes on the way out"
    )


def test_non_word_edge_aliases_match_in_plain_prose(tmp_path):
    """Bug I2. Under \\b anchors an alias whose first or last character is
    outside [A-Za-z0-9_] could effectively never match: after "Su Nü" the
    trailing \\b wanted the NEXT character to be a word character, which
    prose does not supply. 29 live entries were affected, silently."""
    out = _run_term_marks(tmp_path, """
        emit(TM.wrapText('Su N\\u00fc arrived at dawn.', 'en', P));
        emit(TM.wrapText('Her talent(s) were rare.', 'en', P));
        emit(TM.wrapSanitizedHtml('<p>Su N\\u00fc arrived.</p>', P));
    """)
    su_nu, talents, su_nu_html = out
    assert 'data-entry-id="3"' in su_nu
    assert 'data-entry-id="4"' in talents
    assert ">talent(s)</span>" in talents
    assert 'data-entry-id="3"' in su_nu_html


def test_ordinary_word_edge_aliases_are_unchanged(tmp_path):
    """No-regression pin. At a word-character edge the lookarounds run
    exactly \\b's test, so every ordinary alias must behave as before:
    matched whole-word, case-insensitively, never inside a longer word."""
    out = _run_term_marks(tmp_path, """
        emit(TM.wrapText('Golden Core cultivator.', 'en', P));
        emit(TM.wrapText('GoldenCore cultivator.', 'en', P));
        emit(TM.wrapText('Saints everywhere.', 'en', P));
        emit(TM.wrapText('A saint appeared.', 'en', P));
        emit(TM.wrapText('\\u4ed6\\u7ed3\\u4e86\\u91d1\\u4e39\\u3002', 'zh', P));
        emit(TM.wrapText('A & B <tag>', 'en', P));
        emit(Object.keys(TM).sort().join(','));
        emit(TM.buildPattern(ENTRIES) === P);
    """)
    (matched, glued, plural, lowered, zh, specials, api, cached) = out

    assert matched == (
        '<span class="term" data-entry-id="5" title="金丹 ↔ Golden Core '
        '· concept">Golden Core</span> cultivator.'
    )
    assert glued == "GoldenCore cultivator.", "no match inside a longer word"
    assert plural == "Saints everywhere.", (
        "a trailing word character still blocks the match, exactly as \\b did"
    )
    assert 'data-entry-id="1"' in lowered, "matching stays case-insensitive"
    assert ">saint</span>" in lowered, "the span keeps the prose's own casing"
    assert 'data-entry-id="5"' in zh, "the zh side is unanchored and unchanged"
    assert specials == "A &amp; B &lt;tag&gt;", "the raw path still escapes output"
    assert api == "buildPattern,invalidate,wrapSanitizedHtml,wrapText"
    assert cached is True, "buildPattern still caches on array identity"


def test_reader_threads_term_marks_and_toggle():
    chapter = (JS / "reader-chapter.js").read_text(encoding="utf-8")
    assert "TermMarks.buildPattern(glossaryCache)" in chapter
    assert '_buildAlignedRows(ch.original_text || "", enSource, termPattern)' in chapter
    assert "readerTermMarks" in chapter

    html = (FRONTEND / "reader.html").read_text(encoding="utf-8")
    assert '<input id="term-marks-toggle" type="checkbox">' in html

    # g-chord conflict fix: the reader's plain `g`/`G` glossary shortcut is
    # gone (it shadowed the command palette's `g g` chord app-wide). Pin the
    # ABSENCE of the removed branch by its distinctive body (recovered from
    # git history: `git log -p -S'"g"'` shows the exact fragment removed).
    assert 'e.key === "g" || e.key === "G"' not in chapter
    assert "location.href = glossaryLink.href;" not in chapter

    # The help dialog's glossary row now reads "g g", not a single "g".
    assert (
        '<div class="key-cell"><span class="kbd">g</span> <span class="kbd">g</span></div>'
        "<div>Open glossary</div>"
    ) in html


def test_editor_marks_never_enter_contenteditable():
    core = (JS / "editor-core.js").read_text(encoding="utf-8")
    assert core.count("TermMarks.wrapText") >= 2, (
        "rowHtml must wrap both .seg-src (zh) and .seg-tgt (en) via TermMarks"
    )
    # startEdit reseeds the live cell from the PLAIN segment field, not the
    # marked-up rowHtml string, so a term span can never end up inside the
    # contenteditable surface.
    assert "cell.textContent = seg.target_text;" in core

    tools = (JS / "editor-tools.js").read_text(encoding="utf-8")
    # Delegated click is scoped to the SOURCE side only (memoQ/Trados act on
    # source terms); .seg-tgt keeps meaning "start editing" in editor-core.
    assert ".seg-src .term" in tools

    css = (CSS / "editor.css").read_text(encoding="utf-8")
    assert ".seg-tgt .term { cursor: text; }" in css


def test_position_handoff_both_directions():
    core = (JS / "editor-core.js").read_text(encoding="utf-8")
    assert 'params.get("para")' in core
    assert "segIndexForDisplayedOrdinal" in core
    assert "displayedOrdinalForSeg" in core
    assert 'id="continue-read-link"' in core

    reader_core = (JS / "reader-core.js").read_text(encoding="utf-8")
    assert "pendingDeepPara" in reader_core

    chapter = (JS / "reader-chapter.js").read_text(encoding="utf-8")
    assert "href += `&para=${idx}`;" in chapter


def test_dead_end_fixes():
    tools = (JS / "editor-tools.js").read_text(encoding="utf-8")
    # Missing-locked tier keeps the server's term_id (was discarded before),
    # so the row's revise button can resolve the entry.
    assert "data-term-id" in tools
    assert "missing-locked-edit" in tools
    assert "This term's source text was not found in this chapter's segments." in tools

    assist = (JS / "editor-assist.js").read_text(encoding="utf-8")
    assert 'class="assist-term${present' in assist
    assert 'data-entry-id="${g.id}"' in assist

    glossary = (JS / "glossary.js").read_text(encoding="utf-8")
    assert "/editor?novel=" in glossary
    assert "&ch=1" not in glossary, (
        "glossary.js must not hard-code ch=1: the breadcrumb resumes the "
        "reader's last-read chapter (spine.js convention) instead"
    )


def test_cross_tab_bus_and_shared_toast_copy():
    utils = (JS / "utils.js").read_text(encoding="utf-8")
    for needed in (
        "broadcastNovelChange", "onNovelChange", "glossaryApplyToastText", '"glossary"',
    ):
        assert needed in utils, f"utils.js missing the shared cross-tab bus piece: {needed!r}"

    reader_html = (FRONTEND / "reader.html").read_text(encoding="utf-8")
    editor_html = (FRONTEND / "editor.html").read_text(encoding="utf-8")
    assert "utils.js?v=2" in reader_html
    assert "utils.js?v=2" in editor_html


def test_reader_glossary_version_bumped_past_history():
    html = (FRONTEND / "reader.html").read_text(encoding="utf-8")
    m = re.search(r"reader-glossary\.js\?v=(\d+)", html)
    assert m, "reader.html must reference reader-glossary.js with a ?v= integer"
    assert int(m.group(1)) >= 4, (
        "reader-glossary.js was deleted and re-created during the CAT pivot; "
        "its ?v must be a never-before-used integer (git history already "
        "used 1 and 3), never reset to 1"
    )
