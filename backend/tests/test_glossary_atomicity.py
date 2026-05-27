"""Regression test for Bug #5: merge_new_terms must be all-or-nothing.

Previously the loop ran INSERTs in SQLite's per-statement autocommit, so
an exception mid-batch left the glossary in a partially-merged state. The
queue worker would then stamp glossary_merge_error on the chapter but
never re-attempt the missing rows. Wrap the loop in BEGIN/COMMIT (with
rollback on exception) so the outcome is binary.

Uses a per-test tempfile DB rather than touching the shared `DB_PATH`
fixture — aiosqlite's connection close is lazy on Windows, and other
glossary tests in this suite still use the unlink-and-recreate pattern
that races with that lazy close.
"""

import sqlite3
import tempfile
from pathlib import Path

import aiosqlite
import pytest

from backend.db import SCHEMA
from backend.models import NewTerm
from backend.services.glossary import merge_new_terms


def _make_db() -> Path:
    """Per-test fresh DB with schema + one novel (id=1)."""
    db_path = Path(tempfile.mkdtemp(prefix="glossary-atomic-")) / "test.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO novels (id, title, source_type) VALUES (1, 'Test', 'paste')"
    )
    conn.commit()
    conn.close()
    return db_path


def _glossary_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as c:
        return c.execute(
            "SELECT COUNT(*) FROM glossary_entries WHERE novel_id = 1"
        ).fetchone()[0]


@pytest.mark.asyncio
async def test_merge_new_terms_happy_path_persists_all():
    """Sanity check: the BEGIN/COMMIT wrapper doesn't break the normal flow."""
    db_path = _make_db()
    terms = [
        NewTerm(zh="筑基", en="Foundation Establishment", category="other"),
        NewTerm(zh="金丹", en="Golden Core", category="other"),
        NewTerm(zh="元婴", en="Nascent Soul", category="other"),
    ]
    async with aiosqlite.connect(db_path) as conn:
        await merge_new_terms(conn, 1, terms)
    assert _glossary_count(db_path) == 3


@pytest.mark.asyncio
async def test_merge_new_terms_rolls_back_on_exception():
    """The headline regression. Inject an exception inside the merge loop
    and confirm the glossary is empty afterwards — no partial state."""
    db_path = _make_db()
    terms = [
        NewTerm(zh="筑基", en="Foundation Establishment", category="other"),
        NewTerm(zh="金丹", en="Golden Core", category="other"),
        NewTerm(zh="元婴", en="Nascent Soul", category="other"),
    ]
    async with aiosqlite.connect(db_path) as conn:
        # Patch `conn.execute` to raise on the second INSERT. Initial
        # SELECT and BEGIN also go through execute, so we count only the
        # glossary_entries INSERTs.
        real_execute = conn.execute
        insert_count = {"n": 0}

        async def flaky_execute(sql, *args, **kwargs):
            if sql.lstrip().upper().startswith("INSERT") and "glossary_entries" in sql:
                insert_count["n"] += 1
                if insert_count["n"] == 2:
                    raise RuntimeError("simulated mid-batch failure")
            return await real_execute(sql, *args, **kwargs)

        conn.execute = flaky_execute  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="simulated"):
            await merge_new_terms(conn, 1, terms)
    # The all-or-nothing contract: no terms should have landed because the
    # second INSERT raised and the whole transaction rolled back.
    assert _glossary_count(db_path) == 0, (
        f"expected 0 entries after rollback, got {_glossary_count(db_path)} -- "
        "partial commit means the BEGIN/COMMIT wrapper isn't working"
    )
