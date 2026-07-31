"""Translation memory routes (Initiative 5).

One endpoint:
  * GET /api/novels/{id}/tm/concordance?q=...
      Substring search across the novel's source ↔ target index.
      Returns paragraph-context hits with chapter+paragraph links the
      reader can jump to.

The old GET /api/novels/{id}/tm/inconsistencies endpoint was removed
2026-07-30 (no UI caller); the same drift signal reaches users via the
tm_inconsistency observations queue.py writes on every translate
(services/tm.py::find_inconsistencies is still the shared core).
"""

from __future__ import annotations

import logging
from typing import Literal

import aiosqlite
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.db import get_conn
from backend.services import tm as tm_svc

logger = logging.getLogger(__name__)
router = APIRouter()


class ConcordanceHit(BaseModel):
    chapter_id: int
    chapter_num: int
    chapter_title_en: str | None
    paragraph_index: int
    source_text: str
    target_text: str
    matched_side: Literal["source", "target"]


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
    hits = await tm_svc.search(conn, novel_id, q, sides)
    return [ConcordanceHit(**h.__dict__) for h in hits]
