"""Cache stats endpoint (Section 6.7).

Exposes in-process LLM-cache hit/miss counters so the settings UI can
show the user how often translations are reused. Counters reset on
process restart; the user shouldn't trust them as a long-term metric,
only as a "what's the cache doing right now" sanity check.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.services import llm_cache

router = APIRouter()


@router.get("/stats")
async def cache_stats() -> dict:
    return llm_cache.get_stats()
