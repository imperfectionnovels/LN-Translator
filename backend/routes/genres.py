"""Read-only genre registry endpoint. The UI calls this to populate the
per-novel genre dropdown. New genres are added by editing backend/genres.py
and dropping the matching overlay + examples files under backend/prompts/.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.config import DEFAULT_GENRE
from backend.genres import GENRES

router = APIRouter()


@router.get("")
async def list_genres() -> dict:
    return {
        "default": DEFAULT_GENRE,
        "genres": [
            {"key": g.key, "name": g.name, "description": g.description}
            for g in GENRES.values()
        ],
    }
