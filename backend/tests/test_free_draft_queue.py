"""Tests for backend.services.free_draft_queue.

Stubs out ``GoogleTranslateFreeTranslator.translate_chapter`` so we don't
need network access. Verifies:
  * queue_free_draft only spawns when free_draft_status is 'none' or 'error'.
  * the worker writes free_draft_text + flips status to 'done'.
  * a failing Google Translate call lands free_draft_status='error' with the message.
  * maybe_queue_for_open_chapter skips when status='done'.
  * drain_on_startup re-queues 'pending' rows and resets stuck 'in_progress'.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from backend.db import init_db, open_conn
from backend.models import TranslationResult
from backend.services import free_draft_queue
from backend.services.translators import google_translate_free as gt_module

pytestmark = pytest.mark.asyncio


async def _reset_db():
    async with open_conn() as conn:
        for table in ("chapters", "novels", "providers"):
            try:
                await conn.execute(f"DELETE FROM {table}")
            except aiosqlite.OperationalError:
                pass
        await conn.commit()


@pytest.fixture(autouse=True)
async def fresh_db():
    await init_db()
    await _reset_db()
    # Ensure the module's in-flight task set is empty across tests.
    free_draft_queue._background_tasks.clear()
    yield
    await _reset_db()


async def _make_novel_and_chapter(
    *,
    source_language: str = "zh",
    free_draft_status: str = "none",
    status: str = "pending",
) -> tuple[int, int]:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, source_language) "
            "VALUES (?, 'paste', ?)",
            ("Test Novel", source_language),
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, original_text, status, free_draft_status) "
            "VALUES (?, 1, ?, ?, ?)",
            (novel_id, "第一章内容。", status, free_draft_status),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return novel_id, chapter_id


def _install_stub_translator(
    monkeypatch, *, output: str = "drafted body", raise_exc: Exception | None = None,
):
    """Replace GoogleTranslateFreeTranslator.translate_chapter with a coroutine
    that returns a TranslationResult (or raises) without touching the network."""

    async def _fake_translate_chapter(
        self,
        chapter_zh: str,
        title_zh,
        glossary,
        previous_context=None,
        style_edits=None,
        use_cache=True,
        style_note=None,
        genre=None,
        custom_brief=None,
        free_draft=None,
        source_language=None,
    ):
        if raise_exc is not None:
            raise raise_exc
        return TranslationResult(
            title_en=(title_zh or "(untitled)"),
            translated_text=output,
            new_terms=[],
            degraded=True,
        )

    monkeypatch.setattr(
        gt_module.GoogleTranslateFreeTranslator,
        "translate_chapter",
        _fake_translate_chapter,
        raising=True,
    )


# ---------------------------------------------------------------------------
# queue_free_draft contract
# ---------------------------------------------------------------------------

async def test_queue_free_draft_spawns_when_status_is_none(monkeypatch):
    novel_id, chapter_id = await _make_novel_and_chapter()
    _install_stub_translator(monkeypatch, output="EN draft")

    spawned = await free_draft_queue.queue_free_draft(novel_id, chapter_id)
    assert spawned is True
    # Wait for the worker to run.
    for _ in range(50):
        await asyncio.sleep(0.01)
        async with open_conn() as conn:
            cur = await conn.execute(
                "SELECT free_draft_status, free_draft_text FROM chapters WHERE id = ?",
                (chapter_id,),
            )
            row = await cur.fetchone()
        if row["free_draft_status"] == "done":
            break
    assert row["free_draft_status"] == "done"
    assert row["free_draft_text"] == "EN draft"


async def test_queue_free_draft_idempotent_when_already_done(monkeypatch):
    novel_id, chapter_id = await _make_novel_and_chapter(free_draft_status="done")
    _install_stub_translator(monkeypatch)
    spawned = await free_draft_queue.queue_free_draft(novel_id, chapter_id)
    assert spawned is False


async def test_queue_free_draft_retries_from_error(monkeypatch):
    novel_id, chapter_id = await _make_novel_and_chapter(free_draft_status="error")
    _install_stub_translator(monkeypatch)
    spawned = await free_draft_queue.queue_free_draft(novel_id, chapter_id)
    assert spawned is True


async def test_worker_records_error_message(monkeypatch):
    novel_id, chapter_id = await _make_novel_and_chapter()
    _install_stub_translator(monkeypatch, raise_exc=RuntimeError("Google rate-limited"))
    await free_draft_queue.queue_free_draft(novel_id, chapter_id)
    for _ in range(50):
        await asyncio.sleep(0.01)
        async with open_conn() as conn:
            cur = await conn.execute(
                "SELECT free_draft_status, free_draft_error FROM chapters WHERE id = ?",
                (chapter_id,),
            )
            row = await cur.fetchone()
        if row["free_draft_status"] == "error":
            break
    assert row["free_draft_status"] == "error"
    assert "rate-limited" in (row["free_draft_error"] or "")


# ---------------------------------------------------------------------------
# maybe_queue_for_open_chapter contract
# ---------------------------------------------------------------------------

async def test_maybe_queue_runs_even_when_chapter_translated(monkeypatch):
    """The Polished / Free draft toggle depends on free_draft_text being
    set; the user wants the toggle on already-translated chapters too, so
    the trigger must NOT gate on chapter.status."""
    novel_id, chapter_id = await _make_novel_and_chapter(status="done")
    _install_stub_translator(monkeypatch, output="lazy-drafted")
    spawned = await free_draft_queue.maybe_queue_for_open_chapter(novel_id, chapter_id)
    assert spawned is True


async def test_maybe_queue_skips_when_already_done(monkeypatch):
    novel_id, chapter_id = await _make_novel_and_chapter(free_draft_status="done")
    spawned = await free_draft_queue.maybe_queue_for_open_chapter(novel_id, chapter_id)
    assert spawned is False


async def test_maybe_queue_retries_when_previous_attempt_errored(monkeypatch):
    """A chapter whose previous free-draft attempt errored (network blip,
    Google rate-limit) should get retried on the next reader open. Mirrors
    queue_free_draft's WHERE clause that accepts both 'none' and 'error'."""
    novel_id, chapter_id = await _make_novel_and_chapter(free_draft_status="error")
    _install_stub_translator(monkeypatch, output="retry-drafted")
    spawned = await free_draft_queue.maybe_queue_for_open_chapter(novel_id, chapter_id)
    assert spawned is True


async def test_maybe_queue_spawns_on_happy_path(monkeypatch):
    novel_id, chapter_id = await _make_novel_and_chapter()
    _install_stub_translator(monkeypatch, output="lazy-drafted")
    spawned = await free_draft_queue.maybe_queue_for_open_chapter(novel_id, chapter_id)
    assert spawned is True


# ---------------------------------------------------------------------------
# drain_on_startup
# ---------------------------------------------------------------------------

async def test_drain_resets_stuck_in_progress(monkeypatch):
    novel_id, chapter_id = await _make_novel_and_chapter(
        free_draft_status="in_progress",
    )
    _install_stub_translator(monkeypatch)
    await free_draft_queue.drain_on_startup()
    # The drain moves in_progress → pending, then spawns; wait for the
    # worker to finish.
    for _ in range(50):
        await asyncio.sleep(0.01)
        async with open_conn() as conn:
            cur = await conn.execute(
                "SELECT free_draft_status FROM chapters WHERE id = ?",
                (chapter_id,),
            )
            row = await cur.fetchone()
        if row["free_draft_status"] == "done":
            break
    assert row["free_draft_status"] == "done"


# ---------------------------------------------------------------------------
# Queue worker writes translated_by_provider_id
# ---------------------------------------------------------------------------

async def test_translator_provenance_column_written(monkeypatch):
    """A regression pin: queue.py's success commit writes
    chapters.translated_by_provider_id so the reader can branch banner copy
    on provider_type without re-deriving from novels.translator_provider_id.
    """
    from backend.services import queue as queue_svc

    # Make a chapter and a provider row.
    novel_id, chapter_id = await _make_novel_and_chapter()
    async with open_conn() as conn:
        # Mint a provider row and resolve it.
        await conn.execute(
            "INSERT INTO providers (name, provider_type, model_id, params_json) "
            "VALUES ('test-provider', 'gemini', 'gemini-2.5-flash', '{}')",
        )
        cur = await conn.execute("SELECT id FROM providers WHERE name = 'test-provider'")
        prov_row = await cur.fetchone()
        provider_id = prov_row["id"]
        await conn.execute(
            "UPDATE novels SET translator_provider_id = ? WHERE id = ?",
            (provider_id, novel_id),
        )
        # Pre-claim the row so the worker's "status='pending'" guard succeeds.
        await conn.execute(
            "UPDATE chapters SET translate_queued = 1 WHERE id = ?", (chapter_id,),
        )
        await conn.commit()

    # Stub translate_chapter at the service layer.
    from backend.services import translators as translators_module

    async def _fake_translate_chapter(*args, **kwargs):
        return TranslationResult(
            title_en="EN title",
            translated_text="EN body.",
            new_terms=[],
            degraded=False,
        )

    monkeypatch.setattr(
        translators_module, "translate_chapter", _fake_translate_chapter,
        raising=True,
    )
    # Also patch the queue's reference (queue imports translate_chapter
    # by name, not from a fresh module attribute lookup, so we need to
    # patch the bound reference too).
    monkeypatch.setattr(queue_svc, "translate_chapter", _fake_translate_chapter)

    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)

    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT status, translated_by_provider_id, translated_text "
            "FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        row = await cur.fetchone()
    assert row["status"] == "done"
    assert row["translated_by_provider_id"] == provider_id
    assert row["translated_text"] == "EN body."
