"""Unit tests for the read-only SQLite MCP server's pure helpers
(`tools/sqlite_ro_mcp.py`). Importing it is side-effect free; the DB path is
resolved per test via `_configure()` against a real temp SQLite DB.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

import pytest

# Static import (side-effect free since arg parsing moved into main()).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tools"))
import sqlite_ro_mcp  # noqa: E402


@pytest.fixture
def ro_db(tmp_path):
    db = tmp_path / "ro.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        "CREATE TABLE widget (id INTEGER PRIMARY KEY, name TEXT, qty INTEGER);"
        "INSERT INTO widget VALUES (1,'alpha',10),(2,'beta',20);"
    )
    con.commit()
    con.close()
    sqlite_ro_mcp._configure(str(db))
    return db


def test_guard_allows_read_statements():
    assert sqlite_ro_mcp._guard("SELECT 1") is None
    assert sqlite_ro_mcp._guard("  with x as (select 1) select * from x") is None
    assert sqlite_ro_mcp._guard("EXPLAIN SELECT 1") is None
    assert sqlite_ro_mcp._guard("PRAGMA table_info(widget)") is None


def test_guard_rejects_writes_and_ddl():
    assert sqlite_ro_mcp._guard("INSERT INTO widget VALUES (3,'g',1)") is not None
    assert sqlite_ro_mcp._guard("UPDATE widget SET qty=0") is not None
    assert sqlite_ro_mcp._guard("DELETE FROM widget") is not None
    assert sqlite_ro_mcp._guard("DROP TABLE widget") is not None


def test_guard_rejects_multistatement_and_nonintrospection_pragma():
    assert sqlite_ro_mcp._guard("SELECT 1; DROP TABLE widget") is not None
    assert sqlite_ro_mcp._guard("PRAGMA writable_schema=ON") is not None
    # introspection pragmas remain allowed
    assert sqlite_ro_mcp._guard("PRAGMA foreign_key_list(widget)") is None


def test_configure_sets_module_globals(tmp_path):
    db = tmp_path / "x.db"
    sqlite_ro_mcp._configure(str(db), name="custom")
    assert sqlite_ro_mcp.SERVER_NAME == "custom"
    assert sqlite_ro_mcp.DB_PATH.endswith("x.db")
    sqlite_ro_mcp._configure(str(db))
    assert sqlite_ro_mcp.SERVER_NAME.startswith("sqlite-ro:")


def test_connect_reads_but_write_is_blocked(ro_db):
    con = sqlite_ro_mcp._connect()
    rows = con.execute("SELECT name, qty FROM widget ORDER BY id").fetchall()
    assert [r["name"] for r in rows] == ["alpha", "beta"]
    assert rows[0]["qty"] == 10
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO widget VALUES (3,'gamma',1)")
    con.close()


def test_connect_unconfigured_raises(monkeypatch):
    monkeypatch.setattr(sqlite_ro_mcp, "DB_PATH", None)
    with pytest.raises(RuntimeError):
        sqlite_ro_mcp._connect()


def test_rows_to_json_clamps_limit_and_serializes(ro_db):
    con = sqlite_ro_mcp._connect()
    out = sqlite_ro_mcp._rows_to_json(con.execute("SELECT * FROM widget"), limit=1)
    data = json.loads(out)
    assert len(data) == 1
    assert data[0]["name"] == "alpha"
    assert set(data[0]) == {"id", "name", "qty"}
    con.close()


def test_main_selftest_branch(tmp_path, capsys):
    db = tmp_path / "self.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()
    sqlite_ro_mcp.main(["--db", str(db), "--selftest"])
    out = capsys.readouterr().out
    assert "opened read-only" in out
    assert "write correctly blocked" in out
