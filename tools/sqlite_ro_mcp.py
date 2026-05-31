"""Tiny READ-ONLY SQLite MCP server for inspecting LN-Translator databases.

Why this exists: glossary / chapter debugging kept requiring throwaway Python
scripts to read `novels.db`, and the live DB the packaged EXE uses must never be
written to by a dev tool. This server is read-only at three layers:
  1. opens the DB with the SQLite `?mode=ro` URI (writes are impossible),
  2. accepts only SELECT / WITH / EXPLAIN / introspection-PRAGMA statements,
  3. rejects multi-statement input (no `;`-smuggled writes).

It is launched by Claude Code via `.mcp.json` (one instance per database). It is
NOT part of the app runtime and is never imported by `backend/`. Depends only on
the `mcp` SDK (already installed in the pythoncore env).

Tools exposed:
  list_tables()                  -> table + view names
  describe_table(table)          -> column schema (PRAGMA table_info)
  read_query(sql, limit=200)     -> rows as JSON (SELECT/WITH/EXPLAIN only)

Smoke test without MCP:
  python tools/sqlite_ro_mcp.py --db data/novels.db --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys

_READ_PREFIXES = ("select", "with", "explain", "pragma")
_PRAGMA_OK = re.compile(
    r"^pragma\s+(table_info|index_list|index_info|table_list|"
    r"foreign_key_list|database_list)\b",
    re.IGNORECASE,
)

parser = argparse.ArgumentParser()
parser.add_argument("--db", required=True, help="absolute path to the SQLite file")
parser.add_argument("--name", default=None, help="server display name")
parser.add_argument("--selftest", action="store_true", help="run a local check, no MCP")
ARGS, _ = parser.parse_known_args()

DB_PATH = os.path.abspath(os.path.expandvars(ARGS.db))
SERVER_NAME = ARGS.name or ("sqlite-ro:" + os.path.basename(DB_PATH))


def _connect() -> sqlite3.Connection:
    # mode=ro: open an existing DB read-only; any write raises OperationalError.
    uri = "file:" + DB_PATH.replace("\\", "/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _guard(sql: str) -> str | None:
    """Return an error string if the query is not a safe read, else None."""
    body = sql.strip().rstrip(";")
    if ";" in body:
        return "Only a single statement is allowed."
    low = body.lstrip("(").strip().lower()
    if not low.startswith(_READ_PREFIXES):
        return "Read-only: only SELECT / WITH / EXPLAIN / PRAGMA queries are allowed."
    if low.startswith("pragma") and not _PRAGMA_OK.match(low):
        return "Only introspection PRAGMAs (e.g. table_info) are allowed."
    return None


def _rows_to_json(cur: sqlite3.Cursor, limit: int) -> str:
    rows = cur.fetchmany(max(1, min(limit, 2000)))
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1, default=str)


def main() -> None:
    if ARGS.selftest:
        con = _connect()
        n = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchone()[0]
        print(f"[selftest] {SERVER_NAME}: opened read-only, {n} tables/views")
        try:
            con.execute("CREATE TABLE _should_fail(x)")
            print("[selftest] ERROR: write succeeded, NOT read-only!")
            sys.exit(1)
        except sqlite3.OperationalError as e:
            print(f"[selftest] write correctly blocked: {e}")
        con.close()
        return

    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(SERVER_NAME)

    @mcp.tool()
    def list_tables() -> str:
        """List all tables and views in the database."""
        con = _connect()
        try:
            rows = con.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY type, name"
            ).fetchall()
            return json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1)
        finally:
            con.close()

    @mcp.tool()
    def describe_table(table: str) -> str:
        """Show the column schema for a table (name, type, nullability, pk)."""
        if not re.fullmatch(r"[A-Za-z0-9_]+", table or ""):
            return "Invalid table name."
        con = _connect()
        try:
            rows = con.execute(f"PRAGMA table_info({table})").fetchall()
            if not rows:
                return f"No such table: {table}"
            return json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1)
        finally:
            con.close()

    @mcp.tool()
    def read_query(sql: str, limit: int = 200) -> str:
        """Run a read-only query (SELECT/WITH/EXPLAIN). Returns rows as JSON.

        Read-only is enforced: the DB is opened with ?mode=ro and only read
        statements are accepted. `limit` caps returned rows (max 2000)."""
        err = _guard(sql)
        if err:
            return err
        con = _connect()
        try:
            return _rows_to_json(con.execute(sql), limit)
        except sqlite3.Error as e:
            return f"SQL error: {e}"
        finally:
            con.close()

    mcp.run()


if __name__ == "__main__":
    main()
