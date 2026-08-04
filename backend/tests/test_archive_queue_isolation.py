"""Bug hunt 2026-08-04 (B2): archived novels are invisible to the queue.

archive_novel clears the novel's queue flags (covered in test_soft_delete);
this module pins the three novel-filtered queue surfaces:

  - drain_on_startup: skips archived novels' rows AND sweeps stale flags an
    older DB still carries (translate_queued cleared, refinement pending /
    in_progress demoted to 'none'), so a later restore cannot resurrect
    forgotten work;
  - the claim SELECT in _run_translate: an archived novel's queued chapter
    is never claimed, even at a higher queue_priority;
  - prioritize_chapter: the MAX+1 baseline ignores archived novels' rows.
"""

from __future__ import annotations

import pytest

from backend.config import DB_PATH
from backend.db import init_db, open_conn
from backend.services import queue as queue_svc

pytestmark = pytest.mark.asyncio


def _unlink_db() -> None:
    """File-level reset (stronger than per-table DELETEs): the claim SELECT
    under test is deliberately DB-global, so any leftover queued row from
    another module would poison it."""
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH.parent / (DB_PATH.name + suffix)
        if p.exists():
            p.unlink()


@pytest.fixture(autouse=True)
async def fresh_db():
    _unlink_db()
    await init_db()
    yield
    _unlink_db()


async def _new_novel(*, archived: bool = False) -> int:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, deleted_at) "
            "VALUES ('N', 'paste', ?)",
            ("2026-08-04 00:00:00" if archived else None,),
        )
        await conn.commit()
        return cur.lastrowid


async def _new_chapter(novel_id: int, chapter_num: int, **cols) -> int:
    base = {
        "novel_id": novel_id, "chapter_num": chapter_num,
        "original_text": "原文。", "status": "pending",
    }
    base.update(cols)
    keys = ", ".join(base)
    placeholders = ", ".join("?" for _ in base)
    async with open_conn() as conn:
        cur = await conn.execute(
            f"INSERT INTO chapters ({keys}) VALUES ({placeholders})",
            tuple(base.values()),
        )
        await conn.commit()
        return cur.lastrowid


async def _chapter_state(chapter_id: int) -> dict:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT translate_queued, queue_priority, refinement_status "
            "FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        return dict(await cur.fetchone())


async def test_drain_skips_archived_and_sweeps_stale_flags(monkeypatch):
    """A restart must not respawn archived work, and stale flags on an
    archived novel (pre-fix DB, or a mid-flight archive + crash) are swept
    rather than left dormant for a restore to resurrect."""
    active = await _new_novel()
    archived = await _new_novel(archived=True)
    active_ch = await _new_chapter(active, 1, translate_queued=1)
    active_ref = await _new_chapter(
        active, 2, status="done", refinement_status="in_progress",
    )
    stale_t = await _new_chapter(
        archived, 1, translate_queued=1, queue_priority=9,
    )
    stale_r = await _new_chapter(
        archived, 2, status="done", refinement_status="in_progress",
    )
    stale_p = await _new_chapter(
        archived, 3, status="done", refinement_status="pending",
    )

    spawned_translate: list[tuple[int, int]] = []
    spawned_refine = 0

    def _fake_spawn_translate(novel_id: int, chapter_id: int) -> None:
        spawned_translate.append((novel_id, chapter_id))

    def _fake_spawn(coro) -> None:
        nonlocal spawned_refine
        spawned_refine += 1
        coro.close()

    monkeypatch.setattr(queue_svc, "_spawn_translate", _fake_spawn_translate)
    monkeypatch.setattr(queue_svc, "_spawn", _fake_spawn)

    await queue_svc.drain_on_startup()

    # Only the active novel's work respawns.
    assert spawned_translate == [(active, active_ch)]
    assert spawned_refine == 1
    # Active recovery unchanged: in_progress became pending.
    assert (await _chapter_state(active_ref))["refinement_status"] == "pending"
    # Archived rows swept, not just skipped.
    st = await _chapter_state(stale_t)
    assert (st["translate_queued"], st["queue_priority"]) == (0, 0)
    assert (await _chapter_state(stale_r))["refinement_status"] == "none"
    assert (await _chapter_state(stale_p))["refinement_status"] == "none"


async def test_claim_select_never_picks_archived_chapter(monkeypatch):
    """Even a high-priority stale queued row on an archived novel loses the
    claim to an active novel's queued chapter."""
    active = await _new_novel()
    archived = await _new_novel(archived=True)
    active_ch = await _new_chapter(active, 1, translate_queued=1)
    await _new_chapter(archived, 1, translate_queued=1, queue_priority=99)

    processed: list[tuple[int, int]] = []

    async def _fake_translate(conn, novel_id, chapter_id):
        processed.append((novel_id, chapter_id))

    async def _fake_refine(conn, novel_id, chapter_id):
        return None

    monkeypatch.setattr(queue_svc, "_translate_chapter_in_db", _fake_translate)
    monkeypatch.setattr(queue_svc, "_refine_chapter_in_db", _fake_refine)

    await queue_svc._run_translate(active, active_ch)

    assert processed == [(active, active_ch)]


async def test_prioritize_max_ignores_archived_rows():
    """The MAX+1 subquery reads active novels only, so a stale priority on
    an archived novel cannot inflate the baseline."""
    active = await _new_novel()
    archived = await _new_novel(archived=True)
    await _new_chapter(active, 1, translate_queued=1, queue_priority=0)
    await _new_chapter(archived, 1, translate_queued=1, queue_priority=100)

    async with open_conn() as conn:
        priority = await queue_svc.prioritize_chapter(conn, active, 1)

    assert priority == 1
