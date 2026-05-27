"""Project-wide find/replace engine with frozen-preview commit contract
(Initiative 4).

Two-phase flow keeps the user in control and prevents DB drift between
seeing the preview and writing the replacement:

1. **Preview** (`build_preview`) — scans matching chapters, computes
   per-chapter hit counts + content hashes, samples a few example lines,
   and stores everything under an opaque token (5-min TTL). Returns the
   token plus the preview rows.

2. **Commit** (`commit_preview`) — looks the token up, rehashes the same
   chapters' content, and refuses if anything changed (a background
   translation finishing, a concurrent edit). Otherwise runs the
   substitution as a single transaction; FTS5 auto-syncs via the existing
   `chapter_fts_au` trigger.

The engine operates on `chapters.translated_text` and/or
`chapters.refined_text` (never `original_text` — source is immutable).
Regex is supported but without capturing-group replacement in v1; the
in-place glossary path uses plain word-boundary substitution which fits
the no-capture constraint.

Also exposed: `apply_in_place_for_glossary_term` — the integration point
the glossary PATCH route uses when the user chooses "Apply to existing
translations" after editing a `term_en`. Same engine, no preview gate (the
glossary dialog has shown the user what they're doing), word-boundary +
case-sensitive substitution scoped to the right novel set.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Literal

import aiosqlite

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Cap the find/replace strings so a stray multi-MB regex doesn't pin the
# event loop in re.compile. Real workflows are short tokens — names,
# phrases. Both halves get the same cap.
MAX_PATTERN_BYTES = 1024
MAX_REPLACEMENT_BYTES = 4096

# How long a preview token stays valid before the user has to re-preview.
# Long enough to read the preview + scroll the matches; short enough that
# a stale token can't be replayed days later against changed content.
PREVIEW_TOKEN_TTL_SECONDS = 300  # 5 minutes

# Sample line cap per chapter in the preview response — keeps payloads
# small without leaving the user blind to what they're about to commit.
PREVIEW_SAMPLE_LINES_PER_CHAPTER = 3

# Total cap on chapter-count returned in one preview. Even a project-wide
# replace across 200 novels × 1000 chapters should not push 200k preview
# rows down to the browser; clamp and tell the user.
MAX_PREVIEW_CHAPTERS = 5000


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FindReplaceError(Exception):
    """Base for engine-level errors. Routes translate to 4xx HTTP."""


class InvalidPatternError(FindReplaceError):
    """Bad input: empty find string, regex that won't compile, oversize."""


class TokenExpiredError(FindReplaceError):
    """Preview token unknown or past TTL."""


class PreviewDriftError(FindReplaceError):
    """At least one chapter's content changed between preview and commit.
    Carries the set of drifted chapter ids so the UI can show which."""

    def __init__(self, drifted_chapter_ids: list[int]) -> None:
        super().__init__(
            f"{len(drifted_chapter_ids)} chapter(s) changed since preview; "
            f"re-preview required"
        )
        self.drifted_chapter_ids = drifted_chapter_ids


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


TargetCol = Literal["translated_text", "refined_text"]


@dataclass
class FindReplaceQuery:
    """Normalized inputs to the engine. Routes build one of these from the
    request body; commit revalidates against the stored snapshot."""

    find: str
    replacement: str
    scope_kind: Literal["chapter", "novel", "novels", "all"]
    # When scope_kind=="chapter": [chapter_id]. "novel": [novel_id].
    # "novels": [novel_id...]. "all": [] (empty, scope is implicit).
    scope_ids: list[int] = field(default_factory=list)
    target_cols: list[TargetCol] = field(default_factory=lambda: ["translated_text", "refined_text"])
    use_regex: bool = False
    case_sensitive: bool = True
    word_boundary: bool = False


@dataclass
class ChapterPreviewRow:
    """One row in the preview response — what would happen to one chapter."""

    chapter_id: int
    novel_id: int
    novel_title: str
    chapter_num: int
    chapter_title_en: str | None
    # Hit counts split by column so the UI can show which body changes.
    hits_translated: int
    hits_refined: int
    # Up to PREVIEW_SAMPLE_LINES_PER_CHAPTER context lines containing
    # at least one match (truncated to keep the payload small).
    sample_lines: list[str]


@dataclass
class PreviewResult:
    """Full preview payload returned by `build_preview`."""

    token: str
    expires_at: float
    total_chapters: int
    total_hits_translated: int
    total_hits_refined: int
    rows: list[ChapterPreviewRow]
    truncated: bool  # True when MAX_PREVIEW_CHAPTERS clamped the response


@dataclass
class CommitResult:
    """Returned by `commit_preview` on successful write."""

    chapters_updated: int
    rows_updated_translated: int
    rows_updated_refined: int
    # Only populated by `apply_in_place_for_glossary_term` — the generic
    # find/replace engine deliberately never touches title_en (titles are
    # rewritten by the `normalize_title_en` step at translate time and the
    # user-facing find/replace UI is scoped to body text). Defaults to 0
    # so the generic commit_preview path stays untouched.
    rows_updated_titles: int = 0


# Internal in-memory store. {token: _StoredPreview}. Process-local;
# stays under the EXE's single asyncio loop. A simple dict is enough —
# tokens are short-lived and bounded by user interactions.
@dataclass
class _StoredPreview:
    query: FindReplaceQuery
    chapter_hashes: dict[int, str]  # chapter_id → SHA256
    created_at: float


_PREVIEW_STORE: dict[str, _StoredPreview] = {}


def _now() -> float:
    return time.time()


def _gc_expired_tokens() -> None:
    """Drop expired tokens. Cheap to run on every operation since the dict
    is small (5-min TTL × per-user click rate keeps it tiny)."""
    cutoff = _now() - PREVIEW_TOKEN_TTL_SECONDS
    expired = [t for t, s in _PREVIEW_STORE.items() if s.created_at < cutoff]
    for t in expired:
        _PREVIEW_STORE.pop(t, None)


# Test hook: lets tests purge the store between runs without reaching into
# private state. Calling it from production code is a smell; calling it
# from a teardown fixture is fine.
def _reset_token_store_for_tests() -> None:
    _PREVIEW_STORE.clear()


def _hash_chapter_content(translated: str | None, refined: str | None) -> str:
    """Stable hash of a chapter's mutable bodies. Used for drift detection;
    NOT a cryptographic identity, just a fast fingerprint. Both halves go
    in so a change to either column is visible at commit time."""
    h = hashlib.sha256()
    h.update((translated or "").encode("utf-8"))
    # Null byte separator so [a]+[b] and [ab]+[] still hash differently.
    h.update(b"\x00")
    h.update((refined or "").encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Pattern building
# ---------------------------------------------------------------------------


def _build_pattern(query: FindReplaceQuery) -> re.Pattern:
    """Compile the find pattern. Plain strings are escaped; regex input
    flows through as-is. Word-boundary wrapping is added when requested.

    Reject input that exceeds MAX_PATTERN_BYTES or fails to compile."""
    raw = query.find
    if not raw:
        raise InvalidPatternError("find string must not be empty")
    if len(raw.encode("utf-8")) > MAX_PATTERN_BYTES:
        raise InvalidPatternError(
            f"find string exceeds {MAX_PATTERN_BYTES}-byte cap"
        )
    if len(query.replacement.encode("utf-8")) > MAX_REPLACEMENT_BYTES:
        raise InvalidPatternError(
            f"replacement string exceeds {MAX_REPLACEMENT_BYTES}-byte cap"
        )
    pattern_src = raw if query.use_regex else re.escape(raw)
    if query.word_boundary:
        # \b doesn't fire between non-word chars, so CJK-only patterns
        # silently no-op when word_boundary is on. The route validates
        # this upstream — the engine just compiles what it's given.
        pattern_src = rf"\b{pattern_src}\b"
    flags = 0 if query.case_sensitive else re.IGNORECASE
    try:
        return re.compile(pattern_src, flags)
    except re.error as e:
        raise InvalidPatternError(f"invalid regex: {e}") from e


def _validate_replacement_no_groups(replacement: str) -> None:
    """v1 rejects capture-group references in the replacement string —
    \\1, \\g<...>, \\g<name>. Adding capture support is straightforward
    but punted to a future iteration so the contract stays simple."""
    if re.search(r"\\[0-9]", replacement):
        raise InvalidPatternError(
            "capture-group references (\\1, \\2 …) are not supported in v1; "
            "use a literal replacement"
        )
    if re.search(r"\\g<", replacement):
        raise InvalidPatternError(
            "named capture-group references (\\g<…>) are not supported in v1"
        )


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


async def _select_chapters_for_scope(
    conn: aiosqlite.Connection, query: FindReplaceQuery
) -> list[aiosqlite.Row]:
    """Fetch (id, novel_id, chapter_num, title_en, translated_text,
    refined_text, novel_title) for every chapter in scope.

    Per-chapter materialization is required because we need the body text
    to count hits and compute the drift hash. A pure-SQL count via INSTR
    wouldn't honor regex / case-sensitivity / word-boundary.
    """
    base_select = (
        "SELECT c.id, c.novel_id, c.chapter_num, c.title_en, "
        "c.translated_text, c.refined_text, n.title AS novel_title "
        "FROM chapters c JOIN novels n ON n.id = c.novel_id "
        "WHERE c.status = 'done' "
    )
    where_clause = ""
    params: list = []
    if query.scope_kind == "chapter":
        if not query.scope_ids:
            raise InvalidPatternError("scope_kind=chapter requires scope_ids")
        placeholders = ",".join("?" * len(query.scope_ids))
        where_clause = f"AND c.id IN ({placeholders}) "
        params.extend(query.scope_ids)
    elif query.scope_kind == "novel":
        if not query.scope_ids or len(query.scope_ids) != 1:
            raise InvalidPatternError("scope_kind=novel requires exactly one novel_id")
        where_clause = "AND c.novel_id = ? "
        params.append(query.scope_ids[0])
    elif query.scope_kind == "novels":
        if not query.scope_ids:
            raise InvalidPatternError("scope_kind=novels requires at least one novel_id")
        placeholders = ",".join("?" * len(query.scope_ids))
        where_clause = f"AND c.novel_id IN ({placeholders}) "
        params.extend(query.scope_ids)
    elif query.scope_kind == "all":
        pass
    else:
        raise InvalidPatternError(f"unknown scope_kind: {query.scope_kind!r}")

    cur = await conn.execute(
        base_select + where_clause + "ORDER BY c.novel_id, c.chapter_num",
        params,
    )
    return await cur.fetchall()


# ---------------------------------------------------------------------------
# Preview + commit
# ---------------------------------------------------------------------------


def _sample_match_lines(body: str, pattern: re.Pattern) -> list[str]:
    """Return up to PREVIEW_SAMPLE_LINES_PER_CHAPTER lines containing at
    least one match, truncated to a sensible width. Splits on \\n so a
    long paragraph counts as one line — sample size is line-bounded, not
    char-bounded."""
    out: list[str] = []
    for line in (body or "").split("\n"):
        if pattern.search(line):
            # Trim very long lines (keep the first ~160 chars; that's
            # enough surrounding context for a name substitution).
            snippet = line.strip()
            if len(snippet) > 160:
                snippet = snippet[:157] + "…"
            out.append(snippet)
            if len(out) >= PREVIEW_SAMPLE_LINES_PER_CHAPTER:
                break
    return out


async def build_preview(
    conn: aiosqlite.Connection, query: FindReplaceQuery
) -> PreviewResult:
    """Build a preview and store the frozen snapshot under a fresh token."""
    _validate_replacement_no_groups(query.replacement)
    pattern = _build_pattern(query)
    rows = await _select_chapters_for_scope(conn, query)

    preview_rows: list[ChapterPreviewRow] = []
    chapter_hashes: dict[int, str] = {}
    total_translated_hits = 0
    total_refined_hits = 0
    truncated = False

    for r in rows:
        translated = r["translated_text"]
        refined = r["refined_text"]
        n_trans = len(pattern.findall(translated or "")) if "translated_text" in query.target_cols else 0
        n_ref = len(pattern.findall(refined or "")) if "refined_text" in query.target_cols else 0
        if n_trans == 0 and n_ref == 0:
            continue
        if len(preview_rows) >= MAX_PREVIEW_CHAPTERS:
            truncated = True
            break
        # Sample lines preferentially from the body the user will actually
        # see — refined wins when present and refinement is targeted.
        sample_source = ""
        if "refined_text" in query.target_cols and refined:
            sample_source = refined
        elif "translated_text" in query.target_cols and translated:
            sample_source = translated
        elif refined:
            sample_source = refined
        else:
            sample_source = translated or ""
        preview_rows.append(
            ChapterPreviewRow(
                chapter_id=r["id"],
                novel_id=r["novel_id"],
                novel_title=r["novel_title"],
                chapter_num=r["chapter_num"],
                chapter_title_en=r["title_en"],
                hits_translated=n_trans,
                hits_refined=n_ref,
                sample_lines=_sample_match_lines(sample_source, pattern),
            )
        )
        chapter_hashes[r["id"]] = _hash_chapter_content(translated, refined)
        total_translated_hits += n_trans
        total_refined_hits += n_ref

    _gc_expired_tokens()
    token = secrets.token_urlsafe(24)
    _PREVIEW_STORE[token] = _StoredPreview(
        query=query,
        chapter_hashes=chapter_hashes,
        created_at=_now(),
    )
    return PreviewResult(
        token=token,
        expires_at=_now() + PREVIEW_TOKEN_TTL_SECONDS,
        total_chapters=len(preview_rows),
        total_hits_translated=total_translated_hits,
        total_hits_refined=total_refined_hits,
        rows=preview_rows,
        truncated=truncated,
    )


async def commit_preview(
    conn: aiosqlite.Connection, token: str
) -> CommitResult:
    """Apply the substitution against the frozen chapter set.

    Refuses on drift: if any chapter's translated_text or refined_text
    has changed since the preview, the commit is rejected and the user
    must re-preview. Without this guard a background translation finishing
    between preview and commit would silently apply the user's replacement
    against text they never saw.
    """
    _gc_expired_tokens()
    stored = _PREVIEW_STORE.get(token)
    if stored is None:
        raise TokenExpiredError(token)
    if _now() - stored.created_at > PREVIEW_TOKEN_TTL_SECONDS:
        _PREVIEW_STORE.pop(token, None)
        raise TokenExpiredError(token)

    query = stored.query
    pattern = _build_pattern(query)

    if not stored.chapter_hashes:
        # No matches to apply; token is consumed regardless so the UI
        # can't replay the same preview hoping the answer changed.
        _PREVIEW_STORE.pop(token, None)
        return CommitResult(
            chapters_updated=0,
            rows_updated_translated=0,
            rows_updated_refined=0,
        )

    chapter_ids = list(stored.chapter_hashes.keys())
    placeholders = ",".join("?" * len(chapter_ids))
    cur = await conn.execute(
        f"SELECT id, novel_id, translated_text, refined_text FROM chapters "
        f"WHERE id IN ({placeholders})",
        chapter_ids,
    )
    fetched = list(await cur.fetchall())
    current = {r["id"]: (r["translated_text"], r["refined_text"]) for r in fetched}
    # F36 (2026-05-25): snapshot recording. Group chapters by novel so
    # each novel's restore History stays scoped to its own chapters.
    # A cross-novel commit produces one snapshot row per touched novel.
    chapter_novel = {r["id"]: r["novel_id"] for r in fetched}

    drifted: list[int] = []
    for cid, expected_hash in stored.chapter_hashes.items():
        bodies = current.get(cid)
        if bodies is None:
            # Row went away — count as drift; user must re-preview to
            # see the smaller universe.
            drifted.append(cid)
            continue
        if _hash_chapter_content(bodies[0], bodies[1]) != expected_hash:
            drifted.append(cid)
    if drifted:
        raise PreviewDriftError(drifted)

    # Apply substitutions in one transaction. The chapter_fts_au trigger
    # fires per UPDATE row; SQLite handles that fine inside a single
    # transaction (which the per-statement implicit BEGIN gives us with
    # the explicit commit() at the end).
    # Build the snapshot payload AS we walk — captures pre-substitution
    # bodies for restore. Grouped by novel_id; written after the UPDATEs
    # but before commit so the snapshot lives in the same transaction.
    snapshot_payloads: dict[int, dict[str, dict]] = {}
    rows_translated = 0
    rows_refined = 0
    for cid, (translated, refined) in current.items():
        new_translated = translated
        new_refined = refined
        change_translated = False
        change_refined = False
        if "translated_text" in query.target_cols and translated:
            substituted, n = pattern.subn(query.replacement, translated)
            if n > 0:
                new_translated = substituted
                change_translated = True
        if "refined_text" in query.target_cols and refined:
            substituted, n = pattern.subn(query.replacement, refined)
            if n > 0:
                new_refined = substituted
                change_refined = True
        if not (change_translated or change_refined):
            continue
        # Stash pre-substitution bodies for restore. Only fields the
        # commit actually changed go in (saves payload bytes).
        novel_id = chapter_novel.get(cid)
        if novel_id is not None:
            payload = snapshot_payloads.setdefault(novel_id, {})
            before: dict[str, str | None] = {}
            if change_translated:
                before["translated_before"] = translated
            if change_refined:
                before["refined_before"] = refined
            payload[str(cid)] = before
        if change_translated and change_refined:
            await conn.execute(
                "UPDATE chapters SET translated_text = ?, refined_text = ? "
                "WHERE id = ?",
                (new_translated, new_refined, cid),
            )
        elif change_translated:
            await conn.execute(
                "UPDATE chapters SET translated_text = ? WHERE id = ?",
                (new_translated, cid),
            )
        else:
            await conn.execute(
                "UPDATE chapters SET refined_text = ? WHERE id = ?",
                (new_refined, cid),
            )
        if change_translated:
            rows_translated += 1
        if change_refined:
            rows_refined += 1

    # Record snapshots per touched novel — same transaction as the
    # UPDATEs so a crash between them can't leave un-restorable changes.
    from backend.services.fr_snapshots import record_snapshot  # noqa: PLC0415
    target_label = (
        "both"
        if {"translated_text", "refined_text"} <= set(query.target_cols)
        else (query.target_cols[0] if query.target_cols else "both")
    )
    for novel_id, payload in snapshot_payloads.items():
        await record_snapshot(
            conn,
            novel_id=novel_id,
            commit_token=token,
            find_pattern=query.find,
            replace_pattern=query.replacement,
            target=target_label,
            scope=query.scope_kind,
            chapters_changed=len(payload),
            payload=payload,
        )

    await conn.commit()

    # Token is single-use — drop after a successful commit so the user
    # can't replay against a now-different DB.
    _PREVIEW_STORE.pop(token, None)
    return CommitResult(
        chapters_updated=len({cid for cid in current.keys()
                              if cid in stored.chapter_hashes}),
        rows_updated_translated=rows_translated,
        rows_updated_refined=rows_refined,
    )


# ---------------------------------------------------------------------------
# Glossary integration — apply a term_en change in-place
# ---------------------------------------------------------------------------


async def apply_in_place_for_glossary_term(
    conn: aiosqlite.Connection,
    old_en: str,
    new_en: str,
    novel_id: int | None,
) -> CommitResult:
    """Substitute every word-boundary occurrence of `old_en` with `new_en`
    across the relevant chapters. Used by the glossary PATCH route when
    the user picks "Apply to existing translations" after editing a term.

    Scope: a non-None `novel_id` restricts to that novel; None means every
    novel (the global-glossary edit case). Always case-sensitive (English
    proper nouns are meaning-bearing) and word-boundary (so editing
    "Bai Xiaochun" doesn't disturb "Bai Xiaochuns'").

    Bypasses the preview/token gate — the glossary dialog has already
    shown the user the impact and confirmed. Direct single-step substitution.
    """
    if not old_en or not new_en or old_en == new_en:
        return CommitResult(0, 0, 0)
    query = FindReplaceQuery(
        find=old_en,
        replacement=new_en,
        scope_kind="novel" if novel_id is not None else "all",
        scope_ids=[novel_id] if novel_id is not None else [],
        target_cols=["translated_text", "refined_text"],
        use_regex=False,
        case_sensitive=True,
        word_boundary=True,
    )
    pattern = _build_pattern(query)
    rows = await _select_chapters_for_scope(conn, query)
    rows_translated = 0
    rows_refined = 0
    rows_titles = 0
    chapters_touched: set[int] = set()
    for r in rows:
        translated = r["translated_text"]
        refined = r["refined_text"]
        title_en = r["title_en"]
        new_translated = translated
        new_refined = refined
        new_title = title_en
        change_translated = False
        change_refined = False
        change_title = False
        if translated:
            substituted, n = pattern.subn(new_en, translated)
            if n > 0:
                new_translated = substituted
                change_translated = True
        if refined:
            substituted, n = pattern.subn(new_en, refined)
            if n > 0:
                new_refined = substituted
                change_refined = True
        if title_en:
            substituted, n = pattern.subn(new_en, title_en)
            if n > 0:
                new_title = substituted
                change_title = True
        if not (change_translated or change_refined or change_title):
            continue
        chapters_touched.add(r["id"])
        # One UPDATE per chapter; assemble the SET clause from whichever
        # columns actually changed so we don't rewrite untouched bodies.
        set_parts: list[str] = []
        set_values: list = []
        if change_translated:
            set_parts.append("translated_text = ?")
            set_values.append(new_translated)
        if change_refined:
            set_parts.append("refined_text = ?")
            set_values.append(new_refined)
        if change_title:
            set_parts.append("title_en = ?")
            set_values.append(new_title)
        set_values.append(r["id"])
        await conn.execute(
            f"UPDATE chapters SET {', '.join(set_parts)} WHERE id = ?",
            set_values,
        )
        if change_translated:
            rows_translated += 1
        if change_refined:
            rows_refined += 1
        if change_title:
            rows_titles += 1
    await conn.commit()
    return CommitResult(
        chapters_updated=len(chapters_touched),
        rows_updated_translated=rows_translated,
        rows_updated_refined=rows_refined,
        rows_updated_titles=rows_titles,
    )
