"""Content-addressed on-disk cache for translator LLM responses.

The cache key derives from every input the LLM sees — backend identity (name
plus upstream model id), the system instruction, and the final user prompt
(which itself includes the filtered glossary, chapter text, and title). Any
change to any of these naturally invalidates entries — no manual purge needed.

Plain-text-fallback results are deliberately NOT cached: they drop
`new_terms`, so a fallback-cached entry would poison a later proper-mode call
against identical inputs. Only successful structured results are written.

Writes are atomic (tempfile + os.replace). Reads tolerate any IO or schema
error by returning a miss so the LLM call proceeds normally — the cache is
strictly an optimization, never a correctness dependency.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import TypedDict

from backend.config import USER_DATA_ROOT
from backend.models import TranslationResult

logger = logging.getLogger(__name__)


class CacheStageStats(TypedDict):
    hits: int
    misses: int
    hit_rate: float | None


class CacheStats(TypedDict):
    """Fixed shape returned by `get_stats`, consumed by the cache-stats route
    and the dashboard. Naming it makes a dropped/renamed nested key a
    type-check failure rather than a runtime surprise in the UI."""
    translator: CacheStageStats
    refiner: CacheStageStats
    on_disk_bytes: int
    on_disk_files: int

# Age (in seconds) past which a `*.tmp` file in a cache stage dir is assumed
# orphaned by a crashed write and is safe to delete.
_ORPHAN_TMP_MAX_AGE = 24 * 3600

# Bump when the structure of the prompt or response changes in a way that
# should force every existing entry to be regenerated, independent of
# system-instruction edits. The system instruction is already part of the
# key, so most edits self-invalidate; this is the override.
#
# v2 (2026-05-22): single-pass restructure — humanizer deleted, bilingual
# review deleted, delimited envelope unified across backends, prompt
# carries style_note. Every prior cached translation is stale by definition.
PROMPT_TEMPLATE_VERSION = "v2"


def _cache_root() -> Path:
    override = os.getenv("LLM_CACHE_ROOT")
    if override:
        return Path(override)
    # USER_DATA_ROOT resolves to repo/data/ in dev and %APPDATA%/... in
    # frozen mode, so cache state persists across both contexts.
    return USER_DATA_ROOT / "llm_cache"


def _stage_dir(stage: str) -> Path:
    p = _cache_root() / stage
    p.mkdir(parents=True, exist_ok=True)
    return p


def _compute_key(parts: list[str]) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


# In-process hit/miss counters since boot. Bumped by load_translation /
# load_refinement; the value never persists across restarts (that would be
# misleading after a config change). Exposed via /api/cache/stats so the
# settings UI can show "78% cache hit rate (last 100 calls saved ~$X)".
_STATS = {
    "translator_hits": 0,
    "translator_misses": 0,
    "refiner_hits": 0,
    "refiner_misses": 0,
}


def get_stats() -> CacheStats:
    """Snapshot of the in-process cache counters. Read-only; reset_stats()
    is the only mutator besides the load_* functions."""
    total_t = _STATS["translator_hits"] + _STATS["translator_misses"]
    total_r = _STATS["refiner_hits"] + _STATS["refiner_misses"]
    hit_rate_t = (
        _STATS["translator_hits"] / total_t if total_t else None
    )
    hit_rate_r = (
        _STATS["refiner_hits"] / total_r if total_r else None
    )
    # On-disk size of the cache directory — useful signal of how much
    # state the user has accumulated. Glob both stage dirs, ignore I/O
    # errors so a partially-deleted cache doesn't break the endpoint.
    on_disk_bytes = 0
    on_disk_files = 0
    root = _cache_root()
    if root.is_dir():
        for stage_dir in root.iterdir():
            if not stage_dir.is_dir():
                continue
            for entry in stage_dir.iterdir():
                if entry.suffix == ".tmp":
                    continue
                try:
                    on_disk_bytes += entry.stat().st_size
                    on_disk_files += 1
                except OSError:
                    pass
    return {
        "translator": {
            "hits": _STATS["translator_hits"],
            "misses": _STATS["translator_misses"],
            "hit_rate": hit_rate_t,
        },
        "refiner": {
            "hits": _STATS["refiner_hits"],
            "misses": _STATS["refiner_misses"],
            "hit_rate": hit_rate_r,
        },
        "on_disk_bytes": on_disk_bytes,
        "on_disk_files": on_disk_files,
    }


def reset_stats() -> None:
    for k in _STATS:
        _STATS[k] = 0


def translation_key(
    backend_id: str,
    system_instruction: str,
    prompt: str,
) -> str:
    return _compute_key(
        [
            "translator",
            PROMPT_TEMPLATE_VERSION,
            backend_id,
            system_instruction,
            prompt,
        ]
    )


def load_translation(key: str) -> TranslationResult | None:
    path = _stage_dir("translator") / f"{key}.json"
    if not path.is_file():
        _STATS["translator_misses"] += 1
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        result = TranslationResult.model_validate_json(raw)
        _STATS["translator_hits"] += 1
        return result
    except Exception as e:
        logger.warning("translator cache read failed for %s…: %s", key[:12], e)
        # Treat a corrupt cache file as a miss so the caller falls through
        # to a fresh LLM call. Counting it as a miss is honest accounting.
        _STATS["translator_misses"] += 1
        return None


def store_translation(key: str, result: TranslationResult) -> None:
    path = _stage_dir("translator") / f"{key}.json"
    _write_atomic(path, result.model_dump_json())


# ----- Refinement -----
# Refinement is a second pass over a translator draft. It produces only the
# refined English body (no new_terms), so the cache value is a plain string
# stored as text, not JSON. The key includes the refiner's backend identity,
# the editor system instruction, and the draft text — a different draft, a
# different provider, or an edit to the refiner prompt invalidates separately.


def refinement_key(
    backend_id: str,
    system_instruction: str,
    draft_translation: str,
) -> str:
    return _compute_key(
        [
            "refiner",
            PROMPT_TEMPLATE_VERSION,
            backend_id,
            system_instruction,
            draft_translation,
        ]
    )


def load_refinement(key: str) -> str | None:
    path = _stage_dir("refiner") / f"{key}.txt"
    if not path.is_file():
        _STATS["refiner_misses"] += 1
        return None
    try:
        text = path.read_text(encoding="utf-8")
        _STATS["refiner_hits"] += 1
        return text
    except Exception as e:
        logger.warning("refiner cache read failed for %s…: %s", key[:12], e)
        _STATS["refiner_misses"] += 1
        return None


def store_refinement(key: str, refined_text: str) -> None:
    path = _stage_dir("refiner") / f"{key}.txt"
    _write_atomic(path, refined_text)


def _write_atomic(path: Path, content: str) -> None:
    """Write to a tempfile in the same directory then os.replace."""
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=path.stem + ".", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as e:
        logger.warning("cache write failed for %s: %s", path.name, e)


def gc_orphan_tmp_files() -> int:
    """Sweep `*.tmp` files older than _ORPHAN_TMP_MAX_AGE from every cache
    stage dir. Returns the number removed."""
    root = _cache_root()
    if not root.is_dir():
        return 0
    now = time.time()
    removed = 0
    for stage_dir in root.iterdir():
        if not stage_dir.is_dir():
            continue
        for entry in stage_dir.iterdir():
            if entry.suffix != ".tmp":
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age < _ORPHAN_TMP_MAX_AGE:
                continue
            try:
                entry.unlink()
                removed += 1
            except OSError:
                continue
    return removed
