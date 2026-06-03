"""Glossary persistence and auto-merge — runtime DB ops + missing-term checks.

Locked=True entries (user-edited) are never overwritten by auto-detected
suggestions. Auto-detected entries can be replaced by later chapters'
suggestions.

This module owns the DB-touching code paths (`list_for_novel`,
`merge_new_terms`, `create_or_overwrite_entry`, etc.) and the
`missing_translator_terms` family that backs the translator's missing-term
observation. Pure helpers live in two sibling modules:

- `glossary_casing.py`: `_GENERIC_RANK_RE`, `is_atomic_case_locked_term`,
  `_normalize_extracted_casing` — the casing rules.
- `glossary_filters.py`: `canonical_zh`, `split_aliases`,
  `dedupe_against_locked`, `filter_glossary_candidates`,
  `filter_glossary_for_chapter`, `detect_candidate_terms` — pure
  filtering / alias logic.

The names above are re-exported from this module so existing callers keep
working without import changes; new code can import directly from the
right submodule.
"""

from __future__ import annotations

import logging
import re

import aiosqlite

from backend.models import GlossaryEntry, NewTerm
from backend.services.glossary_casing import (
    _GENERIC_RANK_RE,
    _normalize_extracted_casing,
    is_atomic_case_locked_term,
    is_half_applied_lowercase_hatch,
)
from backend.services.glossary_filters import (
    _ALIAS_SEP_RE,
    canonical_zh,
    dedupe_against_locked,
    detect_candidate_terms,
    filter_glossary_candidates,
    filter_glossary_for_chapter,
    split_aliases,
)

__all__ = [
    # Re-exported from glossary_casing
    "_GENERIC_RANK_RE",
    "_normalize_extracted_casing",
    "is_atomic_case_locked_term",
    "is_half_applied_lowercase_hatch",
    # Re-exported from glossary_filters
    "canonical_zh",
    "dedupe_against_locked",
    "detect_candidate_terms",
    "filter_glossary_candidates",
    "filter_glossary_for_chapter",
    "split_aliases",
    # Owned by this module
    "english_term_present",
    "headword_for_substitution",
    "missing_translator_terms",
    "list_for_novel",
    "merge_new_terms",
    "LockedEntryConflict",
    "create_or_overwrite_entry",
    "update_entry",
    "delete_entry",
    "find_chapters_using_term",
    "get_one",
]

logger = logging.getLogger(__name__)


async def list_for_novel(
    conn: aiosqlite.Connection, novel_id: int
) -> list[GlossaryEntry]:
    cur = await conn.execute(
        "SELECT id, novel_id, term_zh, term_en, category, notes, usage_note, "
        "auto_detected, locked, updated_at FROM glossary_entries WHERE novel_id = ? "
        "ORDER BY category, term_zh",
        (novel_id,),
    )
    rows = await cur.fetchall()
    return [
        GlossaryEntry(
            id=r["id"],
            novel_id=r["novel_id"],
            term_zh=r["term_zh"],
            term_en=r["term_en"],
            category=r["category"],
            notes=r["notes"],
            usage_note=r["usage_note"],
            auto_detected=bool(r["auto_detected"]),
            locked=bool(r["locked"]),
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


def english_term_present(term: str, haystack: str) -> bool:
    """Substring membership of a glossary `term_en` in a body of text.

    Generic rank/grade/tier descriptors ("Second-Rank", "late stage") match
    case-insensitively — the translator deliberately re-cases them mid-sentence
    — while genuine proper nouns stay case-sensitive (casing is meaning-bearing
    for cultivation names)."""
    if _GENERIC_RANK_RE.match(term):
        return term.lower() in haystack.lower()
    return term in haystack


# Parenthetical metadata appended to a glossary `term_en` — the substantive
# English is what precedes the first `(`. Used at check time only; the stored
# entry is never rewritten. `Demonic Path (philosophy/affiliation)` checks as
# `Demonic Path`.
_TERM_EN_PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _check_variants(term_en: str) -> list[str]:
    """Strings to try when checking presence of a glossary `term_en` in a
    translation. Handles two soft-row conventions:

      - Trailing parenthetical metadata is stripped: `Demonic Path
        (philosophy/affiliation)` → `Demonic Path`.
      - Slash alternatives are split: `Karma / Karmic Threads` → both
        `Karma` and `Karmic Threads`.

    These two transformations compose — `Demonic Path (general) / Demonic
    Sect (organizational)` yields both `Demonic Path` and `Demonic Sect`.

    Returns a deduped list. The caller passes if ANY variant is present in
    the translation."""
    term = (term_en or "").strip()
    if not term:
        return []
    # Trailing parenthetical first — applied AFTER alias split too, since each
    # alias half may carry its own parenthetical.
    stripped = _TERM_EN_PARENTHETICAL_RE.sub("", term).strip()
    parts = [
        _TERM_EN_PARENTHETICAL_RE.sub("", p).strip()
        for p in _ALIAS_SEP_RE.split(stripped)
        if p.strip()
    ]
    # If no slashes, parts has one entry equal to stripped.
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def headword_for_substitution(term_en: str) -> str:
    """Bare English form suitable for inline injection (e.g., into NMT input).

    Strips trailing parenthetical metadata and any alias alternates, returning
    the first remaining variant. Use this only at the substitution boundary,
    the stored `term_en` preserves the user-visible descriptor syntax used by
    the LLM prompt and the UI."""
    variants = _check_variants(term_en)
    return variants[0] if variants else ""


_LOCKED_IDIOM_VARIANTS: dict[str, tuple[str, ...]] = {
    "you court death": (
        "you court death",
        "court death",
        "courts death",
        "courted death",
        "courting death",
    ),
}


def _check_variants_for_entry(g: GlossaryEntry, term_en: str) -> list[str]:
    """Presence variants for one glossary row.

    Soft rows still get only the normal parenthetical/slash handling. Locked
    idiom rows may add a small hand-approved inflection set so grammar can
    bend around a fixed expression without accepting loose paraphrases.
    """
    variants = _check_variants(term_en)
    if g.category != "idiom":
        return variants
    out: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        extras = _LOCKED_IDIOM_VARIANTS.get(variant.strip().lower(), (variant,))
        for item in extras:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
    return out


def _covered_by_longer_locked_alias(
    src_canon: str,
    term_canon: str,
    start: int,
    locked_aliases: list[str],
) -> bool:
    """True when this source occurrence of a shorter locked term sits wholly
    inside a longer locked alias.

    Example: 宝光 appears inside 长曜宝光洞天. If the long title is translated
    correctly, requiring the shorter common-noun row ("treasure light") as a
    separate phrase creates a false missing-term retry.
    """
    end = start + len(term_canon)
    for alias in locked_aliases:
        if len(alias) <= len(term_canon) or term_canon not in alias:
            continue
        pos = src_canon.find(alias)
        while pos != -1:
            if pos <= start and end <= pos + len(alias):
                return True
            pos = src_canon.find(alias, pos + 1)
    return False


def _known_false_substring(term_canon: str, src_canon: str, start: int) -> bool:
    """Suppress a few high-confidence Chinese substring false positives.

    These are not word-boundary problems English can solve later: the glyph
    sequence is present, but it belongs to a different word entirely.
    """
    end = start + len(term_canon)
    prev_ch = src_canon[start - 1] if start > 0 else ""
    next_ch = src_canon[end] if end < len(src_canon) else ""
    # 道身 ("Dao Body") inside 一道身影 ("a figure").
    if term_canon == canonical_zh("道身") and prev_ch == "一" and next_ch == "影":
        return True
    # 魔道 ("Demonic Path") inside 血魔道人 ("Blood Demon Daoist").
    if term_canon == canonical_zh("魔道") and prev_ch == "血" and next_ch == "人":
        return True
    return False


def _source_has_checkable_term(
    src_canon: str,
    term_canon: str,
    locked_aliases: list[str],
) -> bool:
    """Return True if `term_canon` has at least one source occurrence that is
    not just a substring of another locked term or a known false word split."""
    pos = src_canon.find(term_canon)
    while pos != -1:
        if not _covered_by_longer_locked_alias(
            src_canon, term_canon, pos, locked_aliases
        ) and not _known_false_substring(term_canon, src_canon, pos):
            return True
        pos = src_canon.find(term_canon, pos + 1)
    return False


def missing_translator_terms(
    chapter_zh: str, translated: str, glossary: list[GlossaryEntry]
) -> list[tuple[str, str]]:
    """Locked glossary terms whose Chinese appears in the source chapter but
    whose English rendering is absent from the translation.

    Returns (zh_variant, expected_en) pairs. The translator output is the
    canonical reader text, so a dropped locked term here is a substantive
    fidelity miss, not a cosmetic one. Alias-aware: a locked `筑基 / 築基`
    row is verified against whichever variant appears in the source.

    Noise reduction:
      - Parenthetical metadata on `term_en` is stripped at check time
        (`Demonic Path (philosophy/affiliation)` checks as `Demonic Path`).
      - Slash alternatives on `term_en` are split and any variant counts
        (`Karma / Karmic Threads` passes if either appears).
      - Locked idiom rows can accept a narrow, hand-approved inflection set
        when grammar requires it (`you court death` -> `courting death`).
      - Atomic locked terms (per `is_atomic_case_locked_term`) keep
        case-sensitive checking — casing is meaning-bearing for proper
        nouns. Soft rows get case-insensitive checking; this keeps real
        casing drift visible while preventing slash / parenthetical /
        lowercase-note rows from generating spurious retry payloads."""
    if not chapter_zh or not translated or not glossary:
        return []
    src_canon = canonical_zh(chapter_zh)
    haystack_lower = translated.lower()
    missing: list[tuple[str, str]] = []
    seen_en: set[str] = set()
    locked_aliases = [
        canonical_zh(zh)
        for g in glossary
        if g.locked
        for zh, _ in split_aliases(g.term_zh or "", g.term_en or "")
        if zh and len(zh) >= 2
    ]
    for g in glossary:
        if not g.locked:
            continue
        atomic = is_atomic_case_locked_term(g)
        for zh, en in split_aliases(g.term_zh or "", g.term_en or ""):
            # 1-char zh variants match almost any chapter — skip, as elsewhere.
            if not zh or not en or len(zh) < 2:
                continue
            cz = canonical_zh(zh)
            if (
                not cz
                or en in seen_en
                or not _source_has_checkable_term(src_canon, cz, locked_aliases)
            ):
                continue
            seen_en.add(en)
            variants = _check_variants_for_entry(g, en)
            if not variants:
                continue
            if atomic:
                # Atomic: exact-casing on at least one variant.
                if any(english_term_present(v, translated) for v in variants):
                    continue
            else:
                # Soft: case-insensitive on at least one variant.
                if any(v.lower() in haystack_lower for v in variants):
                    continue
            missing.append((zh, en))
    return missing


async def merge_new_terms(
    conn: aiosqlite.Connection, novel_id: int, terms: list[NewTerm]
) -> None:
    """Insert new auto-detected terms; update auto-detected entries; never touch
    locked entries. Single atomic upsert per term so two concurrent
    _translate_one calls extracting the same bracketed term don't race on
    SELECT-then-INSERT and crash the second one with IntegrityError on
    UNIQUE(novel_id, term_zh) — which would in turn mark the (already-
    committed) translation as 'error'.

    The ON CONFLICT upsert keys on the *exact* `term_zh` string, so it can't
    see a simplified/traditional script variant of a term already in the
    glossary (索唤 vs locked 索喚). We pre-load the novel's terms and skip any
    incoming term whose canonical (script-folded) form already exists under a
    different literal `term_zh` — it's the same term, already tracked.
    The SELECT-then-decide is safe against double-insert because translation
    is strictly serialized (one process-global lock), so two merge_new_terms
    calls for one novel never overlap; the exact-match case stays guarded by
    the UNIQUE constraint regardless."""
    cur = await conn.execute(
        "SELECT term_zh, term_en FROM glossary_entries WHERE novel_id = ?",
        (novel_id,),
    )
    # canonical form -> the literal term_zh row already covering it. Every
    # alias variant of a slash-delimited row is registered, so an auto-detected
    # `筑基` is recognized as already covered by a locked `筑基 / 築基`.
    existing: dict[str, str] = {}
    for r in await cur.fetchall():
        for zh, _ in split_aliases(r[0] or "", r[1] or ""):
            cz = canonical_zh(zh)
            if cz:
                existing.setdefault(cz, r[0])
    # Bug #5: atomic batch. Previously the loop ran each INSERT in
    # SQLite's implicit per-statement autocommit, so an exception mid-loop
    # (a connection drop, an unexpected constraint surprise) left the
    # glossary in a partial state — some terms in, some not. The caller
    # would stamp glossary_merge_error on the chapter but never re-attempt
    # the missing rows. Wrap the loop in an explicit BEGIN/COMMIT so the
    # outcome is all-or-nothing.
    await conn.execute("BEGIN")
    try:
        for term in terms:
            zh = term.zh.strip()
            en = _normalize_extracted_casing(term.en.strip(), term.category)
            if not zh or not en:
                continue
            canon = canonical_zh(zh)
            prior = existing.get(canon)
            if prior is not None and prior != zh:
                # Same term, already in the glossary under a different Han script.
                # Inserting would create a duplicate UNIQUE(novel_id, term_zh)
                # cannot catch. Skip — the translator already had it in-prompt.
                logger.info(
                    "glossary: skipping script-variant %r of existing term %r "
                    "(novel %d)",
                    zh,
                    prior,
                    novel_id,
                )
                continue
            await conn.execute(
                "INSERT INTO glossary_entries "
                "(novel_id, term_zh, term_en, category, auto_detected, locked) "
                "VALUES (?, ?, ?, ?, 1, 0) "
                "ON CONFLICT(novel_id, term_zh) DO UPDATE SET "
                "term_en = excluded.term_en, category = excluded.category "
                "WHERE locked = 0",
                (novel_id, zh, en, term.category),
            )
            # Keep the map current so two script variants in the same batch don't
            # both insert.
            existing[canon] = zh
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


class LockedEntryConflict(Exception):
    """Raised when a manual add targets a term_zh that already exists and is locked."""


async def create_or_overwrite_entry(
    conn: aiosqlite.Connection,
    novel_id: int,
    term_zh: str,
    term_en: str,
    category: str,
    notes: str | None,
    usage_note: str | None = None,
) -> GlossaryEntry:
    """Manual add. Inserts a new locked entry, or overwrites an existing
    unlocked (auto-detected) entry. Raises LockedEntryConflict if the existing
    entry is locked. Raises ValueError on empty terms.

    An exact `term_zh` match is preferred; failing that, a simplified/
    traditional script variant of an existing entry counts as a match too, so
    manually adding 索唤 when locked 索喚 already exists is reported as a
    conflict instead of creating a duplicate row."""
    zh = term_zh.strip()
    en = term_en.strip()
    if not zh or not en:
        raise ValueError("term_zh and term_en must not be empty")

    cur = await conn.execute(
        "SELECT id, locked FROM glossary_entries "
        "WHERE novel_id = ? AND term_zh = ?",
        (novel_id, zh),
    )
    existing = await cur.fetchone()

    if existing is None:
        # No exact row — fall back to a script-folded, alias-aware comparison
        # so adding `筑基` when a locked `筑基 / 築基` exists is reported as a
        # conflict rather than creating a duplicate row.
        canon = canonical_zh(zh)
        cur = await conn.execute(
            "SELECT id, term_zh, term_en, locked FROM glossary_entries "
            "WHERE novel_id = ?",
            (novel_id,),
        )
        for r in await cur.fetchall():
            variants = {
                canonical_zh(v)
                for v, _ in split_aliases(r["term_zh"] or "", r["term_en"] or "")
            }
            if canon in variants:
                existing = r
                break

    if existing is None:
        cur = await conn.execute(
            "INSERT INTO glossary_entries "
            "(novel_id, term_zh, term_en, category, notes, usage_note, auto_detected, locked) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 1)",
            (novel_id, zh, en, category, notes, usage_note),
        )
        new_id = cur.lastrowid
        await conn.commit()
        entry = await get_one(conn, new_id)
        assert entry is not None
        return entry

    if existing["locked"]:
        raise LockedEntryConflict(existing["id"])

    await conn.execute(
        "UPDATE glossary_entries "
        "SET term_en = ?, category = ?, notes = ?, usage_note = ?, "
        "    auto_detected = 0, locked = 1, "
        "    updated_at = datetime('now') "
        "WHERE id = ?",
        (en, category, notes, usage_note, existing["id"]),
    )
    await conn.commit()
    entry = await get_one(conn, existing["id"])
    assert entry is not None
    return entry


async def update_entry(
    conn: aiosqlite.Connection,
    entry_id: int,
    term_en: str | None,
    category: str | None,
    notes: str | None,
    usage_note: str | None = None,
    locked: bool | None = None,
) -> GlossaryEntry | None:
    """User edit. If `locked` is explicitly set, it is honored. Otherwise an
    edit that touches another field implicitly locks the entry (so the user's
    correction won't be overwritten by later auto-detection)."""
    sets: list[str] = []
    params: list[object] = []
    if term_en is not None:
        # Pydantic's min_length=1 on GlossaryUpdate catches the empty-string
        # case; this guard catches whitespace-only ("   ") which Pydantic
        # accepts. Same shape as create_or_overwrite_entry's post-strip
        # validation, so create and update behave consistently.
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
    if locked is not None:
        sets.append("locked = ?")
        params.append(1 if locked else 0)
    elif sets:
        # Implicit lock-on-edit: only when another field is changing AND the
        # caller didn't say anything about locked.
        sets.append("locked = 1")
    if not sets:
        return await get_one(conn, entry_id)
    # Phase D: stamp updated_at on every PATCH. A column that only carries
    # the insert time would never drive the stale-glossary watermark in the
    # reader / glossary UI; the whole point of the column is to mark when
    # the term's English text was last touched relative to the chapters
    # that used the prior rendering. Test:
    # test_glossary.py::test_update_entry_stamps_updated_at.
    sets.append("updated_at = datetime('now')")
    params.append(entry_id)
    await conn.execute(
        f"UPDATE glossary_entries SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    await conn.commit()
    return await get_one(conn, entry_id)


async def delete_entry(conn: aiosqlite.Connection, entry_id: int) -> bool:
    cur = await conn.execute(
        "DELETE FROM glossary_entries WHERE id = ?", (entry_id,)
    )
    await conn.commit()
    return cur.rowcount > 0


async def find_chapters_using_term(
    conn: aiosqlite.Connection, novel_id: int, term_zh: str
) -> list[dict]:
    """Return chapters whose original_text contains the given term.

    Uses INSTR so the term can contain any characters (including SQL LIKE
    metacharacters) without escaping.
    """
    term = (term_zh or "").strip()
    if not term:
        return []
    cur = await conn.execute(
        "SELECT id, chapter_num, title_zh, title_en, status, translate_queued "
        "FROM chapters "
        "WHERE novel_id = ? AND INSTR(original_text, ?) > 0 "
        "ORDER BY chapter_num",
        (novel_id, term),
    )
    rows = await cur.fetchall()
    return [
        {
            "id": r["id"],
            "chapter_num": r["chapter_num"],
            "title_zh": r["title_zh"],
            "title_en": r["title_en"],
            "status": r["status"],
            "translate_queued": r["translate_queued"],
        }
        for r in rows
    ]


async def get_one(
    conn: aiosqlite.Connection, entry_id: int
) -> GlossaryEntry | None:
    cur = await conn.execute(
        "SELECT id, novel_id, term_zh, term_en, category, notes, usage_note, "
        "auto_detected, locked, updated_at FROM glossary_entries WHERE id = ?",
        (entry_id,),
    )
    r = await cur.fetchone()
    if r is None:
        return None
    return GlossaryEntry(
        id=r["id"],
        novel_id=r["novel_id"],
        term_zh=r["term_zh"],
        term_en=r["term_en"],
        category=r["category"],
        notes=r["notes"],
        usage_note=r["usage_note"],
        auto_detected=bool(r["auto_detected"]),
        locked=bool(r["locked"]),
        updated_at=r["updated_at"],
    )
