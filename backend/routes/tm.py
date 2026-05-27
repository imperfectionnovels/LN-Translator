"""Translation memory routes (Initiative 5).

Two endpoints:
  * GET /api/novels/{id}/tm/concordance?q=...
      Substring search across the novel's source ↔ target index.
      Returns paragraph-context hits with chapter+paragraph links the
      reader can jump to.
  * GET /api/novels/{id}/tm/inconsistencies
      Same-source-paragraph-multiple-target-renderings detection.
      Surfaces drift the user can clean up via Initiative 4's
      find/replace engine.
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


class ConcordanceChapterMeta(BaseModel):
    chapter_id: int
    chapter_num: int
    title_en: str | None


class InconsistencyRendering(BaseModel):
    target_text: str
    chapters: list[ConcordanceChapterMeta]


class InconsistencyGroup(BaseModel):
    source_text: str
    source_hash: str
    renderings: list[InconsistencyRendering]
    total_occurrences: int


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


@router.get("/novels/{novel_id}/tm/inconsistencies")
async def inconsistencies(
    novel_id: int,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> list[InconsistencyGroup]:
    groups = await tm_svc.find_inconsistencies(conn, novel_id)
    return [
        InconsistencyGroup(
            source_text=g.source_text,
            source_hash=g.source_hash,
            renderings=[
                InconsistencyRendering(
                    target_text=r["target_text"],
                    chapters=[
                        ConcordanceChapterMeta(**c)
                        for c in r["chapters"]
                    ],
                )
                for r in g.renderings
            ],
            total_occurrences=g.total_occurrences,
        )
        for g in groups
    ]
