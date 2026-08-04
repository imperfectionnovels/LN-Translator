"""Per-chapter translate queue.

Single-pass pipeline: the translator owns correctness AND prose. Each chapter
runs through one LLM call (claude_agent with extended thinking by default),
then deterministic text fixups clean em-dashes / brackets / casing. Guardrail
hits are LOGGED but no longer trigger retries or mark chapters degraded — the
single-pass thesis is that noticing happens upstream of checking, so a retry
is just two shallow passes instead of one deeper one.

Chapters only enter the queue when the user explicitly indicates them
(per-chapter buttons, glossary retranslate-affected). The `translate_queued`
flag survives a server restart so `drain_on_startup` re-spawns workers for
anything still pending. Concurrency is strictly serial via one process-global
asyncio.Lock — every backend is effectively max_parallel=1 (Claude burns the
subscription window in parallel, Gemini burns tokens).
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiosqlite

# A/B flags are read as config.<NAME> attributes at call time, never held as
# module-level copies (bug hunt 2026-08-04, F8): one monkeypatch site
# (backend.config) controls every gate AND the snapshot's flags dict, so the
# recorded flag state can never diverge from the gate actually applied.
from backend import config
from backend.db import open_conn
from backend.services import global_glossary as global_glossary_svc
from backend.services import glossary as glossary_svc
from backend.services import segments as segments_svc
from backend.services import tm as tm_svc
from backend.services._task_registry import BackgroundTaskRegistry
from backend.services.observations import (
    NormalizedObservation,
    implicit_observation_glossary_merge_error,
    implicit_observation_paragraph_count_drift,
    implicit_observation_tm_inconsistency,
    implicit_observation_translation_degraded,
    normalize_observer_outputs,
    parse_disabled_observers,
)
from backend.services.parser import (
    normalize_title_en,
    strip_heading_update_marker,
    strip_leading_title_line,
    strip_title_update_marker,
)
from backend.services.prompt_inputs import (
    NovelGenreBrief,
    fetch_confirmed_exemplars,
    fetch_novel_genre_brief,
    fetch_previous_chapter_tail,
    fetch_style_edits,
    fetch_style_note,
    resolve_translator_provider,
)
from backend.services.providers import (
    Provider,
    load_provider,
)
from backend.services.refiner import refine_chapter
from backend.services.segmentation import (
    chapter_source_paragraphs,
    split_target_paragraphs,
)
from backend.services.text_fixups import (
    enforce_balanced_emphasis,
    enforce_brackets,
    enforce_em_dash,
    enforce_locked_term_casing,
    enforce_lowercase_locked_terms,
    enforce_mid_sentence_comma_break,
    enforce_sentence_initial_capitalization,
    enforce_spaced_hyphen_dash,
    enforce_stem_branch_casing,
    strip_chapter_end_marker,
)
from backend.services.text_observers import (
    body_correctness_observations,
    detect_glossary_predicate_loss,
)
from backend.services.translators import translate_chapter
from backend.services.translators.base import (
    COUNT_MISMATCH_ACCEPTED,
    COUNT_MISMATCH_RETRY,
    PROMPT_TEMPLATE_VERSION,
    ParagraphCountMismatch,
    build_count_corrective,
    pair_side_key,
)

logger = logging.getLogger(__name__)


def _build_prompt_config_snapshot(
    *,
    provider: Provider | None,
    novel_meta: NovelGenreBrief,
    free_draft_included: bool,
    previous_context_included: bool,
    style_note_included: bool,
    style_edits_included: bool,
    approved_translations_included: bool,
    confirmed_exemplars_included: bool,
    plain_fallback: bool = False,
) -> str:
    """JSON blob recording the prompt-assembly config that produced this
    chapter. Stamped onto chapters.prompt_config_snapshot in the same
    transaction as the chapter success commit so A/B runs stay recoverable
    per-output.

    The `*_included` keys record what actually shipped to the model (block
    sent only when both the env flag was true AND the data was non-empty).
    The `flags` dict records what the env said, so a flag-on + data-empty
    state is distinguishable from a flag-off state.

    `plain_fallback` (F4, bug hunt 2026-08-04): when the committed body came
    from the translator's plain-text fallback, the prompt that produced it
    carried NONE of the dynamic blocks (see base._plain_text_fallback), so
    every block `*_included` key is forced false regardless of what was
    fetched, and the always-present `plain_fallback` key marks the row so
    A/B queries can exclude (or isolate) degraded output."""
    import json  # noqa: PLC0415
    if plain_fallback:
        free_draft_included = previous_context_included = False
        style_note_included = style_edits_included = False
        approved_translations_included = confirmed_exemplars_included = False
    return json.dumps({
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "translator_provider_id": provider.id if provider else None,
        "translator_provider_type": provider.provider_type if provider else None,
        "translator_model_id": provider.model_id if provider else None,
        "genre": novel_meta["genre"],
        "custom_brief_present": bool(novel_meta["custom_style_brief"]),
        "plain_fallback": plain_fallback,
        "free_draft_included": free_draft_included,
        "previous_context_included": previous_context_included,
        "style_note_included": style_note_included,
        "style_edits_included": style_edits_included,
        "approved_translations_included": approved_translations_included,
        "confirmed_exemplars_included": confirmed_exemplars_included,
        "flags": {
            "PROMPT_INCLUDE_FREE_DRAFT": config.PROMPT_INCLUDE_FREE_DRAFT,
            "PROMPT_INCLUDE_STYLE_NOTE": config.PROMPT_INCLUDE_STYLE_NOTE,
            "PROMPT_INCLUDE_STYLE_EDITS": config.PROMPT_INCLUDE_STYLE_EDITS,
            "PROMPT_INCLUDE_REFINER": config.PROMPT_INCLUDE_REFINER,
            "PROMPT_INCLUDE_APPROVED_TRANSLATIONS":
                config.PROMPT_INCLUDE_APPROVED_TRANSLATIONS,
            "PROMPT_INCLUDE_CONFIRMED_EXEMPLARS":
                config.PROMPT_INCLUDE_CONFIRMED_EXEMPLARS,
            "PREVIOUS_CONTEXT_ENABLED": config.PREVIOUS_CONTEXT_ENABLED,
        },
    }, sort_keys=True)


def _extend_snapshot_with_refiner(
    existing_json: str | None, refiner: Provider
) -> str:
    """Merge refiner_* keys into the existing prompt_config_snapshot blob.
    Tolerant of None / empty / malformed JSON: starts from {} so the refiner
    provenance is recorded even when the translator-side stamp is missing
    (legacy rows or migration boundary)."""
    import json  # noqa: PLC0415
    try:
        base = json.loads(existing_json) if existing_json else {}
        if not isinstance(base, dict):
            base = {}
    except (TypeError, ValueError):
        base = {}
    base["refiner_provider_id"] = refiner.id
    base["refiner_provider_type"] = refiner.provider_type
    base["refiner_model_id"] = refiner.model_id
    return json.dumps(base, sort_keys=True)


# Process-global lock. Acquired by every translate task before doing work, so
# however many tasks have been spawned, only one runs at a time. Subscription
# quota math (Claude) and API spend (Gemini/DeepSeek) require serial.
_translator_lock = asyncio.Lock()

# Strong references to in-flight fire-and-forget worker tasks. asyncio's event
# loop keeps only a WEAK reference to a task, so an unreferenced task can be
# garbage-collected mid-run — silently dropping a queued chapter. Holding the
# task here until its done-callback fires prevents that.
_registry = BackgroundTaskRegistry()


def _spawn(coro) -> None:
    _registry.spawn(coro)


# Maps chapter_id -> the in-flight _run_translate task, so an explicit cancel
# request can find and interrupt the worker. At most one task per chapter is
# ever active (the serial translator lock guarantees it), so a plain
# chapter_id key is unambiguous.
_translate_tasks: dict[int, asyncio.Task] = {}


def _spawn_translate(novel_id: int, chapter_id: int) -> None:
    """Spawn a translate worker and register it under its chapter id so it can
    be cancelled mid-flight. Uses the shared registry for the strong-ref
    bookkeeping, plus a per-chapter cancellation slot."""

    def _done(t: asyncio.Task) -> None:
        # Clear every slot that still points at THIS task: the claim-at-lock
        # rebind (audit 3.2) may have re-keyed the task under the chapter it
        # actually processed, so the spawn-time key alone is not enough. Slots
        # already overwritten by a newer task (rapid retranslate) are left.
        for key in [k for k, v in _translate_tasks.items() if v is t]:
            _translate_tasks.pop(key, None)

    task = _registry.spawn(_run_translate(novel_id, chapter_id), on_done=_done)
    _translate_tasks[chapter_id] = task


async def cancel_translate(chapter_id: int) -> bool:
    """Cancel an in-flight translate worker for this chapter, if one is
    running. Returns True if a cancel was issued. The worker's CancelledError
    handler resets the row out of the 'translating' state."""
    task = _translate_tasks.get(chapter_id)
    if task is not None and not task.done():
        task.cancel()
        return True
    return False


# Batch size for IN(...) clauses in reset_chapters_for_retranslate and
# queue_translations. SQLite caps parameters at 999 on pre-3.32 builds and
# 32766 on newer; 500 is comfortably under both.
_QUEUE_BATCH_CHUNK = 500


# Minimum draft length (stripped) to bother calling the refiner. Drafts
# shorter than this are usually parse errors or stray author-note rows;
# refining them burns a paid LLM round-trip for ≤ a paragraph of text the
# reader will show as-is anyway. The chapter is marked refinement_status
# 'none' (same as the empty-draft path) so the reader falls back to the
# draft without a banner.
_REFINEMENT_MIN_DRAFT_CHARS = 200


def spawn_translate_worker(novel_id: int, chapter_id: int) -> None:
    """Spawn a translator worker task without touching the DB. Caller is
    responsible for having already set translate_queued=1 in a prior
    transaction. Used by the retranslate paths."""
    _spawn_translate(novel_id, chapter_id)


async def queue_translation(novel_id: int, chapter_id: int) -> None:
    """Mark a chapter as queued for translation and spawn a worker task."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "UPDATE chapters SET translate_queued = 1 "
            "WHERE id = ? AND novel_id = ?",
            (chapter_id, novel_id),
        )
        await conn.commit()
    if (cur.rowcount or 0) > 0:
        _spawn_translate(novel_id, chapter_id)


async def queue_translations(novel_id: int, chapter_ids: list[int]) -> None:
    """Batched `queue_translation` for many chapters in one UPDATE per chunk."""
    if not chapter_ids:
        return
    spawned: list[int] = []
    async with open_conn() as conn:
        for i in range(0, len(chapter_ids), _QUEUE_BATCH_CHUNK):
            chunk = chapter_ids[i : i + _QUEUE_BATCH_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            await conn.execute(
                f"UPDATE chapters SET translate_queued = 1 "
                f"WHERE novel_id = ? AND id IN ({placeholders})",
                [novel_id, *chunk],
            )
            cur = await conn.execute(
                f"SELECT id FROM chapters "
                f"WHERE novel_id = ? AND id IN ({placeholders}) "
                f"  AND translate_queued = 1",
                [novel_id, *chunk],
            )
            spawned.extend(r["id"] for r in await cur.fetchall())
        await conn.commit()
    for cid in spawned:
        _spawn_translate(novel_id, cid)


async def reset_chapters_for_retranslate(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapter_ids: list[int],
) -> list[int]:
    """Reset rows for a re-translation + flag them in the queue, atomically.
    Returns the chapter ids actually reset (in-flight rows are skipped by
    the WHERE guard). The caller spawns workers via spawn_translate_worker
    — the flag is already set, so don't go through queue_translation again.

    Race-safety against the worker's claim: the worker's pending→translating
    claim has `WHERE status='pending'`; the worker's terminal success UPDATE
    has `WHERE status='translating'`. The `status != 'translating'` guard
    here skips in-flight rows entirely; the worker's UPDATE still wins.

    Refinement claim (bug hunt 2026-08-04, B5): the reset also demotes
    refinement_status to 'none' (clearing refinement_error) for the rows it
    resets. An in-flight refine worker thereby loses its claim: its commit
    UPDATE is gated on refinement_status='in_progress' and discards the
    whole result (refined_text, ledger merge, drift observation) on
    rowcount 0, so a refinement can no longer land on a chapter already
    reset to 'pending'. refined_text / refined_at stay paired and untouched
    (the reader keeps showing the old body until the retranslate commits,
    which nulls both).

    Crash-window durability: single atomic UPDATE means reset and flag
    commit together or not at all."""
    if not chapter_ids:
        return []
    reset_ids: list[int] = []
    for i in range(0, len(chapter_ids), _QUEUE_BATCH_CHUNK):
        chunk = chapter_ids[i : i + _QUEUE_BATCH_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        # Audit 3.2: zero queue_priority on re-queue so a chapter that was
        # previously "translate-nexted" does not retain a stale high priority
        # when it is re-queued via retranslate. New translate-next clicks will
        # re-assign a fresh MAX+1 priority if the user wants it front again.
        await conn.execute(
            f"UPDATE chapters SET "
            f"status = 'pending', error_msg = NULL, "
            f"force_retranslate = 1, translate_queued = 1, queue_priority = 0, "
            f"translation_degraded = 0, glossary_merge_error = NULL, "
            f"refinement_status = 'none', refinement_error = NULL "
            f"WHERE novel_id = ? AND id IN ({placeholders}) "
            f"  AND status != 'translating'",
            [novel_id, *chunk],
        )
        cur = await conn.execute(
            f"SELECT id FROM chapters "
            f"WHERE novel_id = ? AND id IN ({placeholders}) "
            f"  AND translate_queued = 1 AND status = 'pending'",
            [novel_id, *chunk],
        )
        reset_ids.extend(r["id"] for r in await cur.fetchall())
    await conn.commit()
    return reset_ids


async def prioritize_chapter(
    conn: aiosqlite.Connection, novel_id: int, chapter_num: int
) -> int | None:
    """Move a queued pending chapter to the front of the translate queue.

    Returns the assigned priority, or None when the chapter is not in a
    prioritizable state (not queued, already translating, or done).
    MAX+1 semantics: the most recent translate-next click wins the front.
    The subquery reads MAX across ALL queued chapters (not just this novel)
    so a prioritized chapter beats any other waiting chapter globally.
    Archived novels' rows are excluded from the MAX (B2): archive clears
    their queue flags, but a stale flag on an old DB must not inflate the
    priority baseline.
    """
    cur = await conn.execute(
        "UPDATE chapters SET queue_priority = ("
        "  SELECT COALESCE(MAX(c.queue_priority), 0) + 1 FROM chapters c "
        "  JOIN novels n ON n.id = c.novel_id "
        "  WHERE c.translate_queued = 1 AND n.deleted_at IS NULL"
        ") WHERE novel_id = ? AND chapter_num = ? "
        "  AND translate_queued = 1 AND status = 'pending'",
        (novel_id, chapter_num),
    )
    await conn.commit()
    if (cur.rowcount or 0) == 0:
        return None
    cur = await conn.execute(
        "SELECT queue_priority FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    row = await cur.fetchone()
    return row["queue_priority"] if row else None


async def shutdown() -> None:
    """Cancel and await every in-flight queue worker. Called from main.py's
    lifespan finally-block so subprocess cleanup paths run before the event
    loop is torn down (Claude CLI / Agent kill their child process on
    CancelledError)."""
    if not _registry.tasks:
        return
    tasks = list(_registry.tasks)
    for t in tasks:
        t.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=10.0,
        )
    except asyncio.TimeoutError:
        still_running = sum(1 for t in tasks if not t.done())
        logger.warning(
            "queue shutdown: %d task(s) did not finish within 10s; "
            "an orphan child process may survive if subprocess.kill was not delivered.",
            still_running,
        )


async def drain_on_startup() -> None:
    """Re-spawn worker tasks for every chapter still flagged in the queue.

    Without this, a server restart loses the in-flight queue: orphan recovery
    resets 'translating' → 'pending' but the asyncio tasks that were waiting
    on the lock are gone, so nothing picks the rows back up.

    Also resumes refinement work: reset refinement_status='in_progress' →
    'pending' (server died mid-refinement) then spawn refine workers for
    every 'pending' row.

    Archived novels are excluded end to end (bug hunt 2026-08-04, B2):
    archive_novel clears their queue flags, and the drain additionally
    sweeps any stale flags an older DB still carries (a novel archived
    mid-flight before this fix, or by a pre-fix build) instead of merely
    skipping them, so a later restore cannot resurrect forgotten work.
    """
    async with open_conn() as conn:
        # B2 sweep: clear stale queue/refinement state on ARCHIVED novels'
        # chapters before selecting work. Runs before the in_progress →
        # pending recovery so a mid-refinement archive demotes to 'none'
        # rather than becoming a dormant 'pending'.
        await conn.execute(
            "UPDATE chapters SET translate_queued = 0, queue_priority = 0 "
            "WHERE translate_queued = 1 AND novel_id IN "
            "  (SELECT id FROM novels WHERE deleted_at IS NOT NULL)"
        )
        await conn.execute(
            "UPDATE chapters SET refinement_status = 'none' "
            "WHERE refinement_status IN ('pending', 'in_progress') "
            "  AND novel_id IN "
            "  (SELECT id FROM novels WHERE deleted_at IS NOT NULL)"
        )
        cur = await conn.execute(
            "SELECT c.id, c.novel_id FROM chapters c "
            "JOIN novels n ON n.id = c.novel_id "
            "WHERE c.translate_queued = 1 AND n.deleted_at IS NULL "
            "ORDER BY c.novel_id, c.chapter_num"
        )
        translate_rows = await cur.fetchall()
        # Refinement recovery: reset in_progress → pending. Locked in 2026-05-23
        # over the 'mark error / require manual retry' alternative because the
        # user picked auto-recovery — they don't want a stuck refinement to
        # require manual action. (Archived novels' rows were demoted above.)
        recovered = await conn.execute(
            "UPDATE chapters SET refinement_status = 'pending', "
            "refinement_error = NULL "
            "WHERE refinement_status = 'in_progress'"
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT c.id, c.novel_id FROM chapters c "
            "JOIN novels n ON n.id = c.novel_id "
            "WHERE c.refinement_status = 'pending' AND n.deleted_at IS NULL "
            "ORDER BY c.novel_id, c.chapter_num"
        )
        refine_rows = await cur.fetchall()
    for r in translate_rows:
        _spawn_translate(r["novel_id"], r["id"])
    for r in refine_rows:
        _spawn(_run_refine(r["novel_id"], r["id"]))
    if translate_rows:
        logger.info("queue drain: %d translate tasks resumed", len(translate_rows))
    if recovered.rowcount:
        logger.info(
            "queue drain: %d stuck refinements reset in_progress → pending",
            recovered.rowcount,
        )
    if refine_rows:
        logger.info("queue drain: %d refine tasks resumed", len(refine_rows))


async def _run_translate(novel_id: int, chapter_id: int) -> None:
    """One translate task: wait on the translator lock, then process the row.

    Audit 3.2: once this task holds the lock, it claims the best-ranked
    queued pending chapter rather than necessarily the one it was spawned
    for (queue_priority DESC, then novel_id ASC, chapter_num ASC). Tasks
    equal queued chapters one-to-one, so every queued chapter is still
    processed exactly once; only the binding of task-to-chapter moves from
    spawn time to lock-acquire time. The cancellation map is re-keyed so
    cancel_translate finds the task that is actually translating a given
    chapter.

    Chains into the refinement pass in the SAME lock acquisition when the
    chapter ends up with refinement_status='pending'. Sequencing them under
    one lock keeps the queue strictly serial AND avoids a race where another
    translate task could wedge between the two passes on the same chapter.
    """
    # Initialise with the spawn-time ids so both recovery handlers below
    # can always reference valid identifiers even if the claim SELECT
    # fires before the lock is acquired.
    run_novel_id, run_chapter_id = novel_id, chapter_id
    async with _translator_lock:
        try:
            async with open_conn() as conn:
                # Audit 3.2: while holding the lock, claim the highest-priority
                # queued pending chapter. queue_priority DESC puts user-prioritized
                # chapters first; (novel_id, chapter_num) ASC is the Up-next
                # ordering the /queue page displays. The spawning task was
                # registered under the original chapter_id; re-key the map so
                # cancel_translate can find the task by the chapter it actually
                # processes.
                # Archived novels never claim (B2): archive clears the queue
                # flags, and this join keeps even a stale flag from burning a
                # paid call.
                cur = await conn.execute(
                    "SELECT c.id, c.novel_id FROM chapters c "
                    "JOIN novels n ON n.id = c.novel_id "
                    "WHERE c.translate_queued = 1 AND c.status = 'pending' "
                    "  AND n.deleted_at IS NULL "
                    "ORDER BY c.queue_priority DESC, c.novel_id ASC, "
                    "  c.chapter_num ASC LIMIT 1"
                )
                row = await cur.fetchone()
                if row is not None:
                    run_chapter_id = row["id"]
                    run_novel_id = row["novel_id"]
                if run_chapter_id != chapter_id:
                    # Re-key the cancellation map so cancel_translate finds the
                    # task actually translating this chapter, not the original
                    # spawn-time chapter.
                    t = asyncio.current_task()
                    if t is not None:
                        if _translate_tasks.get(chapter_id) is t:
                            _translate_tasks.pop(chapter_id, None)
                        _translate_tasks[run_chapter_id] = t
                await _translate_chapter_in_db(conn, run_novel_id, run_chapter_id)
                # Same lock, same connection: if the translator just flipped
                # refinement_status='pending', refine now. _refine is a no-op
                # for any other status, so this branch costs a single SELECT
                # when refinement isn't configured.
                await _refine_chapter_in_db(conn, run_novel_id, run_chapter_id)
        except asyncio.CancelledError:
            # User cancelled mid-flight (see cancel_translate). The LLM call,
            # or a chained refine, was interrupted at its await point; nothing
            # was committed. Reset the row so it isn't stuck in 'translating':
            # back to 'done' if a prior translation is still on the row (a
            # cancelled retranslate must not destroy good work), else 'pending'.
            # The translator lock is released by the surrounding `async with`
            # unwinding, so the next queued chapter proceeds immediately. The
            # provider's underlying subprocess may take a few seconds to
            # actually abort, but that does not block the queue.
            logger.info("queue translate ch_id=%d cancelled by user", run_chapter_id)
            try:
                async with open_conn() as recovery:
                    await recovery.execute(
                        "UPDATE chapters SET "
                        "status = CASE WHEN translated_text IS NOT NULL "
                        " AND translated_text != '' THEN 'done' ELSE 'pending' END, "
                        "translate_queued = 0, queue_priority = 0, "
                        "force_retranslate = 0, error_msg = NULL "
                        "WHERE id = ? AND status = 'translating'",
                        (run_chapter_id,),
                    )
                    await recovery.commit()
            except Exception:
                logger.exception(
                    "translate cancel cleanup failed for ch_id=%d; row may be "
                    "stuck in 'translating' until next server restart",
                    run_chapter_id,
                )
            raise
        except Exception:
            logger.exception("queue translate ch_id=%d crashed", run_chapter_id)
            try:
                async with open_conn() as recovery:
                    await recovery.execute(
                        "UPDATE chapters SET "
                        "status = CASE WHEN status = 'translating' THEN 'error' ELSE status END, "
                        "error_msg = CASE WHEN status = 'translating' "
                        " THEN COALESCE(error_msg, 'translator worker crashed') "
                        " ELSE error_msg END, "
                        "translate_queued = 0, queue_priority = 0 "
                        "WHERE id = ?",
                        (run_chapter_id,),
                    )
                    await recovery.commit()
            except Exception:
                logger.exception(
                    "translate recovery cleanup also failed for ch_id=%d; "
                    "queue flag may be stuck until next server restart",
                    run_chapter_id,
                )


async def _emit_tm_inconsistency_observations(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapter_id: int,
) -> None:
    """Initiative 5 — write a `tm_inconsistency` observation row for every
    paragraph in THIS chapter whose source_hash has > 1 distinct target
    rendering across the novel's TM.

    Runs inside the surrounding success-commit transaction so observations
    land atomically with the chapter UPDATE. Idempotent against re-runs:
    the chapter's observation rows were just cleared by the
    DELETE+INSERT cycle above, so we won't double-write.
    """
    # Pull this chapter's TM rows; for each, query the full set of distinct
    # target_text values sharing the source_hash across the novel. If > 1,
    # emit an observation.
    cur = await conn.execute(
        "SELECT paragraph_index, source_text, source_hash, target_text "
        "FROM tm_segments WHERE chapter_id = ? ORDER BY paragraph_index",
        (chapter_id,),
    )
    my_rows = await cur.fetchall()
    for r in my_rows:
        cur = await conn.execute(
            "SELECT DISTINCT target_text FROM tm_segments "
            "WHERE novel_id = ? AND source_hash = ?",
            (novel_id, r["source_hash"]),
        )
        renderings = [row["target_text"] for row in await cur.fetchall()]
        if len(renderings) < 2:
            continue
        obs = implicit_observation_tm_inconsistency(
            source_text=r["source_text"],
            paragraph_index=r["paragraph_index"],
            renderings=renderings,
        )
        await conn.execute(
            "INSERT INTO chapter_observations "
            "(chapter_id, kind, severity, paragraph_index, excerpt) "
            "VALUES (?, ?, ?, ?, ?)",
            (chapter_id, obs.kind, obs.severity, obs.paragraph_index, obs.excerpt),
        )


async def _record_commit_provenance(
    conn: aiosqlite.Connection,
    *,
    novel_id: int,
    chapter_id: int,
    chapter_num: int,
    provider: Provider | None,
    novel_meta: NovelGenreBrief,
    result,
    translation_degraded: bool,
    original_text: str,
    cleaned_text: str,
    free_draft: str | None,
    previous_context: str | None,
    style_note: str | None,
    style_edits: list[tuple[str, str]] | None,
    approved_pairs: list[tuple[int, str, str]] | None = None,
    confirmed_exemplars: list[tuple[str, str]] | None = None,
    fixup_counts: dict | None = None,
) -> None:
    """Post-commit diagnostics for a successful chapter translate: the
    translation-attempts log row, the prompt_config_snapshot provenance blob,
    the fixup self-audit blob, and the TM-segment refresh (+ any
    TM-inconsistency observations).

    All three ride in the SAME transaction as the chapter UPDATE (the caller
    commits afterward) but are best-effort observability that must NEVER fail
    the commit — each guards its own body and only logs on error. Extracted
    from `_translate_chapter_in_db` to keep the worker's
    claim/translate/commit spine readable; it carries no surrounding
    translate-stage state beyond the explicit arguments.
    """
    # F22 (2026-05-25): translation attempts log. One row per
    # _translate_chapter_in_db call — same transaction so a partial
    # attempt can't appear as a phantom row. Backends populate
    # result.prompt_snapshot / result.parse_error when available;
    # NULL on backends that haven't been updated to expose them.
    try:
        from backend.services.translation_attempts import (  # noqa: PLC0415
            record_attempt,
        )
        # Status ladder: the plain-text fallback outranks everything (it means
        # the envelope itself failed); otherwise a 1:1 paragraph-count outcome
        # (count_mismatch_retry = corrective retry recovered,
        # count_mismatch_accepted = second miss committed anyway) is recorded
        # so a count-violating chapter stays queryable; else "ok".
        pc_status = getattr(result, "paragraph_count_status", None)
        if translation_degraded:
            status = "fallback_plaintext"
        elif pc_status in (COUNT_MISMATCH_RETRY, COUNT_MISMATCH_ACCEPTED):
            status = pc_status
        else:
            status = "ok"
        await record_attempt(
            conn,
            chapter_id=chapter_id,
            provider_id=provider.id if provider else None,
            model_id=provider.model_id if provider else None,
            status=status,
            parse_error=getattr(result, "parse_error", None),
            prompt_snapshot=getattr(result, "prompt_snapshot", None),
            retry_count=1 if pc_status else 0,
        )
    except Exception:
        # Diagnostics MUST NOT fail the commit. Log and move on.
        logger.exception(
            "queue: failed to record translation attempt for ch %d",
            chapter_num,
        )
    # Prompt-assembly provenance. Same transaction as the chapter
    # commit so an A/B run can recover "what config produced this
    # row" later via SQL. The blob distinguishes flag-state from
    # block-emitted-state, so a flag-on-but-data-empty translation
    # is queryable separately from a flag-off translation.
    snapshot_json = _build_prompt_config_snapshot(
        provider=provider,
        novel_meta=novel_meta,
        free_draft_included=bool(free_draft and free_draft.strip()),
        previous_context_included=bool(
            previous_context and previous_context.strip()
        ),
        style_note_included=bool(style_note and style_note.strip()),
        style_edits_included=bool(style_edits),
        approved_translations_included=bool(approved_pairs),
        confirmed_exemplars_included=bool(confirmed_exemplars),
        # F4: a degraded commit came from the plain-text fallback prompt,
        # which ships none of the blocks; the builder forces the *_included
        # keys false so the snapshot describes the prompt that actually ran.
        plain_fallback=translation_degraded,
    )
    # Fixup self-audit: record which deterministic enforce_* fixups rewrote the
    # model output and by how much, in the same UPDATE as the snapshot. Makes
    # the post-LLM override layer queryable (see chapters.fixup_audit) so a
    # fixup can't silently rewrite correct output without leaving a record.
    import json  # noqa: PLC0415
    fixup_json = json.dumps(fixup_counts, sort_keys=True) if fixup_counts else None
    await conn.execute(
        "UPDATE chapters SET prompt_config_snapshot = ?, fixup_audit = ? "
        "WHERE id = ? AND novel_id = ?",
        (snapshot_json, fixup_json, chapter_id, novel_id),
    )
    # Initiative 5: refresh the TM rows for this chapter in the
    # same transaction. Failed alignment skips the chapter
    # silently — better than persisting wrong-paragraph pairs.
    try:
        n_segments = await tm_svc.replace_chapter_segments(
            conn, novel_id, chapter_id,
            original_text, cleaned_text,
        )
        if n_segments:
            logger.info(
                "tm: chapter %d populated %d segments",
                chapter_num, n_segments,
            )
            # Surface inconsistencies created by THIS chapter into the
            # QA panel as additional observation rows. We only check
            # source_hashes this chapter introduced (joining against
            # the rows that share them across the novel), so
            # unchanged chapters don't fire fresh observations.
            await _emit_tm_inconsistency_observations(
                conn, novel_id, chapter_id
            )
    except Exception:
        # TM write is best-effort observability — never fail the
        # chapter commit because of it. The translation itself is
        # what the user needs; the concordance index can recover
        # on the next retranslate.
        logger.exception(
            "tm: chapter %d populate failed; chapter commit "
            "proceeding without TM rows", chapter_num,
        )


def _apply_text_fixups(
    result, glossary, chapter_num: int, title_zh: str | None = None,
) -> tuple[str | None, str, dict]:
    """Run the deterministic post-translation text fixups (no LLM) and return
    (title_en, cleaned_text, fixup_counts).

    Two groups: casing/strip fixups land on result.translated_text, then the
    em-dash / bracket / sentence-initial fixups produce the final committed
    body. The second group runs BEFORE the observers so detectors see the same
    text the reader will, preserving the QA dashboard's "observers run on the
    final committed body" invariant.

    `fixup_counts` is {"rules": {rule_name: count}, "total": N} over the rules
    that actually rewrote text (zero-count rules omitted). It is persisted to
    chapters.fixup_audit so the deterministic override layer is queryable: a
    fixup can no longer silently rewrite correct model output without leaving a
    per-chapter record.
    """
    # Normalize line endings to LF before the LF-assuming fixup/observer chain.
    # The source side is normalized in parser.py; the LLM body is not (the
    # subprocess backends decode bytes without universal-newline translation),
    # so this establishes the invariant the paragraph-split fixups depend on.
    result.translated_text = (
        result.translated_text.replace("\r\n", "\n").replace("\r", "\n")
    )
    text, ts_n = strip_leading_title_line(result.translated_text, result.title_en)
    text, lt_n = enforce_locked_term_casing(text, glossary)
    text, lc_n = enforce_lowercase_locked_terms(text, glossary)
    text, sb_n = enforce_stem_branch_casing(text)
    text, cm_n = strip_chapter_end_marker(text)
    result.translated_text = text
    if ts_n + lt_n + lc_n + sb_n + cm_n:
        logger.info(
            "queue: chapter %d post-fixes: %d (title-strip) + %d (locked-case) "
            "+ %d (lowercase) + %d (stem-branch) + %d (end-marker)",
            chapter_num, ts_n, lt_n, lc_n, sb_n, cm_n,
        )

    title_en = normalize_title_en(result.title_en, chapter_num, title_zh=title_zh)
    cleaned_text, em_count = enforce_em_dash(result.translated_text)
    cleaned_text, sh_count = enforce_spaced_hyphen_dash(cleaned_text)
    # Brackets BEFORE balance: stripping a bold-wrapped span can expose a stray
    # `**` (whitespace before the closing marker defeats the fixed-offset
    # check), so emphasis balancing must run last among the `**`-mutating passes.
    cleaned_text, brk_count = enforce_brackets(cleaned_text, glossary=glossary)
    cleaned_text, emph_count = enforce_balanced_emphasis(cleaned_text)
    cleaned_text, si_count = enforce_sentence_initial_capitalization(cleaned_text)
    cleaned_text, mc_count = enforce_mid_sentence_comma_break(cleaned_text)
    if em_count or sh_count or emph_count or brk_count or si_count or mc_count:
        logger.info(
            "queue: chapter %d translate guardrails: %d em-dash, %d spaced-hyphen, "
            "%d emphasis, %d bracket, %d sentence-initial, %d comma-break-join fix(es)",
            chapter_num, em_count, sh_count, emph_count, brk_count, si_count, mc_count,
        )
    rules = {
        name: n for name, n in (
            ("title_strip", ts_n), ("locked_case", lt_n), ("lowercase", lc_n),
            ("stem_branch", sb_n), ("end_marker", cm_n), ("em_dash", em_count),
            ("spaced_hyphen", sh_count), ("brackets", brk_count),
            ("emphasis", emph_count), ("sentence_initial", si_count),
            ("comma_break", mc_count),
        ) if n
    }
    fixup_counts = {"rules": rules, "total": sum(rules.values())}
    return title_en, cleaned_text, fixup_counts


async def _translate_chapter_in_db(
    conn: aiosqlite.Connection, novel_id: int, chapter_id: int
) -> None:
    """Translate one chapter: claim the row, call the LLM, write result.

    Single-pass shape: one LLM call (with extended thinking on claude_agent),
    deterministic text fixups, atomic success commit. Guardrail hits are
    logged as observations only — they do not mark the chapter degraded.
    `translation_degraded` collapses to "the translator's plain-text
    fallback was used"."""
    cur = await conn.execute(
        "SELECT chapter_num, title_zh, original_text, status, translate_queued, "
        "force_retranslate, free_draft_text, import_source_url FROM chapters "
        "WHERE id = ? AND novel_id = ?",
        (chapter_id, novel_id),
    )
    r = await cur.fetchone()
    if r is None:
        await _clear_translate_queue(conn, chapter_id)
        return
    if not r["translate_queued"]:
        return
    if r["status"] == "done":
        await _clear_translate_queue(conn, chapter_id)
        return
    if not (r["original_text"] or "").strip():
        # Import skeletons (resumable scrapes) sit at original_text='' until
        # the fill phase commits the fetch. An empty CHAPTER block sent to the
        # LLM yields an invented chapter that would commit as 'done', so error
        # the row instead of calling the translator.
        msg = (
            "chapter source not yet fetched (import incomplete); "
            "resume the import, then retranslate"
            if r["import_source_url"]
            else "chapter source text is empty"
        )
        # Audit 3.2: also zero queue_priority so the translate-next priority
        # does not linger on an error row that the user later re-queues.
        await conn.execute(
            "UPDATE chapters SET status = 'error', error_msg = ?, "
            "translate_queued = 0, queue_priority = 0, force_retranslate = 0 "
            "WHERE id = ? AND novel_id = ? AND status != 'translating'",
            (msg, chapter_id, novel_id),
        )
        await conn.commit()
        return
    claim = await conn.execute(
        "UPDATE chapters SET status = 'translating' "
        "WHERE id = ? AND novel_id = ? AND status = 'pending'",
        (chapter_id, novel_id),
    )
    # Commit the claim NOW: between here and the success UPDATE the worker
    # must only SELECT. Nothing may hold a write transaction across the LLM
    # await, or editor writes (and every other writer) would block for the
    # whole multi-minute call.
    await conn.commit()
    if (claim.rowcount or 0) == 0:
        # See predecessor commit comment: don't reuse _clear_translate_queue
        # here — a concurrent /retranslate could race the unconditional clear.
        # The status != 'pending' guard makes this atomic w.r.t. retranslate.
        # Audit 3.2: also zero queue_priority so a translate-next priority
        # does not persist on a chapter that was already processed elsewhere.
        await conn.execute(
            "UPDATE chapters SET translate_queued = 0, queue_priority = 0 "
            "WHERE id = ? AND novel_id = ? AND status != 'pending'",
            (chapter_id, novel_id),
        )
        await conn.commit()
        return
    # Initialized before the try so the error handler can still record an
    # attempt row (and provider attribution) when the failure happens before
    # provider resolution below.
    provider: Provider | None = None
    try:
        # Initiative 3: union of per-novel + global glossary. Per-novel
        # entries (locked or auto) shadow any global entry on the same term.
        # The composer stamps scope='novel'/'global' so format_glossary can
        # render visible precedence labels in the prompt.
        glossary = await global_glossary_svc.list_for_novel_with_globals(
            conn, novel_id
        )
        previous_context = await fetch_previous_chapter_tail(
            conn, novel_id, r["chapter_num"]
        )
        # USER STYLE PREFERENCES feed: EN before-after correction pairs,
        # CAT-editor segment edits first then legacy style_edits rows (the
        # merge lives in the fetcher). This chapter's own edits are excluded;
        # they ride the APPROVED TRANSLATIONS block below instead.
        style_edits = await fetch_style_edits(
            conn, novel_id, exclude_chapter_id=chapter_id
        )
        style_note = await fetch_style_note(conn, novel_id)
        provider = await resolve_translator_provider(conn, novel_id)
        novel_meta = await fetch_novel_genre_brief(conn, novel_id)
        # PEMT layer: pass the mechanical NMT free draft (Google Translate)
        # to the LLM as a fidelity reference. NULL when the draft hasn't run
        # yet (or failed); the prompt omits the REFERENCE TRANSLATION section
        # in that case. PROMPT_INCLUDE_FREE_DRAFT=false is the A/B kill-switch
        # for the REFERENCE TRANSLATION block.
        free_draft = r["free_draft_text"] if config.PROMPT_INCLUDE_FREE_DRAFT else None
        # Author update markers (（第四更！）, 求月票) are upload metadata, not
        # title content: strip them from the prompt inputs (the CHAPTER TITLE
        # line and the heading echoed at the top of the body) so the model
        # never translates one into TITLE_EN. Prompt-time only — the stored
        # title_zh / original_text keep the source verbatim.
        prompt_title_zh = strip_title_update_marker(r["title_zh"]) or None
        # 1:1 contract (CAT Phase 1): the model reads the pre-joined effective
        # source paragraphs (heading dropped: the title travels on the prompt's
        # title line; mid-sentence blank-line breaks joined deterministically;
        # author update markers stripped from a surviving heading line) and
        # must return exactly one output paragraph per source paragraph.
        # Prompt-time only: stored original_text stays verbatim.
        # chapter_source_paragraphs is THE canonical recipe shared with the
        # segments service so every chapter_segments writer keys on the same
        # source list (SEGMENTATION_VERSION 2).
        source_paragraphs = chapter_source_paragraphs(r["original_text"])
        if source_paragraphs:
            prompt_source = "\n\n".join(source_paragraphs)
            expected_paragraph_count: int | None = len(source_paragraphs)
        else:
            # Degenerate chapter (e.g. heading-only body): keep the legacy
            # verbatim prompt (marker-stripped) and skip the count validation.
            # Because validation is OFF here, the branch also bypasses the
            # LLM cache in both directions (cacheable=False below, F9): the
            # raw-text prompt could key-collide with an armed 1:1 prompt of
            # identical bytes, and a cache LOAD skips check_paragraph_count,
            # so an unvalidated envelope must never cross into (or out of)
            # the validated path. Rare path; the lost caching is noise.
            prompt_source = strip_heading_update_marker(r["original_text"])
            expected_paragraph_count = None
        # CAT Phase 4: APPROVED TRANSLATIONS block feed. Human rows of this
        # chapter plus cross-chapter exact confirmed matches, listed in the
        # prompt with a verbatim-reuse instruction. Coherence aid only; the
        # deterministic merge below is the enforcement, so this pre-LLM read
        # racing an editor write is harmless (the merge re-reads inside the
        # commit transaction).
        # Fetched UNCONDITIONALLY when source paragraphs exist (F5, bug hunt
        # 2026-08-04): the fetched set also drives the exemplar/style
        # dedupes below, so the flag must gate ONLY the block's inclusion in
        # the prompt (and the snapshot). Pre-fix, flag-off skipped the fetch
        # and therefore un-deduped the exemplar and style blocks too: a
        # three-block delta that made the A/B arm uninterpretable.
        approved_pairs: list[tuple[int, str, str]] | None = None
        if source_paragraphs:
            approved_pairs = await segments_svc.approved_prompt_pairs(
                conn, novel_id, chapter_id, source_paragraphs
            ) or None
        # CAT Phase 5: APPROVED TRANSLATION EXAMPLES block feed. Three
        # ledger-fed blocks divide the roles: APPROVED TRANSLATIONS above
        # lists verbatim-reuse renderings for THIS chapter's own paragraphs;
        # exemplars teach voice from OTHER chapters' recently confirmed
        # zh->EN pairs; USER STYLE PREFERENCES (style_edits above) teaches
        # correction patterns as EN before-after pairs from edited segments.
        # All can appear in the same prompt. The flag gate lives inside the
        # fetcher (sibling pattern).
        confirmed_exemplars = await fetch_confirmed_exemplars(
            conn, novel_id, chapter_id
        ) or None
        if confirmed_exemplars and approved_pairs:
            # A confirmed source that also recurs in THIS chapter already
            # rides the approved block (cross-chapter exact match), so the
            # exemplar copy is redundant; drop it. Both sides compare on
            # pair_side_key (F11): the renderers strip + collapse newlines
            # before the shared truncation, so a raw-slice key would miss
            # whitespace/newline variants that render identically.
            approved_srcs = {
                pair_side_key(zh) for _i, zh, _en in approved_pairs
            }
            confirmed_exemplars = [
                (zh, en) for zh, en in confirmed_exemplars
                if pair_side_key(zh) not in approved_srcs
            ] or None
        if style_edits and approved_pairs:
            # Same dedupe for the style arm: a segment pair whose AFTER text
            # already rides this chapter's approved block would show the
            # model the identical rendering twice (correction example +
            # verbatim-reuse row). Same pair_side_key comparison (F11).
            approved_ens = {
                pair_side_key(en) for _i, _zh, en in approved_pairs
            }
            style_edits = [
                (before, after) for before, after in style_edits
                if pair_side_key(after) not in approved_ens
            ]
        # The flag gates ONLY whether the approved block ships (prompt and
        # snapshot); the dedupes above already consumed the fetched set (F5).
        if not config.PROMPT_INCLUDE_APPROVED_TRANSLATIONS:
            approved_pairs = None
        translate_t0 = time.perf_counter()
        result = await translate_chapter(
            prompt_source, prompt_title_zh, glossary,
            previous_context=previous_context,
            style_edits=style_edits,
            use_cache=not r["force_retranslate"],
            style_note=style_note,
            provider=provider,
            genre=novel_meta["genre"],
            custom_brief=novel_meta["custom_style_brief"],
            free_draft=free_draft,
            source_language=novel_meta["source_language"],
            expected_paragraph_count=expected_paragraph_count,
            approved_pairs=approved_pairs,
            confirmed_exemplars=confirmed_exemplars,
            cacheable=bool(source_paragraphs),
        )
        logger.info(
            "queue: chapter %d translate stage %.1fs",
            r["chapter_num"], time.perf_counter() - translate_t0,
        )
        # Observer convention: mismatch outcomes are logged, never retried
        # further here. The attempt row records the same status via
        # _record_commit_provenance so it stays queryable.
        paragraph_count_status = getattr(result, "paragraph_count_status", None)
        if paragraph_count_status:
            logger.info(
                "queue: chapter %d paragraph count %s (source has %s paragraphs)",
                r["chapter_num"], paragraph_count_status, expected_paragraph_count,
            )

        # Pure deterministic text fixups (casing, em-dash, brackets, title
        # normalization). No LLM. Returns the canonical title + committed body.
        title_en, cleaned_text, fixup_counts = _apply_text_fixups(
            result, glossary, r["chapter_num"], title_zh=r["title_zh"],
        )

        # translation_degraded now reflects ONLY the plain-text fallback case.
        # Guardrail hits log but do not flag.
        translation_degraded = result.degraded
        if translation_degraded:
            logger.info(
                "queue: chapter %d marked degraded (translator fallback path)",
                r["chapter_num"],
            )

        # Token usage. Only present on a fresh translation (cache hits and
        # providers that don't emit usage leave it None); preserve the
        # chapter's existing usage columns when this call didn't produce new
        # ones, so a force-retranslate that hits the cache doesn't blank out
        # the original record.
        input_tokens = output_tokens = cached_input_tokens = None
        if result.usage is not None and (
            result.usage.input_tokens or result.usage.output_tokens
        ):
            input_tokens = result.usage.input_tokens
            output_tokens = result.usage.output_tokens
            cached_input_tokens = result.usage.cached_input_tokens

        # Atomic success commit. Also flag the chapter for the refinement
        # pass when the novel has refinement_provider_id set — single
        # transaction so a crash between translator-commit and pending-flag
        # cannot leave a chapter with status='done' but no refinement signal.
        refinement_pending = (
            config.PROMPT_INCLUDE_REFINER
            and await _novel_has_refinement_provider(conn, novel_id)
        )
        new_refinement_status = "pending" if refinement_pending else "none"
        # translated_by_provider_id is stamped on every successful commit so
        # the reader's banner copy (e.g. "free-tier rough draft" vs "polished")
        # can branch on the provider that actually produced this row, without
        # re-deriving from novels.translator_provider_id (which can change
        # later — see refined_by_provider_id for the same pattern).
        translated_by_id = provider.id if provider is not None else None
        # One success-commit UPDATE. The only difference between the
        # fresh-usage and cache-hit paths is whether the token columns are
        # written: on a cache hit (input_tokens is None) we leave them alone so
        # the original record isn't blanked; on a fresh translation we overwrite
        # them with this call's counts. Build the SET list + params once with
        # the token clause inserted conditionally.
        # Audit 3.2: clear queue_priority alongside translate_queued on the
        # success commit so a priority assigned by translate-next is consumed
        # exactly once and never leaks to a future retranslate of this chapter.
        set_clauses = [
            "title_en = ?", "translated_text = ?", "status = 'done'",
            "error_msg = NULL", "force_retranslate = 0",
            "translation_degraded = ?", "translate_queued = 0",
            "queue_priority = 0",
            "refinement_status = ?", "refined_text = NULL",
            "refinement_error = NULL", "refined_at = NULL",
        ]
        params: list = [
            title_en, cleaned_text,
            1 if translation_degraded else 0,
            new_refinement_status,
        ]
        if input_tokens is not None:
            set_clauses += [
                "input_tokens = ?", "output_tokens = ?", "cached_input_tokens = ?",
            ]
            params += [input_tokens, output_tokens, cached_input_tokens]
        set_clauses += ["translated_by_provider_id = ?", "translated_at = datetime('now')"]
        params += [translated_by_id, chapter_id, novel_id]
        upd = await conn.execute(
            "UPDATE chapters SET " + ", ".join(set_clauses)
            + " WHERE id = ? AND novel_id = ? AND status = 'translating'",
            tuple(params),
        )
        if (upd.rowcount or 0) == 0:
            # Lost the claim (concurrent state change). Nothing may commit:
            # roll the transaction back and discard the result.
            await conn.rollback()
            logger.info(
                "translate ch %d completed but row was no longer 'translating'; "
                "discarding result",
                r["chapter_num"],
            )
            await _clear_translate_queue(conn, chapter_id)
            return

        # CAT Phase 4 worker merge, INSIDE the claim-guarded success
        # transaction (the success UPDATE just above opened the write
        # transaction; the status claim was committed long ago, before the
        # LLM call). The merge therefore re-reads human rows after the
        # write lock is held, so an editor write cannot interleave between
        # the read and this commit. Human rows keep their target_text
        # VERBATIM and only refresh machine_text; machine rows regenerate
        # with origin 'llm'; cross-chapter exact confirmed matches pre-fill
        # as origin 'tm_exact'. The FINAL committed body is the merged join,
        # so a retranslate can no longer clobber confirmed work.
        final_text = cleaned_text
        if source_paragraphs:
            merge_prefill = await segments_svc.prefill_confirmed_exact(
                conn, novel_id, chapter_id, source_paragraphs
            )
            merged = await segments_svc.apply_machine_translation(
                conn,
                novel_id=novel_id,
                chapter_id=chapter_id,
                new_paragraphs=split_target_paragraphs(cleaned_text),
                kind="llm",
                src_paras=source_paragraphs,
                prefill=merge_prefill,
            )
            final_text = segments_svc.join_paragraphs(merged)
            if final_text != cleaned_text:
                await conn.execute(
                    "UPDATE chapters SET translated_text = ? "
                    "WHERE id = ? AND novel_id = ?",
                    (final_text, chapter_id, novel_id),
                )

        # Observations only — no retry, no degraded mark. Computed on the
        # FINAL merged body (invariant: observers see what the reader sees).
        # The single-pass thesis is that noticing happens inside the
        # translator's thinking phase; a retry is just two shallow passes
        # for the same deficit.
        observation_messages = list(body_correctness_observations(
            r["original_text"], final_text, glossary,
        ))
        for zh, en in glossary_svc.missing_translator_terms(
            r["title_zh"] or "", title_en or "", glossary, atomic_only=True,
        ):
            observation_messages.append(f'missing title glossary term {zh!r} → {en!r}')
        observation_messages.extend(
            detect_glossary_predicate_loss(
                r["title_zh"] or "", title_en or "", glossary,
                source_label="chapter title",
            )
        )
        if observation_messages:
            logger.info(
                "queue: chapter %d translation observations (logged, not retried) [%d]: %s",
                r["chapter_num"], len(observation_messages),
                "; ".join(observation_messages[:5]),
            )

        # Persist the normalized observation set. translation_degraded gets
        # its own synthetic row so the panel renders it uniformly with the
        # detect_* hits. The DELETE-then-INSERT rides the same transaction
        # as the chapter UPDATE above — atomic replacement, no
        # mixed-generation window.
        normalized_observations: list[NormalizedObservation] = list(
            normalize_observer_outputs(observation_messages)
        )
        if translation_degraded:
            normalized_observations.append(
                implicit_observation_translation_degraded()
            )
        # 1:1 drift signal (CAT Phase 1): the deterministic fixups and the
        # worker merge run AFTER the count validation, so the committed body
        # can diverge from the validated count (accepted double miss, or a
        # fixup weld/strip that changed the count). Observation only, no
        # retry: keeps the violation rate measurable and marks the chapter
        # as positionally unmappable.
        if expected_paragraph_count is not None:
            committed_count = len(split_target_paragraphs(final_text))
            if committed_count != expected_paragraph_count:
                logger.info(
                    "queue: chapter %d paragraph count drift after fixups "
                    "(%d committed, %d expected)",
                    r["chapter_num"], committed_count, expected_paragraph_count,
                )
                normalized_observations.append(
                    implicit_observation_paragraph_count_drift(
                        got=committed_count,
                        expected=expected_paragraph_count,
                        stage="translation",
                    )
                )
        # F26 (2026-05-25): per-novel observer mute. Read
        # novels.disabled_observers (JSON array of kinds) and filter the
        # observation list before persistence. Lets users mute false-
        # positive observer categories per-novel without losing the
        # other observers' signal.
        cur = await conn.execute(
            "SELECT disabled_observers FROM novels WHERE id = ?", (novel_id,),
        )
        mute_row = await cur.fetchone()
        muted = parse_disabled_observers(
            mute_row["disabled_observers"] if mute_row else None
        )
        if muted:
            normalized_observations = [
                o for o in normalized_observations if o.kind not in muted
            ]

        await conn.execute(
            "DELETE FROM chapter_observations WHERE chapter_id = ?",
            (chapter_id,),
        )
        if normalized_observations:
            await conn.executemany(
                "INSERT INTO chapter_observations "
                "(chapter_id, kind, severity, paragraph_index, excerpt) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        chapter_id, obs.kind, obs.severity,
                        obs.paragraph_index, obs.excerpt,
                    )
                    for obs in normalized_observations
                ],
            )
        # Best-effort post-commit diagnostics (attempt log +
        # prompt_config_snapshot provenance + TM-segment refresh), all in
        # this same transaction. Extracted so the claim/translate/commit
        # spine stays readable; the helper never fails the commit.
        await _record_commit_provenance(
            conn,
            novel_id=novel_id,
            chapter_id=chapter_id,
            chapter_num=r["chapter_num"],
            provider=provider,
            novel_meta=novel_meta,
            result=result,
            translation_degraded=translation_degraded,
            original_text=r["original_text"],
            cleaned_text=final_text,
            free_draft=free_draft,
            previous_context=previous_context,
            style_note=style_note,
            style_edits=style_edits,
            approved_pairs=approved_pairs,
            confirmed_exemplars=confirmed_exemplars,
            fixup_counts=fixup_counts,
        )
        await conn.commit()

        # Glossary merge runs after success commit so a merge failure does
        # not bury the translation. The merge can still fail (SQLite write
        # lock, transient I/O) — persist the failure on the row so the reader
        # can surface a banner.
        try:
            glossary_candidates = glossary_svc.filter_glossary_candidates(
                r["original_text"], result.new_terms
            )
            if glossary_candidates:
                await glossary_svc.merge_new_terms(
                    conn, novel_id, glossary_candidates
                )
            await conn.execute(
                "UPDATE chapters SET glossary_merge_error = NULL WHERE id = ?",
                (chapter_id,),
            )
            await conn.commit()
        except Exception as merge_err:
            logger.exception(
                "glossary merge for ch %d failed; translation committed without "
                "new-terms update", r["chapter_num"]
            )
            try:
                await conn.execute(
                    "UPDATE chapters SET glossary_merge_error = ? WHERE id = ?",
                    (str(merge_err)[:4000], chapter_id),
                )
                # Surface the merge failure as an observation alongside the
                # detect_* hits so the reader's QA panel renders it uniformly.
                # Same transaction as the glossary_merge_error column update.
                synthetic = implicit_observation_glossary_merge_error(
                    str(merge_err)[:4000]
                )
                await conn.execute(
                    "INSERT INTO chapter_observations "
                    "(chapter_id, kind, severity, paragraph_index, excerpt) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        chapter_id, synthetic.kind, synthetic.severity,
                        synthetic.paragraph_index, synthetic.excerpt,
                    ),
                )
                await conn.commit()
            except Exception:
                logger.exception("could not even persist glossary_merge_error")
    except Exception as e:
        logger.exception("translate ch %d failed: %s", r["chapter_num"], e)
        # A failed translation must not touch the segment store (invariant):
        # discard any uncommitted success-transaction writes (claim UPDATE,
        # partial Phase 4 merge) before writing the error state. No-op when
        # no transaction is open.
        try:
            await conn.rollback()
        except Exception:
            logger.exception("translate error-path rollback failed")
        # Audit 3.2: zero queue_priority on error so a translate-next priority
        # does not persist on a failed chapter that the user later re-queues.
        await conn.execute(
            "UPDATE chapters SET status = 'error', error_msg = ?, "
            "translate_queued = 0, queue_priority = 0, force_retranslate = 0 "
            "WHERE id = ? AND novel_id = ? AND status = 'translating'",
            (str(e)[:4000], chapter_id, novel_id),
        )
        # F22: record an 'error' attempt row in the same transaction so the
        # provider's failure_rate_30d metric reflects real failures instead of
        # staying pinned at 0. Best-effort; a diagnostics write must never mask
        # the original failure or block the error commit.
        try:
            from backend.services.translation_attempts import (  # noqa: PLC0415
                record_attempt,
            )
            await record_attempt(
                conn,
                chapter_id=chapter_id,
                provider_id=provider.id if provider else None,
                model_id=provider.model_id if provider else None,
                status="error",
                parse_error=str(e)[:4000],
                prompt_snapshot=None,
                retry_count=0,
            )
        except Exception:
            logger.exception(
                "queue: failed to record error attempt for ch %d",
                r["chapter_num"],
            )
        await conn.commit()


# ============================================================================
# Refinement worker
# ============================================================================

async def _novel_has_refinement_provider(
    conn: aiosqlite.Connection, novel_id: int
) -> bool:
    """True when the novel has a non-NULL refinement_provider_id.

    Read from the schema column; if the column doesn't exist (old DB) treat
    as False so the legacy code path stays intact."""
    try:
        cur = await conn.execute(
            "SELECT refinement_provider_id FROM novels WHERE id = ?",
            (novel_id,),
        )
        row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return False
    return row is not None and row["refinement_provider_id"] is not None


async def _resolve_refinement_provider(
    conn: aiosqlite.Connection, novel_id: int
) -> Provider | None:
    """Load the novel's refinement Provider. Returns None when unset."""
    try:
        cur = await conn.execute(
            "SELECT refinement_provider_id FROM novels WHERE id = ?",
            (novel_id,),
        )
        row = await cur.fetchone()
    except aiosqlite.OperationalError:
        return None
    if row is None or row["refinement_provider_id"] is None:
        return None
    provider = await load_provider(row["refinement_provider_id"])
    if provider is None:
        logger.warning(
            "novel %d references refinement provider %d but the row is gone",
            novel_id, row["refinement_provider_id"],
        )
    return provider


async def _refine_chapter_in_db(
    conn: aiosqlite.Connection, novel_id: int, chapter_id: int
) -> None:
    """Run the refinement pass on a chapter that's been flagged
    refinement_status='pending'. No-op when status is anything else (the
    chapter may have been refined already, errored out, or never had a
    refiner configured). Single LLM call via refiner.refine_chapter.
    """
    cur = await conn.execute(
        "SELECT chapter_num, translated_text, original_text, refinement_status "
        "FROM chapters WHERE id = ? AND novel_id = ?",
        (chapter_id, novel_id),
    )
    r = await cur.fetchone()
    if r is None or r["refinement_status"] != "pending":
        return
    draft = r["translated_text"] or ""
    if len(draft.strip()) < _REFINEMENT_MIN_DRAFT_CHARS:
        # Empty or tiny drafts — most commonly a parse error or a 番外 /
        # author-note row that snuck through the heading detector. Refining
        # them just burns a paid round-trip for ≤ a paragraph of text the
        # reader will display as-is anyway. Skip and clear pending.
        #
        # Safe interaction with `translation_degraded` (audited 2026-05-23):
        # we only mutate `refinement_status` here, leaving the degraded flag
        # set by the translate step untouched. The reader's quality banner
        # (`applyQualityBanner`) keys off `translation_degraded` alone and
        # the refinement banner (`applyRefinementBanner`) keys off
        # `refinement_status` alone, so a short degraded chapter still
        # surfaces the degraded warning.
        logger.info(
            "refine ch %d: draft is %d chars (< %d); marking 'none' and skipping",
            r["chapter_num"], len(draft.strip()), _REFINEMENT_MIN_DRAFT_CHARS,
        )
        await conn.execute(
            "UPDATE chapters SET refinement_status = 'none' WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()
        return
    provider = await _resolve_refinement_provider(conn, novel_id)
    if provider is None:
        # User cleared refinement_provider_id between the translator's
        # commit and now. Clear the pending flag and move on.
        logger.info(
            "refine ch %d: no refinement provider configured; clearing pending",
            r["chapter_num"],
        )
        await conn.execute(
            "UPDATE chapters SET refinement_status = 'none' WHERE id = ?",
            (chapter_id,),
        )
        await conn.commit()
        return
    # Atomic claim: pending → in_progress only if currently pending.
    claim = await conn.execute(
        "UPDATE chapters SET refinement_status = 'in_progress' "
        "WHERE id = ? AND refinement_status = 'pending'",
        (chapter_id,),
    )
    await conn.commit()
    if (claim.rowcount or 0) == 0:
        return
    # Initiative 3: refiner sees the same chapter-filtered union as the
    # translator so it doesn't replace a global term's rendering during the
    # polish pass. Filtering is by chapter relevance only, never by lock
    # status (the 2026-05-23 locked-AND-auto rule stands): the English draft
    # is the primary haystack since that is the text being edited, with the
    # Chinese source as the trad/simp-folded safety net. Without this filter
    # every refine call shipped the full glossary (2k+ entries, ~63 KB on the
    # live novel) for a chapter that contains a few dozen of them.
    glossary = await global_glossary_svc.list_for_novel_with_globals(
        conn, novel_id
    )
    glossary = glossary_svc.filter_glossary_for_chapter(
        glossary, draft, r["original_text"] or ""
    )
    # 1:1 contract (CAT Phase 1): the refinement must return exactly one
    # output paragraph per draft paragraph. refine_chapter validates before
    # its cache store and raises ParagraphCountMismatch; the ladder here is
    # one corrective retry, then DISCARD (the draft costs nothing to fall
    # back to, so no accept-and-flag arm like the translator has).
    expected_paragraphs = len(split_target_paragraphs(draft))
    refine_t0 = time.perf_counter()
    try:
        try:
            refined = await refine_chapter(
                draft, provider, glossary=glossary,
                expected_paragraph_count=expected_paragraphs,
            )
        except ParagraphCountMismatch as first_miss:
            logger.info(
                "refine ch %d: refiner returned %d paragraphs for a "
                "%d-paragraph draft; corrective retry",
                r["chapter_num"], first_miss.got, first_miss.expected,
            )
            refined = await refine_chapter(
                draft, provider, glossary=glossary,
                expected_paragraph_count=expected_paragraphs,
                corrective_note=build_count_corrective(
                    first_miss.got, first_miss.expected, source_label="draft",
                ),
            )
    except ParagraphCountMismatch as second_miss:
        logger.info(
            "refine ch %d: corrective retry still returned %d paragraphs for "
            "a %d-paragraph draft; discarding the refinement (reader falls "
            "back to the draft)",
            r["chapter_num"], second_miss.got, second_miss.expected,
        )
        await conn.execute(
            "UPDATE chapters SET refinement_status = 'error', "
            "refinement_error = ? "
            "WHERE id = ? AND refinement_status = 'in_progress'",
            ("refiner broke paragraph alignment", chapter_id),
        )
        await conn.commit()
        return
    except Exception as e:
        logger.exception(
            "refine ch %d failed: %s", r["chapter_num"], e,
        )
        await conn.execute(
            "UPDATE chapters SET refinement_status = 'error', "
            "refinement_error = ? "
            "WHERE id = ? AND refinement_status = 'in_progress'",
            (str(e)[:4000], chapter_id),
        )
        await conn.commit()
        return
    elapsed = time.perf_counter() - refine_t0
    # Apply the same deterministic guardrails the translator output runs
    # through. The refiner's prompt asks it not to introduce em-dashes,
    # mutate locked terms, or break bracket formatting, but LLMs slip;
    # without these the refined body can regress after a clean draft.
    refined = refined.replace("\r\n", "\n").replace("\r", "\n")
    refined, lt_n = enforce_locked_term_casing(refined, glossary)
    refined, lc_n = enforce_lowercase_locked_terms(refined, glossary)
    refined, sb_n = enforce_stem_branch_casing(refined)
    refined, cm_n = strip_chapter_end_marker(refined)
    refined, em_n = enforce_em_dash(refined)
    refined, sh_n = enforce_spaced_hyphen_dash(refined)
    # Brackets BEFORE balance (see _apply_text_fixups): balance must run last
    # among the `**`-mutating passes so a bracket-strip can't leave a stray `**`.
    refined, brk_n = enforce_brackets(refined, glossary=glossary)
    refined, emph_n = enforce_balanced_emphasis(refined)
    refined, si_n = enforce_sentence_initial_capitalization(refined)
    refined, mc_n = enforce_mid_sentence_comma_break(refined)
    if lt_n + lc_n + sb_n + cm_n + em_n + sh_n + emph_n + brk_n + si_n + mc_n:
        logger.info(
            "refine ch %d post-fixes on refined text: %d locked-case, "
            "%d lowercase, %d stem-branch, %d end-marker, %d em-dash, "
            "%d spaced-hyphen, %d emphasis, %d bracket, %d sentence-initial, "
            "%d comma-break-join",
            r["chapter_num"], lt_n, lc_n, sb_n, cm_n, em_n, sh_n, emph_n, brk_n, si_n, mc_n,
        )
    logger.info(
        "refine ch %d done in %.1fs (provider=%s, %d → %d chars)",
        r["chapter_num"], elapsed, provider.name, len(draft), len(refined),
    )
    # Extend the prompt-config snapshot so the refined chapter records which
    # refiner produced the final visible English. Tolerant of a missing /
    # malformed translator-side snapshot so legacy rows still get refiner
    # provenance recorded.
    snap_cur = await conn.execute(
        "SELECT prompt_config_snapshot FROM chapters WHERE id = ?",
        (chapter_id,),
    )
    snap_row = await snap_cur.fetchone()
    existing_snapshot = snap_row["prompt_config_snapshot"] if snap_row else None
    merged_snapshot = _extend_snapshot_with_refiner(existing_snapshot, provider)
    upd = await conn.execute(
        "UPDATE chapters SET refinement_status = 'done', "
        "refined_text = ?, refined_at = datetime('now'), "
        "refined_by_provider_id = ?, "
        "refinement_error = NULL, "
        "prompt_config_snapshot = ? "
        "WHERE id = ? AND refinement_status = 'in_progress'",
        (refined, provider.id, merged_snapshot, chapter_id),
    )
    if (upd.rowcount or 0) == 0:
        # Lost the claim (concurrent state change): discard everything.
        await conn.rollback()
        return

    # CAT Phase 4 refiner merge, inside the claim-guarded commit transaction.
    # This commit flips the displayed variant to 'refined', so the store must
    # mirror the REFINED body from here on: human rows keep their target_text
    # verbatim (the refiner saw the full draft for context; its rewrites of
    # protected rows are discarded, machine_text records its suggestion) and
    # machine rows take the refined paragraphs with origin 'llm_refined'.
    # refined_text is then materialized from the merged set.
    final_refined = refined
    src_paras = chapter_source_paragraphs(r["original_text"] or "")
    if src_paras:
        # tm_exact prefill survives refinement: without re-passing it here,
        # a refiner pass would replace a confirmed cross-chapter rendering
        # on a machine row with the refiner's own wording, defeating the
        # consistency point of prefill. Human rows are unaffected either way.
        merged = await segments_svc.apply_machine_translation(
            conn,
            novel_id=novel_id,
            chapter_id=chapter_id,
            new_paragraphs=split_target_paragraphs(refined),
            kind="llm_refined",
            src_paras=src_paras,
            prefill=await segments_svc.prefill_confirmed_exact(
                conn, novel_id, chapter_id, src_paras
            ),
        )
        final_refined = segments_svc.join_paragraphs(merged)
        if final_refined != refined:
            await conn.execute(
                "UPDATE chapters SET refined_text = ? WHERE id = ?",
                (final_refined, chapter_id),
            )

    # Refinement-stage drift observations are REPLACED, not appended (Phase 1
    # leftover, on record): a prior refinement's drift row describes a
    # refined_text that no longer exists once a clean re-refinement lands.
    # Translation-stage drift rows are left untouched.
    await conn.execute(
        "DELETE FROM chapter_observations "
        "WHERE chapter_id = ? AND kind = 'paragraph_count_drift' "
        "  AND excerpt LIKE 'Paragraph count drift (refinement)%'",
        (chapter_id,),
    )
    # 1:1 drift signal (CAT Phase 1), mirror of the translate path: the
    # fixups and merge above run AFTER refine_chapter's count validation, so
    # the committed refined body can still diverge from the draft's
    # paragraph count. Observation only, no retry; honors the per-novel
    # observer mute. Computed on the FINAL merged refined body.
    drift_observation: NormalizedObservation | None = None
    refined_count = len(split_target_paragraphs(final_refined))
    if refined_count != expected_paragraphs:
        logger.info(
            "refine ch %d: paragraph count drift after fixups "
            "(%d committed, %d expected)",
            r["chapter_num"], refined_count, expected_paragraphs,
        )
        drift_observation = implicit_observation_paragraph_count_drift(
            got=refined_count, expected=expected_paragraphs,
            stage="refinement",
        )
        cur = await conn.execute(
            "SELECT disabled_observers FROM novels WHERE id = ?", (novel_id,),
        )
        mute_row = await cur.fetchone()
        muted = parse_disabled_observers(
            mute_row["disabled_observers"] if mute_row else None
        )
        if drift_observation.kind in muted:
            drift_observation = None
    if drift_observation is not None:
        await conn.execute(
            "INSERT INTO chapter_observations "
            "(chapter_id, kind, severity, paragraph_index, excerpt) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                chapter_id, drift_observation.kind, drift_observation.severity,
                drift_observation.paragraph_index, drift_observation.excerpt,
            ),
        )
    await conn.commit()


async def _run_refine(novel_id: int, chapter_id: int) -> None:
    """One refine task. Shares the process-global translator lock so refine
    and translate are strictly serial — burning the provider's
    subscription/token budget in parallel is the same anti-goal as for
    translation."""
    async with _translator_lock:
        try:
            async with open_conn() as conn:
                await _refine_chapter_in_db(conn, novel_id, chapter_id)
        except Exception:
            logger.exception("queue refine ch_id=%d crashed", chapter_id)
            try:
                async with open_conn() as recovery:
                    # Rescue both non-terminal states. An exception raised
                    # BEFORE the atomic claim (the SELECT, or
                    # _resolve_refinement_provider) leaves the row at 'pending';
                    # one raised AFTER the claim leaves it at 'in_progress'.
                    # Both must become 'error' so the row stops re-spawning on
                    # every drain_on_startup and surfaces for /retry-refinement.
                    # Terminal states ('done'/'none'/'error') are left untouched.
                    await recovery.execute(
                        "UPDATE chapters SET "
                        "refinement_status = CASE WHEN refinement_status "
                        "    IN ('pending', 'in_progress') "
                        "    THEN 'error' ELSE refinement_status END, "
                        "refinement_error = CASE WHEN refinement_status "
                        "    IN ('pending', 'in_progress') "
                        "    THEN COALESCE(refinement_error, 'refiner worker crashed') "
                        "    ELSE refinement_error END "
                        "WHERE id = ?",
                        (chapter_id,),
                    )
                    await recovery.commit()
            except Exception:
                logger.exception(
                    "refine recovery cleanup also failed for ch_id=%d",
                    chapter_id,
                )


def spawn_refine_worker(novel_id: int, chapter_id: int) -> None:
    """Spawn a refine task. Caller has already set refinement_status='pending'
    in a prior commit; this just queues the worker behind the translator
    lock."""
    _spawn(_run_refine(novel_id, chapter_id))


async def _clear_translate_queue(
    conn: aiosqlite.Connection, chapter_id: int
) -> None:
    # Audit 3.2: clear queue_priority alongside translate_queued so a stale
    # priority never leaks forward after a chapter is dequeued by any path.
    await conn.execute(
        "UPDATE chapters SET translate_queued = 0, queue_priority = 0 WHERE id = ?",
        (chapter_id,),
    )
    await conn.commit()
