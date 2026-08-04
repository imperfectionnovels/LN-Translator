"""Phase 6 (reader edit-mode retirement) wiring pins.

The reader is a pure reading surface and the CAT editor is the single editing
surface. These are static checks over the frontend files plus a route-table
check over the FastAPI app:

  1. `?mode=edit` reader URLs redirect to /editor (reader-core.js).
  2. The reader ships no edit-mode chrome (no .edit-only scope, no mode
     toggle, no aligned grid / terms / consistency rails) and loads exactly
     the four surviving modules; the retired modules are gone from disk.
  3. The editor util menu carries the relocated tools and editor-tools.js
     consumes the same api wrappers the reader used (the backend routes are
     unchanged, just re-consumed).
  4. The edit-paragraph write path is fully gone (no route, no api wrapper);
     the segment PATCH is the only paragraph write.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
JS = FRONTEND / "js"


def test_mode_edit_redirects_to_editor():
    src = (JS / "reader-core.js").read_text(encoding="utf-8")
    assert 'params.get("mode") === "edit"' in src, (
        "reader-core.js lost the legacy ?mode=edit detection"
    )
    assert re.search(r"location\.replace\(\s*\n?\s*`/editor\?novel=", src), (
        "?mode=edit must redirect to /editor (legacy bookmarks / worklists)"
    )


def test_reader_has_no_edit_mode_chrome():
    html = (FRONTEND / "reader.html").read_text(encoding="utf-8")
    for marker in (
        "edit-only",
        "reader-mode-toggle",
        "data-reader-mode",
        "aligned-body",
        "terms-rail",
        "consistency-rail",
        'id="edit-mode"',
        "learn-edits-dialog",
        "observations-dialog",
        "concordance-dialog",
        "insert-chapter-dialog",
        "style-note-dialog",
        "attempts-dialog",
        "last-prompt-dialog",
    ):
        assert marker not in html, f"reader.html still carries edit chrome: {marker}"
    # The retired modules are deleted, not just unreferenced.
    for gone in ("reader-edit.js", "reader-glossary.js", "reader-consistency.js"):
        assert not (JS / gone).exists(), f"{gone} should be deleted (Phase 6)"
        assert gone not in html
    # Exactly the four surviving reader modules, in order.
    srcs = re.findall(r'<script\s+src="/static/js/(reader[\w-]*\.js)', html)
    assert srcs == [
        "reader-core.js",
        "reader-toc.js",
        "reader-chapter.js",
        "reader-quality.js",
    ]
    # Reading chrome the reader must keep.
    for keep in (
        'id="toggle-dual"', 'id="toggle-source"', 'id="quality-badge"',
        'id="bookmark-add"', 'id="bookmarks-open"', 'id="retranslate"',
        'id="open-in-editor"', 'id="download-epub"',
    ):
        assert keep in html, f"reader.html lost reading chrome: {keep}"


def test_reader_css_carries_no_mode_scope():
    css = (FRONTEND / "css" / "reader.css").read_text(encoding="utf-8")
    for marker in ("data-reader-mode", ".edit-only", "data-terms", "data-consistency",
                   "data-aligned", ".sel-pop", ".term-edit-pop", ".edit-mode-hint"):
        assert marker not in css, f"reader.css still styles edit chrome: {marker}"


def test_editor_util_menu_and_tools_wiring():
    html = (FRONTEND / "editor.html").read_text(encoding="utf-8")
    for eid in (
        "editor-util-menu", "editor-observations", "editor-view-attempts",
        "editor-view-last-prompt", "editor-precheck", "editor-learn-edits",
        "editor-concordance", "editor-style-note", "editor-refresh-free-draft",
        "editor-insert-chapter", "assist-missing-locked", "assist-chapter-terms",
        "assist-add-term", "term-form-dialog",
        # Pull-based observation recheck (gap audit 2026-08-04).
        "observations-recheck",
    ):
        assert f'id="{eid}"' in html, f"editor.html missing Phase 6 element #{eid}"
    assert "editor-tools.js" in html

    tools = (JS / "editor-tools.js").read_text(encoding="utf-8")
    for wrapper in (
        "api.chapterObservations", "api.dismissObservation",
        # QA panel refresh path (gap audit 2026-08-04): recheck on open, on
        # the Re-check button, and debounced after segment saves.
        "api.recheckChapterObservations",
        "api.chapterAttempts", "api.chapterLastPrompt", "api.chapterPreCheck",
        "api.refreshFreeDraft", "api.insertChapter", "api.tmConcordance",
        "api.learnEditsStage", "api.learnEditsCommit", "api.updateNovel",
        "api.createGlossary", "api.updateGlossary", "api.glossaryApplyInPlace",
        # Missing-locked tier consumes the SERVER's narrowed glossary tier
        # (atomic_only); a naive all-locked client matcher was reverted as a
        # false-fire regression (review fix, see decisions.md 2026-08-03).
        "api.getChapterConsistency",
    ):
        assert wrapper in tools, f"editor-tools.js does not call {wrapper}"
    # The tier asks for the flags-only fast path (no whole-novel corpus
    # build / fuzzy matcher server-side; the editor discards matches anyway).
    assert "getChapterConsistency(novelId, forCh, true)" in tools, (
        "the missing-locked tier must request flags_only"
    )
    api_src = (JS / "api.js").read_text(encoding="utf-8")
    assert "flags_only=1" in api_src, (
        "api.getChapterConsistency lost its flags_only fast-path param"
    )


def test_edit_paragraph_write_path_is_gone():
    api_js = (JS / "api.js").read_text(encoding="utf-8")
    assert "edit-paragraph" not in api_js
    assert "editParagraph" not in api_js

    # Assert against the sub-routers (the sibling *_routes tests' pattern),
    # not backend.main's assembled app: the app object is shared full-suite
    # state and reading it here proved order-fragile on CI.
    from backend.routes import chapters as chapters_route
    from backend.routes import observations as observations_route

    paths = {r.path for r in chapters_route.router.routes}
    assert not any(p.endswith("/edit-paragraph") for p in paths), (
        "the edit-paragraph route must stay deleted; the segment PATCH is "
        "the single paragraph write path"
    )
    # The util-menu endpoints the editor re-consumes are unchanged.
    for suffix in ("/attempts", "/last-prompt", "/pre-check",
                   "/learn-edits", "/learn-edits/commit"):
        assert any(p.endswith(suffix) for p in paths), f"route {suffix} missing"
    obs_paths = {r.path for r in observations_route.router.routes}
    assert any(p.endswith("/observations") for p in obs_paths), (
        "observations route missing"
    )
    assert any(p.endswith("/observations/recheck") for p in obs_paths), (
        "observations recheck route missing (gap audit 2026-08-04)"
    )
