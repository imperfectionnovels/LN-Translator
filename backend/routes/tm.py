"""Translation memory routes (Initiative 5).

One endpoint:
  * GET /api/novels/{id}/tm/concordance?q=...
      Substring search across the novel's source ↔ target index.
      Returns paragraph-context hits with chapter+paragraph links the
      reader can jump to.

CAT Phase 5: the search is provenance-aware. chapter_segments hits come
first (they carry `status` so the dialogs can chip confirmed/edited rows);
tm_segments is the legacy fallback for chapters with no segment rows (their
hits carry status None). The merge lives in
services/segments.py::concordance_search (single chapter_segments owner).
`status` is an additive response field; the pre-Phase-5 shape is unchanged.

The old GET /api/novels/{id}/tm/inconsistencies endpoint was removed
2026-07-30 (no UI caller; the drift signal ships as tm_inconsistency
observations).
"""

from __future__ import annotations

from typing import Literal

import aiosqlite
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.db import get_conn
from backend.services import segments as segments_svc

router = APIRouter()


class ConcordanceHit(BaseModel):
    chapter_id: int
    chapter_num: int
    chapter_title_en: str | None
    paragraph_index: int
    source_text: str
    target_text: str
    matched_side: Literal["source", "target"]
    # Segment provenance ('machine' | 'edited' | 'confirmed'); None for a
    # legacy tm_segments hit. Additive (CAT Phase 5).
    status: str | None = None


@router.get("/novels/{novel_id}/tm/concordance")
async def concordance(
    novel_id: int,
    q: str = Query(min_length=2, max_length=2000),
    side: Literal["both", "source", "target"] = "both",
    conn: aiosqlite.Connection = Depends(get_conn),
) -> list[ConcordanceHit]:
    sides: tuple[str, ...]
    if side == "both":
        sides = ("source", "target")
    elif side == "source":
        sides = ("source",)
    else:
        sides = ("target",)
    hits = await segments_svc.concordance_search(conn, novel_id, q, sides)
    return [ConcordanceHit(**h) for h in hits]
