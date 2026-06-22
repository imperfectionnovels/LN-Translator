"""Protect the element-ID drift guardrail itself (scripts/check_element_ids.py).

A guardrail that silently stops detecting is worse than none. These tests pin
the three behaviors the gate depends on: the guarded/unguarded distinction, the
dynamic-creation recognition that keeps it false-positive-free, and an end-to-end
HTML+JS scan that proves an unguarded miss is caught while safe shapes are not.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _ROOT / "scripts" / "check_element_ids.py"

_spec = importlib.util.spec_from_file_location("check_element_ids", _SCRIPT)
cei = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cei)


def test_is_unguarded_distinguishes_bare_deref_from_optional_chain():
    assert cei._is_unguarded(".") is True
    assert cei._is_unguarded("[") is True
    assert cei._is_unguarded("?.") is False  # optional chaining cannot crash
    assert cei._is_unguarded(None) is False  # standalone call / assignment
    assert cei._is_unguarded("") is False


def test_created_ids_recognizes_all_dynamic_creation_forms():
    js = """
      const card = document.createElement("div");
      card.id = "refinement-banner";
      el.innerHTML = `<button id="genres-retry-btn">Retry</button>`;
      node.setAttribute("id", "cockpit-preview");
    """
    created = cei._created_ids(js)
    assert {"refinement-banner", "genres-retry-btn", "cockpit-preview"} <= created


def test_scan_flags_unguarded_miss_only(tmp_path):
    js = tmp_path / "page.js"
    js.parent  # keep relative-to(ROOT) working by writing under ROOT-independent path
    # _scan_js calls relative_to(ROOT); write through a path under ROOT instead.
    target = _ROOT / "frontend" / "js" / "_test_scratch.js"
    target.write_text(
        "\n".join(
            [
                'document.getElementById("present").href = "x";',  # present -> ok
                'document.getElementById("made").textContent = "y";',  # created below -> ok
                'document.getElementById("made").id = "made";',
                'document.getElementById("ghost").href = "z";',  # UNGUARDED miss -> fail
                'const e = document.getElementById("legacy"); if (e) e.remove();',  # guarded miss
            ]
        ),
        encoding="utf-8",
    )
    try:
        unguarded, guarded = cei._scan_js(target, available={"present"})
        assert any("#ghost" in u for u in unguarded)
        assert all("#legacy" not in u for u in unguarded)  # guarded not a hard fail
        assert all("#made" not in u for u in unguarded)  # dynamic creation recognized
        assert any("#legacy" in g for g in guarded)
    finally:
        target.unlink()


def test_html_index_maps_script_includes_to_ids(tmp_path):
    (tmp_path / "page.html").write_text(
        '<div id="alpha"></div><span id="beta"></span>'
        '<script src="/static/js/page.js"></script>',
        encoding="utf-8",
    )
    js_to_ids, js_to_htmls = cei._html_index(tmp_path)
    assert js_to_ids["page"] == {"alpha", "beta"}
    assert "page.html" in js_to_htmls["page"]


def test_real_frontend_tree_has_no_unguarded_misses():
    """The committed tree must stay clean -- this is the regression the gate guards."""
    frontend = _ROOT / "frontend"
    js_to_ids, _ = cei._html_index(frontend)
    misses = []
    for js_path in sorted((frontend / "js").glob("*.js")):
        if js_path.stem not in js_to_ids:
            continue
        unguarded, _ = cei._scan_js(js_path, js_to_ids[js_path.stem])
        misses.extend(unguarded)
    assert misses == [], f"unguarded element-ID misses present: {misses}"
