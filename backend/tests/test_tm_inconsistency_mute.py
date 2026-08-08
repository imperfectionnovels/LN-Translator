"""D3 (bug hunt 2026-08-08): _emit_tm_inconsistency_observations must honor
the per-novel `disabled_observers` mute, the same way the translate-stage
observation persist (queue.py ~1254-1264) and the refinement-stage drift
observation (queue.py ~1697-1705) already do.

Before the fix, this emitter inserted `tm_inconsistency` rows with raw SQL
and never consulted the mute at all, so a user who muted that kind on the
QA panel still had chapter_observations rows re-appear on every retranslate
that touched TM.

Tests call queue._emit_tm_inconsistency_observations directly with hand-
seeded tm_segments rows (two chapters sharing a source_hash but rendering it
two different ways -- exactly the shape the emitter looks for), rather than
running the whole translate pipeline: the emitter owns no state besides the
DB rows it reads and writes, so this pins the fix precisely without the
fragility of stubbing glossary/segments/provider resolution too.
"""

from __future__ import annotations

import pytest

from backend.db import init_db, open_conn
from backend.services import queue as queue_svc

pytestmark = pytest.mark.asyncio


async def _reset_db() -> None:
    async with open_conn() as conn:
        for table in (
            "chapter_observations", "tm_segments", "chapters", "novels",
        ):
            try:
                await conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        await conn.commit()


@pytest.fixture(autouse=True)
async def fresh_db():
    await init_db()
    await _reset_db()
    yield
    await _reset_db()


async def _seed_inconsistent_tm(
    disabled_observers: str | None = None,
) -> tuple[int, int]:
    """Two chapters of one novel share a source_hash but render it two
    different ways. Returns (novel_id, chapter_id) for the second chapter --
    the one the emit call is scoped to, mirroring how the queue worker calls
    it right after that chapter's own TM rows are freshly populated."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, disabled_observers) "
            "VALUES (?, ?, ?)",
            ("test novel", "paste", disabled_observers),
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status) VALUES (?, 1, '原文', 'Rendering A', 'done')",
            (novel_id,),
        )
        ch1_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status) VALUES (?, 2, '原文', 'Rendering B', 'done')",
            (novel_id,),
        )
        ch2_id = cur.lastrowid
        await conn.execute(
            "INSERT INTO tm_segments (novel_id, chapter_id, paragraph_index, "
            "source_text, target_text, source_hash) VALUES (?, ?, 0, ?, ?, ?)",
            (novel_id, ch1_id, "源段一", "Rendering A", "deadbeefdeadbeef"),
        )
        await conn.execute(
            "INSERT INTO tm_segments (novel_id, chapter_id, paragraph_index, "
            "source_text, target_text, source_hash) VALUES (?, ?, 0, ?, ?, ?)",
            (novel_id, ch2_id, "源段一", "Rendering B", "deadbeefdeadbeef"),
        )
        await conn.commit()
    return novel_id, ch2_id


async def test_unmuted_novel_writes_tm_inconsistency_row():
    novel_id, chapter_id = await _seed_inconsistent_tm(disabled_observers=None)
    async with open_conn() as conn:
        await queue_svc._emit_tm_inconsistency_observations(
            conn, novel_id, chapter_id
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT kind FROM chapter_observations WHERE chapter_id = ?",
            (chapter_id,),
        )
        rows = await cur.fetchall()
    assert [r["kind"] for r in rows] == ["tm_inconsistency"]


async def test_muted_tm_inconsistency_writes_no_rows():
    novel_id, chapter_id = await _seed_inconsistent_tm(
        disabled_observers='["tm_inconsistency"]'
    )
    async with open_conn() as conn:
        await queue_svc._emit_tm_inconsistency_observations(
            conn, novel_id, chapter_id
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM chapter_observations WHERE chapter_id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["n"] == 0


async def test_muting_an_unrelated_kind_leaves_tm_inconsistency_intact():
    """The mute is per-kind, not a global observer kill switch: muting some
    other kind must not accidentally suppress tm_inconsistency too."""
    novel_id, chapter_id = await _seed_inconsistent_tm(
        disabled_observers='["mt_texture"]'
    )
    async with open_conn() as conn:
        await queue_svc._emit_tm_inconsistency_observations(
            conn, novel_id, chapter_id
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT kind FROM chapter_observations WHERE chapter_id = ?",
            (chapter_id,),
        )
        rows = await cur.fetchall()
    assert [r["kind"] for r in rows] == ["tm_inconsistency"]


async def test_helper_fails_open_on_malformed_disabled_observers():
    """_fetch_muted_observer_kinds delegates to parse_disabled_observers,
    which fails open (treats malformed JSON as no mutes) rather than
    silently dropping every observation."""
    novel_id, chapter_id = await _seed_inconsistent_tm(
        disabled_observers="not valid json"
    )
    async with open_conn() as conn:
        await queue_svc._emit_tm_inconsistency_observations(
            conn, novel_id, chapter_id
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT kind FROM chapter_observations WHERE chapter_id = ?",
            (chapter_id,),
        )
        rows = await cur.fetchall()
    assert [r["kind"] for r in rows] == ["tm_inconsistency"]
