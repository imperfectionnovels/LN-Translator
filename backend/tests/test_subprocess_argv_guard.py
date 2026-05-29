"""H3: build_argv rejects shell metacharacters when wrapping a .cmd / .bat shim
in `cmd /c`. cmd.exe re-parses the joined command line, so a metacharacter in an
argument (the model_id is the only user-influenced one) could be interpreted by
cmd even though we pass an argv list. Defense-in-depth; no shell=True is used.
"""

from __future__ import annotations

import pytest

from backend.services.translators import _subprocess_utils as su


def test_build_argv_rejects_cmd_metachars(monkeypatch):
    monkeypatch.setattr(su.os, "name", "nt")
    with pytest.raises(ValueError):
        su.build_argv(["claude.cmd", "--model", "x & calc.exe"])


def test_build_argv_wraps_clean_cmd(monkeypatch):
    monkeypatch.setattr(su.os, "name", "nt")
    out = su.build_argv(["claude.cmd", "--model", "claude-opus-4-8"])
    assert out[:2] == ["cmd", "/c"]
    assert out[2:] == ["claude.cmd", "--model", "claude-opus-4-8"]


def test_build_argv_posix_passthrough(monkeypatch):
    # On POSIX there is no cmd re-parse, so no wrapping and no metachar check.
    monkeypatch.setattr(su.os, "name", "posix")
    assert su.build_argv(["claude", "--model", "x & y"]) == ["claude", "--model", "x & y"]
