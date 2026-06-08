"""Unit tests for scripts/dash_hook.py (the PostToolUse em-dash guard).

The hook reads a Claude Code PostToolUse JSON payload on stdin and exits 2 when
the just-authored text for a prompt/frontend file contains a disallowed dash
glyph, exit 0 otherwise. We drive main() by feeding crafted payloads on stdin.
"""

from __future__ import annotations

import io
import json
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

import dash_hook  # noqa: E402  static import -> credits scripts/dash_hook.py

EM = "—"  # em-dash
EN = "–"  # en-dash
BAR = "―"  # horizontal bar


def _run(monkeypatch, payload) -> int:
    """Feed a payload (dict -> JSON, or raw bytes) on stdin and run main()."""
    if isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    else:
        raw = json.dumps(payload).encode("utf-8")
    # The hook only touches sys.stdin.buffer.read(); a lightweight stub exposing
    # a .buffer with that one method is enough (TextIOWrapper.buffer is readonly).
    stdin_stub = types.SimpleNamespace(buffer=io.BytesIO(raw))
    monkeypatch.setattr(sys, "stdin", stdin_stub)
    return dash_hook.main()


def test_dashes_constant_covers_all_three_glyphs():
    assert EM in dash_hook._DASHES
    assert EN in dash_hook._DASHES
    assert BAR in dash_hook._DASHES
    assert len(dash_hook._DASHES) == 3


def test_blocks_em_dash_in_prompt_edit(monkeypatch, capsys):
    # The hook matches "/backend/prompts/" with leading separators, mirroring the
    # absolute file_path Claude Code emits in a PostToolUse payload.
    payload = {
        "tool_input": {
            "file_path": "/repo/backend/prompts/base.md",
            "new_string": f"first line ok\nsecond line has {EM} dash\n",
        }
    }
    rc = _run(monkeypatch, payload)
    err = capsys.readouterr().err
    assert rc == 2
    assert "EM-DASH GUARD" in err
    assert "+2:" in err  # the offending line number


def test_blocks_write_content_in_frontend(monkeypatch, capsys):
    payload = {
        "tool_input": {
            "file_path": "/repo/frontend/js/reader.js",
            "content": f"const x = 'a {EN} b';\n",
        }
    }
    rc = _run(monkeypatch, payload)
    err = capsys.readouterr().err
    assert rc == 2
    assert "/repo/frontend/js/reader.js" in err
    assert "disallowed dash glyph" in err


def test_clean_authored_text_passes(monkeypatch, capsys):
    payload = {
        "tool_input": {
            "file_path": "/repo/backend/prompts/base.md",
            "new_string": "use a comma, a colon: or parentheses (here).\nhyphen-ok too\n",
        }
    }
    rc = _run(monkeypatch, payload)
    err = capsys.readouterr().err
    assert rc == 0
    assert err == ""
    assert "GUARD" not in err


def test_out_of_scope_path_is_silent_pass(monkeypatch, capsys):
    # A dash in backend/services is intentional literal code, not gated.
    payload = {
        "tool_input": {
            "file_path": "/repo/backend/services/text_fixups.py",
            "new_string": f"DASH = '{EM}'\n",
        }
    }
    rc = _run(monkeypatch, payload)
    assert rc == 0
    assert capsys.readouterr().err == ""


def test_in_scope_but_unwatched_extension_passes(monkeypatch):
    # backend/prompts path but a .py file -> extension not in the watched set.
    payload = {
        "tool_input": {
            "file_path": "/repo/backend/prompts/loader.py",
            "new_string": f"x = '{EM}'\n",
        }
    }
    rc = _run(monkeypatch, payload)
    assert rc == 0
    # Same path with a watched extension WOULD trip, proving the ext gate matters.
    payload["tool_input"]["file_path"] = "/repo/backend/prompts/x.md"
    assert _run(monkeypatch, payload) == 2


def test_windows_backslash_path_is_normalized(monkeypatch, capsys):
    payload = {
        "tool_input": {
            "file_path": r"C:\repo\backend\prompts\genres\xianxia.md",
            "new_string": f"cut off speech {EM}\n",
        }
    }
    rc = _run(monkeypatch, payload)
    err = capsys.readouterr().err
    assert rc == 2
    assert "xianxia.md" in err


def test_missing_authored_text_passes(monkeypatch):
    # In-scope file but neither new_string nor content present -> nothing to check.
    payload = {"tool_input": {"file_path": "/repo/frontend/index.html"}}
    assert _run(monkeypatch, payload) == 0


def test_malformed_stdin_is_swallowed(monkeypatch):
    # Non-JSON bytes must not raise; the hook returns 0 (silent pass).
    rc = _run(monkeypatch, b"this is not json {{{")
    assert rc == 0
    assert isinstance(rc, int)
