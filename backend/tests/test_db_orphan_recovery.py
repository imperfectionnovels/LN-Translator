"""init_db()'s init-time orphan recovery.

A worker SIGKILLed (or the host rebooted) between the result commit and the
flag clear can leave a chapter wedged in status='translating', or a terminal
row with a stale translate_queued=1. init_db() repairs both on every boot:
status='translating' -> 'pending', and translate_queued cleared on done/error
rows. This pins that recovery (db.py:1411-1426), which had no seeded test.
"""

from __future__ import annotations

import pytest

from backend import db
from backend.db import init_db, open_conn

pytestmark = pytest.mark.asyncio


async def _seed_orphans() -> None:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('Orphan', 'paste')"
        )
        novel_id = cur.lastrowid
        # A chapter wedged mid-translation, and a done chapter with a stale
        # queue flag. (chapter_num is unique per novel.)
        await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, status, "
            "error_msg) VALUES (?, 1, '原文', 'translating', 'boom')",
            (novel_id,),
        )
        await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, status, "
            "translate_queued) VALUES (?, 2, '原文', 'done', 1)",
            (novel_id,),
        )
        # A genuinely-pending queued chapter that must be left untouched.
        await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, status, "
            "translate_queued) VALUES (?, 3, '原文', 'pending', 1)",
            (novel_id,),
        )
        await conn.commit()


async def test_init_db_recovers_stuck_translating_and_stale_queue():
    await init_db()
    await _seed_orphans()

    # Second init_db (a "reboot") runs the recovery.
    await init_db()

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT chapter_num, status, error_msg, translate_queued "
            "FROM chapters ORDER BY chapter_num"
        )
        rows = {r["chapter_num"]: r for r in await cur.fetchall()}

    # Wedged 'translating' -> 'pending', error cleared.
    assert rows[1]["status"] == "pending"
    assert rows[1]["error_msg"] is None
    # Stale flag on the terminal 'done' row cleared.
    assert rows[2]["status"] == "done"
    assert rows[2]["translate_queued"] == 0
    # A legitimately-pending queued chapter is NOT cleared.
    assert rows[3]["status"] == "pending"
    assert rows[3]["translate_queued"] == 1

    assert db.LAST_ORPHAN_RECOVERY["translating_reset"] >= 1
    assert db.LAST_ORPHAN_RECOVERY["stale_translate_cleared"] >= 1
