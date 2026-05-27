"""HTTP routes for the OPUS-MT free-tier model lifecycle.

Mounted at top-level ``/api/opus-mt/`` (not under ``/api/providers/{id}/``)
to keep model-management surface separate from per-provider CRUD. This also
sidesteps the dynamic-segment shadowing risk: ``/api/providers/{provider_id}``
in ``routes/providers.py`` would shadow any sibling subpath unless
``/api/providers/opus-mt/...`` were registered first, and even with correct
ordering it conflates two different concepts.

Endpoints:
    GET    /api/opus-mt/pairs                  list supported pairs + install state
    POST   /api/opus-mt/pairs/{pair}/download  kick off a streaming download
    GET    /api/opus-mt/pairs/{pair}/status    SSE progress stream (in-flight job)
    DELETE /api/opus-mt/pairs/{pair}           remove the installed model

Concurrency model:
    * One in-flight download per pair (enforced by an async lock inside
      ``opus_mt_models.download_pair``).
    * Progress events flow into a per-pair asyncio.Queue. SSE clients
      subscribe by reading from that queue.
    * If a client subscribes before a download starts, the queue is empty
      and it waits; if it subscribes after a download completes, the route
      returns immediately with a synthetic ``done`` event derived from the
      pair's current install state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.services import opus_mt_models

logger = logging.getLogger(__name__)
router = APIRouter()


# Per-pair queue holding the most recent download's progress events. A new
# download replaces the queue; an SSE subscriber reads from it until the
# 'done' or 'error' phase shows up.
_progress_queues: dict[str, asyncio.Queue[opus_mt_models.ProgressEvent]] = {}
# Per-pair sentinel describing the last-completed event so a late subscriber
# sees the final state instead of hanging.
_last_event: dict[str, opus_mt_models.ProgressEvent] = {}


def _serialize_event(ev: opus_mt_models.ProgressEvent) -> str:
    """One SSE ``data: ...\\n\\n`` line."""
    payload = json.dumps(asdict(ev))
    return f"data: {payload}\n\n"


@router.get("/pairs")
async def list_pairs() -> list[dict]:
    """Snapshot of supported language pairs + their on-disk state."""
    out = []
    for pair, spec in opus_mt_models.SUPPORTED_PAIRS.items():
        installed = opus_mt_models.is_installed(pair)
        out.append({
            "pair": pair,
            "source_language": spec.source_lang,
            "target_language": spec.target_lang,
            "display": spec.display,
            "size_mb_expected": spec.size_mb,
            "size_mb_installed": opus_mt_models.installed_size_mb(pair) if installed else 0,
            "installed": installed,
            "download_url": spec.url,
        })
    return out


@router.post("/pairs/{pair}/download")
async def start_download(pair: str) -> dict:
    """Kick off a background download for ``pair``. Returns immediately; the
    client polls the SSE status endpoint for progress.

    Idempotent: if the pair is already installed, returns ``{"status": "done"}``
    without starting a new download. If a download is already in flight,
    returns ``{"status": "in_progress"}`` and the existing task keeps going."""
    if pair not in opus_mt_models.SUPPORTED_PAIRS:
        raise HTTPException(404, f"unknown pair {pair!r}")

    if opus_mt_models.is_installed(pair):
        return {"status": "done", "pair": pair}

    # If a queue already exists and is non-empty, a job is presumed in flight.
    # The download_pair coroutine itself holds the asyncio.Lock so concurrent
    # starts collapse; we just need to avoid spawning a redundant task.
    if pair in _progress_queues and not _progress_queues[pair].empty():
        return {"status": "in_progress", "pair": pair}

    queue: asyncio.Queue[opus_mt_models.ProgressEvent] = asyncio.Queue()
    _progress_queues[pair] = queue
    _last_event.pop(pair, None)

    async def _drive_download() -> None:
        try:
            async for ev in opus_mt_models.download_pair(pair):
                _last_event[pair] = ev
                await queue.put(ev)
        except Exception as exc:
            logger.exception("opus-mt download failed for %s", pair)
            err = opus_mt_models.ProgressEvent(
                pair=pair, phase="error", bytes_done=0, bytes_total=0,
                detail=str(exc),
            )
            _last_event[pair] = err
            await queue.put(err)

    asyncio.create_task(_drive_download(), name=f"opus_mt_download:{pair}")
    return {"status": "started", "pair": pair}


@router.get("/pairs/{pair}/status")
async def stream_status(pair: str) -> StreamingResponse:
    """SSE stream of progress events for the in-flight or just-finished download.

    Closes after the first terminal event (``done`` or ``error``). If no
    download has ever run and the pair is already installed, emits one
    ``done`` event and closes immediately."""
    if pair not in opus_mt_models.SUPPORTED_PAIRS:
        raise HTTPException(404, f"unknown pair {pair!r}")

    async def _gen() -> AsyncIterator[str]:
        # Late-subscriber path: no queue, no last event, but pair is installed.
        if pair not in _progress_queues:
            if opus_mt_models.is_installed(pair):
                yield _serialize_event(opus_mt_models.ProgressEvent(
                    pair=pair, phase="done", bytes_done=0, bytes_total=0,
                ))
            else:
                yield _serialize_event(opus_mt_models.ProgressEvent(
                    pair=pair, phase="error",
                    bytes_done=0, bytes_total=0,
                    detail="no download in progress",
                ))
            return

        queue = _progress_queues[pair]
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=60.0)
            except asyncio.TimeoutError:
                # Heartbeat — SSE clients reconnect on EOF, this keeps the
                # pipe warm on slow downloads.
                yield ": heartbeat\n\n"
                continue
            yield _serialize_event(ev)
            if ev.phase in ("done", "error"):
                return

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/pairs/{pair}")
async def delete_pair(pair: str) -> dict:
    """Remove the installed model for ``pair``. Returns whether it existed."""
    if pair not in opus_mt_models.SUPPORTED_PAIRS:
        raise HTTPException(404, f"unknown pair {pair!r}")
    existed = opus_mt_models.remove_pair(pair)
    _progress_queues.pop(pair, None)
    _last_event.pop(pair, None)
    return {"pair": pair, "removed": existed}
