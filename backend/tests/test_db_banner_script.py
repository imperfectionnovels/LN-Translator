"""Unit tests for backend/scripts/_db_banner.py.

Pins the "which DB am I writing to" banner string + the typed-confirmation gate
the dev / learn-from-edits scripts share. Under the test conftest, DB_PATH is a
temp file set via the DB_PATH env var, so resolution reports the env override
and the target is NOT the repo dev DB.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "backend" / "scripts"))

import _db_banner  # noqa: E402  static import -> credits backend/scripts/_db_banner.py

from backend.config import DB_PATH  # noqa: E402


def test_resolution_reports_db_path_override():
    # conftest sets the DB_PATH env var, so resolution is the env override branch.
    assert _db_banner._resolution() == "DB_PATH env override"
    assert isinstance(_db_banner._resolution(), str)
    assert "DB_PATH" in _db_banner._resolution()


def test_is_dev_db_false_for_temp_test_db():
    # The temp test DB is not the repo's data/novels.db.
    assert _db_banner._is_dev_db() is False
    assert _db_banner._DEV_DB.name == "novels.db"
    assert _db_banner._DEV_DB.parent.name == "data"


def test_resolution_ln_translator_data_branch(monkeypatch):
    # With DB_PATH unset but LN_TRANSLATOR_DATA set, resolution flips branch.
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.setenv("LN_TRANSLATOR_DATA", "/tmp/whatever")
    assert _db_banner._resolution() == "LN_TRANSLATOR_DATA override"
    # And with neither set, it falls back to the ambient default.
    monkeypatch.delenv("LN_TRANSLATOR_DATA", raising=False)
    assert _db_banner._resolution() == "ambient default"


def test_print_db_banner_readonly(capsys):
    _db_banner.print_db_banner(mutates=False)
    err = capsys.readouterr().err
    # The banner names the resolved DB path and the read-only mode, to stderr.
    assert str(DB_PATH) in err
    assert "read-only" in err
    assert "DB target" in err
    assert "WRITE (this run mutates" not in err
    assert "=" * 64 in err  # the ASCII bar


def test_print_db_banner_write_mode(capsys):
    _db_banner.print_db_banner(mutates=True)
    err = capsys.readouterr().err
    assert "WRITE (this run mutates the DB above)" in err
    assert "read-only" not in err
    # Override/live marker because the test DB is not the repo dev DB.
    assert "NOT the repo dev DB" in err


def test_print_db_banner_uses_stderr_not_stdout(capsys):
    _db_banner.print_db_banner(mutates=False)
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout
    assert "DB target" in captured.err
    assert len(captured.err) > 0


def test_confirm_db_assume_yes_bypasses_prompt(capsys):
    # --yes path proceeds without reading stdin and announces the action.
    result = _db_banner.confirm_db("retranslate chapter 5", assume_yes=True)
    err = capsys.readouterr().err
    assert result is True
    assert "--yes: proceeding to retranslate chapter 5." in err
    assert isinstance(result, bool)


def test_confirm_db_typed_yes_proceeds(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "  YES  ")
    result = _db_banner.confirm_db("write glossary")
    assert result is True
    assert result is not False


def test_confirm_db_non_yes_aborts(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    result = _db_banner.confirm_db("delete rows")
    err = capsys.readouterr().err
    assert result is False
    assert "Not confirmed; nothing written." in err
    # An empty / blank response also aborts.
    monkeypatch.setattr("builtins.input", lambda _prompt: "   ")
    assert _db_banner.confirm_db("delete rows") is False
