"""Per-novel genre tags.

`novels.genre` remains the PRIMARY genre — the one that drives the prompt
overlay via `backend.genres.resolve_genre` → `build_system_instruction`.
The `novel_genres` table holds SECONDARY tags only. UI shows primary
distinctly from secondary; "make primary" swaps the chosen secondary
with the current primary in a single transaction so we never observe
a row with two primaries or none.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite
from fastapi import HTTPException

from backend.genres import GENRES


@dataclass(frozen=True)
class NovelGenres:
    primary: str | None
    secondary: list[str]

    @property
    def all_keys(self) -> list[str]:
        """Primary first, then secondary in insertion order. Useful for UI
        loops that want a single ordered list."""
        return ([self.primary] if self.primary else []) + self.secondary


def _validate_genre_key(genre_key: str) -> None:
    """400 if the key isn't in the GENRES registry. Caller catches the
    HTTPException or surfaces it directly via FastAPI."""
    if genre_key not in GENRES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown genre {genre_key!r}; see backend/genres.py for valid keys",
        )


async def _novel_exists(conn: aiosqlite.Connection, novel_id: int) -> bool:
    cur = await conn.execute("SELECT 1 FROM novels WHERE id = ?", (novel_id,))
    return await cur.fetchone() is not None


async def list_novel_genres(
    conn: aiosqlite.Connection, novel_id: int
) -> NovelGenres:
    """Return the primary genre (from novels.genre) + every secondary tag
    (from novel_genres) in insertion order."""
    if not await _novel_exists(conn, novel_id):
        raise HTTPException(status_code=404, detail="novel not found")
    cur = await conn.execute(
        "SELECT genre FROM novels WHERE id = ?", (novel_id,),
    )
    row = await cur.fetchone()
    primary = row["genre"]
    cur = await conn.execute(
        "SELECT genre_key FROM novel_genres "
        "WHERE novel_id = ? ORDER BY id",
        (novel_id,),
    )
    secondary = [r["genre_key"] for r in await cur.fetchall()]
    return NovelGenres(primary=primary, secondary=secondary)


async def add_secondary_genre(
    conn: aiosqlite.Connection, novel_id: int, genre_key: str
) -> NovelGenres:
    """INSERT OR IGNORE — adding the same tag twice is a no-op. Rejects if
    the key matches the current primary (already represented)."""
    _validate_genre_key(genre_key)
    if not await _novel_exists(conn, novel_id):
        raise HTTPException(status_code=404, detail="novel not found")
    cur = await conn.execute(
        "SELECT genre FROM novels WHERE id = ?", (novel_id,),
    )
    row = await cur.fetchone()
    if row["genre"] == genre_key:
        raise HTTPException(
            status_code=409,
            detail=f"{genre_key!r} is already this novel's primary genre",
        )
    await conn.execute(
        "INSERT OR IGNORE INTO novel_genres (novel_id, genre_key) "
        "VALUES (?, ?)",
        (novel_id, genre_key),
    )
    await conn.commit()
    return await list_novel_genres(conn, novel_id)


async def remove_secondary_genre(
    conn: aiosqlite.Connection, novel_id: int, genre_key: str
) -> NovelGenres:
    """Remove a secondary tag. Rejects with 409 if the caller tries to
    remove the current primary — the primary lives on novels.genre, not
    here, and must be replaced via set_primary_genre."""
    if not await _novel_exists(conn, novel_id):
        raise HTTPException(status_code=404, detail="novel not found")
    cur = await conn.execute(
        "SELECT genre FROM novels WHERE id = ?", (novel_id,),
    )
    row = await cur.fetchone()
    if row["genre"] == genre_key:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{genre_key!r} is the primary genre; promote another tag "
                "to primary before removing this one"
            ),
        )
    await conn.execute(
        "DELETE FROM novel_genres WHERE novel_id = ? AND genre_key = ?",
        (novel_id, genre_key),
    )
    await conn.commit()
    return await list_novel_genres(conn, novel_id)


async def set_primary_genre(
    conn: aiosqlite.Connection, novel_id: int, genre_key: str
) -> NovelGenres:
    """Promote a secondary tag (or any registry-valid key) to primary.

    Transactional swap:
      1. read current primary (OLD)
      2. if `genre_key` is in novel_genres → DELETE that row
         (it's becoming the primary; can't also be secondary)
      3. UPDATE novels SET genre = genre_key
      4. if OLD existed and differs from new primary → INSERT OR IGNORE
         OLD as a secondary so it isn't lost

    All four steps inside one transaction so we never observe a no-primary
    or two-primary state. If `genre_key` equals the existing primary, no-op.
    """
    _validate_genre_key(genre_key)
    if not await _novel_exists(conn, novel_id):
        raise HTTPException(status_code=404, detail="novel not found")
    try:
        await conn.execute("BEGIN")
        cur = await conn.execute(
            "SELECT genre FROM novels WHERE id = ?", (novel_id,),
        )
        row = await cur.fetchone()
        old_primary = row["genre"]
        if old_primary == genre_key:
            # No-op; commit the empty transaction so the connection state
            # is clean and return current.
            await conn.commit()
            return await list_novel_genres(conn, novel_id)
        await conn.execute(
            "DELETE FROM novel_genres WHERE novel_id = ? AND genre_key = ?",
            (novel_id, genre_key),
        )
        await conn.execute(
            "UPDATE novels SET genre = ? WHERE id = ?",
            (genre_key, novel_id),
        )
        if old_primary:
            await conn.execute(
                "INSERT OR IGNORE INTO novel_genres (novel_id, genre_key) "
                "VALUES (?, ?)",
                (novel_id, old_primary),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return await list_novel_genres(conn, novel_id)
