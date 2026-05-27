"""Global glossary — cross-novel terms applied at prompt-build time.

A user can register a term once and have every novel's translator see it
without re-entering it per novel. Globals are inherently locked (i.e.
the translator must respect the rendering); a per-novel entry with the
same `term_zh` (or a script-folded variant) takes precedence so a novel
can override a global rendering when its context calls for it.

The compose helper `list_for_novel_with_globals` is the single integration
point: it unions per-novel rows with globals, drops globals shadowed by
a per-novel row, and stamps each result with the right `scope` so the
prompt-glossary block can label entries.
"""

from __future__ import annotations

import logging

import aiosqlite

from backend.models import GlobalGlossaryEntry, GlossaryEntry
from backend.services.glossary import list_for_novel
from backend.services.glossary_filters import canonical_zh, split_aliases

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


_SELECT_COLS = (
    "id, term_zh, term_en, category, notes, usage_note, created_at, updated_at"
)


def _row_to_global(r: aiosqlite.Row) -> GlobalGlossaryEntry:
    return GlobalGlossaryEntry(
        id=r["id"],
        term_zh=r["term_zh"],
        term_en=r["term_en"],
        category=r["category"],
        notes=r["notes"],
        usage_note=r["usage_note"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


async def list_all(conn: aiosqlite.Connection) -> list[GlobalGlossaryEntry]:
    cur = await conn.execute(
        f"SELECT {_SELECT_COLS} FROM global_glossary_entries "
        "ORDER BY category, term_zh"
    )
    return [_row_to_global(r) for r in await cur.fetchall()]


async def get_one(
    conn: aiosqlite.Connection, entry_id: int
) -> GlobalGlossaryEntry | None:
    cur = await conn.execute(
        f"SELECT {_SELECT_COLS} FROM global_glossary_entries WHERE id = ?",
        (entry_id,),
    )
    r = await cur.fetchone()
    return _row_to_global(r) if r is not None else None


async def get_by_term_zh(
    conn: aiosqlite.Connection, term_zh: str
) -> GlobalGlossaryEntry | None:
    cur = await conn.execute(
        f"SELECT {_SELECT_COLS} FROM global_glossary_entries WHERE term_zh = ?",
        (term_zh,),
    )
    r = await cur.fetchone()
    return _row_to_global(r) if r is not None else None


class GlobalGlossaryConflict(Exception):
    """Raised when a create / promote-to-global hits an existing entry on
    term_zh. Carries the conflicting entry so the route can return a 409
    body with enough context for the UI to offer a merge."""

    def __init__(self, existing: GlobalGlossaryEntry) -> None:
        super().__init__(f"global glossary already has term {existing.term_zh!r}")
        self.existing = existing


async def create_entry(
    conn: aiosqlite.Connection,
    term_zh: str,
    term_en: str,
    category: str,
    notes: str | None = None,
    usage_note: str | None = None,
) -> GlobalGlossaryEntry:
    zh = term_zh.strip()
    en = term_en.strip()
    if not zh or not en:
        raise ValueError("term_zh and term_en must not be empty")
    existing = await get_by_term_zh(conn, zh)
    if existing is not None:
        raise GlobalGlossaryConflict(existing)
    cur = await conn.execute(
        "INSERT INTO global_glossary_entries "
        "(term_zh, term_en, category, notes, usage_note) "
        "VALUES (?, ?, ?, ?, ?)",
        (zh, en, category, notes, usage_note),
    )
    await conn.commit()
    result = await get_one(conn, cur.lastrowid)
    assert result is not None
    return result


async def update_entry(
    conn: aiosqlite.Connection,
    entry_id: int,
    term_en: str | None = None,
    category: str | None = None,
    notes: str | None = None,
    usage_note: str | None = None,
) -> GlobalGlossaryEntry | None:
    sets: list[str] = []
    params: list[object] = []
    if term_en is not None:
        stripped = term_en.strip()
        if not stripped:
            raise ValueError("term_en must not be empty")
        sets.append("term_en = ?")
        params.append(stripped)
    if category is not None:
        sets.append("category = ?")
        params.append(category)
    if notes is not None:
        sets.append("notes = ?")
        params.append(notes)
    if usage_note is not None:
        sets.append("usage_note = ?")
        params.append(usage_note)
    if not sets:
        return await get_one(conn, entry_id)
    sets.append("updated_at = datetime('now')")
    params.append(entry_id)
    await conn.execute(
        f"UPDATE global_glossary_entries SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    await conn.commit()
    return await get_one(conn, entry_id)


async def delete_entry(conn: aiosqlite.Connection, entry_id: int) -> bool:
    cur = await conn.execute(
        "DELETE FROM global_glossary_entries WHERE id = ?", (entry_id,)
    )
    await conn.commit()
    return cur.rowcount > 0


async def usage_per_novel(
    conn: aiosqlite.Connection, term_zh: str
) -> list[dict]:
    """Per-novel chapter counts where the given Chinese term appears in
    `original_text`. Sorted novel id ASC. Used by the scope-warning dialog
    when editing a global entry — "this affects N novels / M chapters."
    """
    term = (term_zh or "").strip()
    if not term:
        return []
    cur = await conn.execute(
        "SELECT n.id AS novel_id, n.title AS novel_title, "
        "       COUNT(c.id) AS chapter_count "
        "FROM novels n "
        "JOIN chapters c ON c.novel_id = n.id "
        "WHERE INSTR(c.original_text, ?) > 0 "
        "GROUP BY n.id, n.title "
        "HAVING chapter_count > 0 "
        "ORDER BY n.id",
        (term,),
    )
    rows = await cur.fetchall()
    return [
        {
            "novel_id": r["novel_id"],
            "novel_title": r["novel_title"],
            "chapter_count": r["chapter_count"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Composition with per-novel glossary
# ---------------------------------------------------------------------------


def _global_to_glossary_entry(g: GlobalGlossaryEntry) -> GlossaryEntry:
    """Adapt a GlobalGlossaryEntry into a GlossaryEntry-shaped row for the
    prompt-build pipeline. Globals always come through as locked + scope=global
    so the existing locked-vs-auto branches in `dedupe_against_locked` /
    `filter_glossary_for_chapter` keep behaving consistently."""
    return GlossaryEntry(
        id=g.id,
        novel_id=None,
        term_zh=g.term_zh,
        term_en=g.term_en,
        category=g.category,
        notes=g.notes,
        usage_note=g.usage_note,
        auto_detected=False,
        locked=True,
        scope="global",
    )


async def list_for_novel_with_globals(
    conn: aiosqlite.Connection, novel_id: int
) -> list[GlossaryEntry]:
    """Union of per-novel + global glossary for a single novel's prompt build.

    Precedence: per-novel locked > per-novel auto > global. A global entry
    whose term_zh (or a script-folded variant of any of the per-novel
    entries' aliases) matches a per-novel row is dropped — the per-novel
    rendering wins. The remaining globals come through with scope='global'
    so the prompt-glossary block can label them.
    """
    per_novel = await list_for_novel(conn, novel_id)
    # Tag every per-novel row so the renderer can distinguish locked from
    # auto without re-checking booleans.
    for g in per_novel:
        g.scope = "novel"

    globals_raw = await list_all(conn)
    if not globals_raw:
        return per_novel

    # Build the set of canonical_zh strings covered by per-novel rows,
    # including every alias (so locked "筑基 / 築基" shadows global 筑基).
    covered_canon: set[str] = set()
    for g in per_novel:
        for zh, _ in split_aliases(g.term_zh or "", g.term_en or ""):
            cz = canonical_zh(zh)
            if cz:
                covered_canon.add(cz)

    filtered_globals: list[GlossaryEntry] = []
    for gg in globals_raw:
        # A global entry can itself carry alias slashes; if ANY of its alias
        # canon forms is already covered by a per-novel row, drop the whole
        # global (the per-novel side owns the term).
        gg_canons = {
            canonical_zh(zh)
            for zh, _ in split_aliases(gg.term_zh or "", gg.term_en or "")
            if zh
        }
        if gg_canons & covered_canon:
            continue
        filtered_globals.append(_global_to_glossary_entry(gg))

    return per_novel + filtered_globals
