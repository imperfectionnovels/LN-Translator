"""In-app quality + consistency cockpit service (read-only).

Wraps the two CLI scorers (`scripts/quality_report.py`,
`scripts/consistency_eval.py`) so the app can render their output instead of a
human running them in a terminal. The scorers stay the single source of truth;
this layer only adds (a) an event-loop-friendly threadpool offload and (b) a
cheap in-process cache so a full-novel scan does not re-run on every paint.

Why a threadpool: a full-novel consistency scan over ~1k chapters is
CPU-heavy (per-term TCR + per-paragraph hashing) and the scorers are
synchronous. Running them inline would block the event loop for seconds and
stall queue-status polls. The async DB loaders run on the loop; the pure
build runs in `run_in_threadpool`.

Why a pull-based version token (no invalidation callbacks): the cache stores
`(token, result)` and the token is a hash of cheap per-novel aggregates that
change on any (re)translate or glossary edit. A cached entry is served only
when its token still matches a freshly computed one, so retranslating a
chapter or editing the glossary transparently busts the cache without wiring
hooks into the queue / glossary write paths. The token query is a couple of
indexed aggregates (sub-millisecond) guarding a multi-second scan.

Single-process only (the app forbids WEB_CONCURRENCY>1, so an in-process cache
is correct): a second worker would hold its own cache, but there is never one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import aiosqlite
from fastapi.concurrency import run_in_threadpool

from backend.db import open_conn
from backend.scripts.consistency_eval import _build_report as _consistency_report
from backend.scripts.consistency_eval import _load as _consistency_load
from backend.scripts.quality_metrics import score_text
from backend.scripts.quality_report import (
    _build_scorecard,
    _load_range,
)
from backend.services import glossary as glossary_svc

# key -> (version_token, result). Keys: ("scorecard", novel_id, lo, hi),
# ("consistency", novel_id).
_cache: dict[tuple, tuple[str, object]] = {}
_locks: dict[tuple, asyncio.Lock] = {}


async def _version_token(novel_id: int) -> str:
    """Cheap content stamp: changes on any (re)translate, refine, glossary
    edit, or inline paragraph edit for this novel. Guards the expensive scans
    below.

    An inline edit (routes/chapters.py::edit_paragraph) rewrites
    translated_text/refined_text WITHOUT bumping translated_at/refined_at, so a
    chapter-timestamp stamp alone would serve a stale scorecard right after the
    user fixed a chapter. Every such edit inserts exactly one style_edits row,
    so MAX(style_edits.id) + COUNT advance on each one and bust the cache. The
    aggregate rides the idx_style_edits_novel index, so it stays sub-millisecond."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS done, MAX(translated_at) AS lt, "
            "MAX(refined_at) AS lr FROM chapters "
            "WHERE novel_id = ? AND status = 'done'",
            (novel_id,),
        )
        c = await cur.fetchone()
        cur = await conn.execute(
            "SELECT MAX(updated_at) AS lg, COUNT(*) AS n "
            "FROM glossary_entries WHERE novel_id = ?",
            (novel_id,),
        )
        g = await cur.fetchone()
        cur = await conn.execute(
            "SELECT MAX(id) AS le, COUNT(*) AS ne "
            "FROM style_edits WHERE novel_id = ?",
            (novel_id,),
        )
        e = await cur.fetchone()
    raw = f"{c['done']}|{c['lt']}|{c['lr']}|{g['lg']}|{g['n']}|{e['le']}|{e['ne']}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


async def _cached(key: tuple, token: str, producer):
    """Serve `key` from cache when its stored token matches `token`, else run
    `producer()` once (deduped across concurrent callers) and store it."""
    hit = _cache.get(key)
    if hit is not None and hit[0] == token:
        return hit[1]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)  # re-check: another caller may have filled it
        if hit is not None and hit[0] == token:
            return hit[1]
        result = await producer()
        _cache[key] = (token, result)
        return result


async def scorecard(novel_id: int, lo: int, hi: int) -> dict | None:
    """Per-category quality scorecard for a chapter range (None when empty)."""
    token = await _version_token(novel_id)

    async def _produce():
        data = await _load_range(novel_id, lo, hi)
        if not data["chapters"]:
            return None
        # Reuse the cached consistency report rather than re-running the scan
        # privately: the quality page loads scorecard + consistency together,
        # so a private _consistency_load + _consistency_report here ran the
        # full-novel TCR scan (and a second glossary load) twice per paint. The
        # load is gated behind the empty-range check above so a narrow empty
        # sub-range never triggers a whole-novel scan it would just discard.
        cons = await consistency(novel_id)
        return await run_in_threadpool(
            _build_scorecard, novel_id, lo, hi, data, cons
        )

    return await _cached(("scorecard", novel_id, lo, hi), token, _produce)


async def consistency(novel_id: int) -> dict:
    """Full consistency report (TCR overall + per-category + worst terms)."""
    token = await _version_token(novel_id)

    async def _produce():
        data = await _consistency_load(novel_id)
        return await run_in_threadpool(_consistency_report, novel_id, data)

    return await _cached(("consistency", novel_id), token, _produce)


async def chapter_quality(
    conn: aiosqlite.Connection, novel_id: int, chapter_num: int
) -> dict | None:
    """Single-chapter score for the reader's quality badge.

    Cheap (one chapter), so it is computed on the request connection with no
    caching: an inline paragraph edit (which does not bump translated_at)
    must still reflect immediately. Scores COALESCE(refined_text,
    translated_text) to match exactly what the reader displays.
    """
    cur = await conn.execute("PRAGMA table_info(chapters)")
    cols = {r["name"] for r in await cur.fetchall()}
    fixup_sel = "fixup_audit" if "fixup_audit" in cols else "NULL AS fixup_audit"
    cur = await conn.execute(
        f"SELECT chapter_num, title_en, status, original_text, "
        f"COALESCE(refined_text, translated_text) AS body, {fixup_sel} "
        "FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    row = await cur.fetchone()
    if row is None:
        return None

    base = {
        "novel_id": novel_id,
        "chapter_num": chapter_num,
        "title_en": row["title_en"],
        "status": row["status"],
    }
    if row["status"] != "done" or not row["body"]:
        return {**base, "scored": False}

    glossary = await glossary_svc.list_for_novel(conn, novel_id)
    score = await run_in_threadpool(
        score_text, row["body"], row["original_text"] or "", glossary
    )
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM chapter_observations o "
        "JOIN chapters c ON c.id = o.chapter_id "
        "WHERE c.novel_id = ? AND c.chapter_num = ? AND o.dismissed_at IS NULL",
        (novel_id, chapter_num),
    )
    observer_hits = (await cur.fetchone())["n"]

    fixup_rules: dict = {}
    fixup_total = 0
    if row["fixup_audit"]:
        try:
            fa = json.loads(row["fixup_audit"])
            if isinstance(fa, dict):
                fixup_rules = fa.get("rules", {}) or {}
                fixup_total = fa.get("total", 0)
        except (TypeError, ValueError):
            pass

    cats = score["categories"]
    viol = sum(c["violations"] for c in cats)
    opp = sum(c["opportunities"] for c in cats)
    worst_cat = max(cats, key=lambda c: c["violations"], default=None)
    return {
        **base,
        "scored": True,
        "violations": viol,
        "opportunities": opp,
        "rate": viol / (opp or 1),
        "worst_category": worst_cat["name"] if worst_cat and viol else None,
        "observer_hits": observer_hits,
        "fixup_total": fixup_total,
        "fixup_rules": fixup_rules,
        "categories": cats,
    }
