"""Cancel an in-flight translation.

Covers queue.cancel_translate + the CancelledError handler in _run_translate:
- A worker blocked mid-LLM-call can be cancelled; the row resets out of
  'translating' (to 'pending' when there's no prior translation) and the
  durable queue flag is cleared.
- Cancelling a retranslate of an already-done chapter preserves the prior
  translation (row reverts to 'done', keeps translated_text).
- cancel_translate is a no-op (returns False) when no task is registered.
- D1 (bug hunt 2026-08-08): a second _spawn_translate call for a chapter
  that already has a live task registered must be a no-op, so cancel keeps
  targeting the real worker instead of a freshly spawned idle duplicate.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.db import init_db, open_conn
from backend.services import providers as providers_svc
from backend.services import queue as queue_svc

pytestmark = pytest.mark.asyncio


async def _reset_db() -> None:
    async with open_conn() as conn:
        for table in ("chapters", "novels", "providers"):
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


async def _seed_translator_provider() -> int:
    p = await providers_svc.create_provider(
        name="translator", provider_type="gemini", model_id="m", is_default=True,
    )
    return p.id


async def _make_chapter(prior_text: str | None = None) -> tuple[int, int]:
    """Insert a novel + one queued chapter; optionally pre-seed translated_text
    (simulating a retranslate of an already-done chapter)."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, ?)",
            ("test novel", "paste"),
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status, translate_queued) "
            "VALUES (?, 1, '原文', ?, 'pending', 1)",
            (novel_id, prior_text),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return novel_id, chapter_id


async def _wait_for_status(chapter_id: int, target: str, timeout: float = 5.0) -> None:
    """Poll the row until its status reaches `target`, or fail on timeout."""
    waited = 0.0
    while waited < timeout:
        async with open_conn() as conn:
            cur = await conn.execute(
                "SELECT status FROM chapters WHERE id = ?", (chapter_id,)
            )
            row = await cur.fetchone()
        if row and row["status"] == target:
            return
        await asyncio.sleep(0.02)
        waited += 0.02
    raise AssertionError(f"chapter {chapter_id} never reached status={target!r}")


def _block_translate(monkeypatch) -> None:
    """Stub translate_chapter so the worker parks inside the lock, after the
    pending->translating claim has committed, until it's cancelled."""
    async def _never(*args, **kwargs):
        await asyncio.Event().wait()  # blocks forever
    monkeypatch.setattr("backend.services.queue.translate_chapter", _never)


async def test_cancel_in_flight_resets_to_pending(monkeypatch):
    await _seed_translator_provider()
    novel_id, chapter_id = await _make_chapter()
    _block_translate(monkeypatch)

    queue_svc._spawn_translate(novel_id, chapter_id)
    await _wait_for_status(chapter_id, "translating")

    assert await queue_svc.cancel_translate(chapter_id) is True

    # The task should unwind with CancelledError after the handler runs.
    task = queue_svc._translate_tasks.get(chapter_id)
    if task is not None:
        with pytest.raises(asyncio.CancelledError):
            await task

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT status, translate_queued, force_retranslate "
            "FROM chapters WHERE id = ?", (chapter_id,)
        )
        row = await cur.fetchone()
    assert row["status"] == "pending"
    assert not row["translate_queued"]
    assert not row["force_retranslate"]


async def test_cancel_retranslate_preserves_prior_translation(monkeypatch):
    await _seed_translator_provider()
    novel_id, chapter_id = await _make_chapter(prior_text="A previously good translation.")
    _block_translate(monkeypatch)

    queue_svc._spawn_translate(novel_id, chapter_id)
    await _wait_for_status(chapter_id, "translating")
    assert await queue_svc.cancel_translate(chapter_id) is True
    task = queue_svc._translate_tasks.get(chapter_id)
    if task is not None:
        with pytest.raises(asyncio.CancelledError):
            await task

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT status, translated_text, translate_queued "
            "FROM chapters WHERE id = ?", (chapter_id,)
        )
        row = await cur.fetchone()
    assert row["status"] == "done"
    assert row["translated_text"] == "A previously good translation."
    assert not row["translate_queued"]


async def test_cancel_with_no_task_is_noop():
    assert await queue_svc.cancel_translate(999999) is False


async def test_second_spawn_is_noop_while_first_task_is_live(monkeypatch):
    """D1: a rapid double Retranslate (or any second _spawn_translate call
    for a chapter that is already covered by a live task) must NOT replace
    the registered task. Before the fix, the unconditional
    `_translate_tasks[chapter_id] = task` assignment overwrote the real
    worker's slot with a second, idle task; cancel_translate would then
    cancel the harmless newcomer while the real, billed LLM call ran to
    completion uncancelled."""
    await _seed_translator_provider()
    novel_id, chapter_id = await _make_chapter()
    _block_translate(monkeypatch)

    queue_svc._spawn_translate(novel_id, chapter_id)
    await _wait_for_status(chapter_id, "translating")
    live_task = queue_svc._translate_tasks.get(chapter_id)
    assert live_task is not None and not live_task.done()

    # Second spawn for the SAME chapter id, mirroring a second Retranslate
    # click landing while the first worker is still running.
    queue_svc._spawn_translate(novel_id, chapter_id)
    assert queue_svc._translate_tasks.get(chapter_id) is live_task, (
        "a second spawn for an already-covered chapter must not replace "
        "the live task"
    )

    # Cancel must reach the ORIGINAL (real) worker, not a duplicate.
    assert await queue_svc.cancel_translate(chapter_id) is True
    with pytest.raises(asyncio.CancelledError):
        await live_task

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT status, translate_queued FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["status"] == "pending"
    assert not row["translate_queued"]


async def test_spawn_after_task_completes_registers_a_fresh_task(monkeypatch):
    """The D1 guard only blocks a spawn while the existing task is live.
    Once that task is done, a later spawn for the same chapter id must
    register normally (a genuinely new translate run, e.g. a subsequent
    retranslate after the first one finished)."""
    await _seed_translator_provider()
    novel_id, chapter_id = await _make_chapter()
    _block_translate(monkeypatch)

    queue_svc._spawn_translate(novel_id, chapter_id)
    await _wait_for_status(chapter_id, "translating")
    first_task = queue_svc._translate_tasks.get(chapter_id)
    assert await queue_svc.cancel_translate(chapter_id) is True
    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert first_task.done()

    # Re-flag the row as queued (mirrors what the retranslate route does
    # before spawning) and spawn again.
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET status = 'pending', translate_queued = 1 "
            "WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()
    queue_svc._spawn_translate(novel_id, chapter_id)
    await _wait_for_status(chapter_id, "translating")
    second_task = queue_svc._translate_tasks.get(chapter_id)
    assert second_task is not None
    assert second_task is not first_task

    # Clean up: cancel the second task so it doesn't leak into other tests.
    assert await queue_svc.cancel_translate(chapter_id) is True
    with pytest.raises(asyncio.CancelledError):
        await second_task
