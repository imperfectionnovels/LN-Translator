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
case-sensitive substitution scoped to the right novel set. That path is
alias-aware, records a restore snapshot, and reports what it could not
reach; see its own docstring, which is the authority on those semantics.
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
    # Row ids of the find_replace_snapshots rows written during this commit,
    # one per touched novel. Empty when there were no matches (no snapshots
    # written) or when record_snapshot skipped a novel due to payload size.
    # Callers must treat a SHORT list as "some novels have no undo" rather
    # than assuming one id per touched novel.
    snapshot_ids: list[int] = field(default_factory=list)
    # Block 1 (2026-08-07): honest reporting of what the glossary apply could
    # NOT reach. Only populated by `apply_in_place_for_glossary_term`; the
    # generic commit_preview path leaves them at zero/empty. See that
    # function's docstring for the deliberate counting asymmetry between the
    # two (in-scope status count vs matching-chapters-only).
    skipped_translating: int = 0
    skipped_refining: int = 0
    skipped_refining_chapter_ids: list[int] = field(default_factory=list)


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

    Archived novels are OUT OF SCOPE for every scope kind (bug hunt
    2026-08-04, B1): soft_delete's contract is that an archived novel's
    chapters are "preserved untouched", so neither a novel-wide / global
    find-replace nor the glossary apply-in-place may rewrite them. The
    explicit chapter / single-novel scopes exclude archived too. This is
    consistent (the only UI path to an archived novel is a stale tab), and
    restore-then-apply works again.
    """
    base_select = (
        "SELECT c.id, c.novel_id, c.chapter_num, c.title_en, "
        "c.translated_text, c.refined_text, c.refinement_status, "
        "n.title AS novel_title "
        "FROM chapters c JOIN novels n ON n.id = c.novel_id "
        "WHERE c.status = 'done' AND n.deleted_at IS NULL "
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
    # Same archived-novel exclusion as _select_chapters_for_scope (B1): a
    # novel archived BETWEEN preview and commit drops out of this fetch, so
    # its chapters register as drift below and the commit refuses. Both
    # halves of the flow therefore resolve through the same predicate.
    cur = await conn.execute(
        f"SELECT c.id, c.novel_id, c.translated_text, c.refined_text "
        f"FROM chapters c JOIN novels n ON n.id = c.novel_id "
        f"WHERE c.id IN ({placeholders}) AND n.deleted_at IS NULL",
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
    body_changed_ids: list[int] = []
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
        body_changed_ids.append(cid)

    # CAT Phase 3: re-sync each touched chapter's segment store to its new
    # body IN THE SAME TRANSACTION (status-preserving: confirmed segments
    # stay confirmed through a find-replace). No-op for chapters without
    # segment rows.
    from backend.services import segments as segments_svc  # noqa: PLC0415
    for cid in body_changed_ids:
        await segments_svc.reproject_from_body(conn, cid)

    # Record snapshots per touched novel — same transaction as the
    # UPDATEs so a crash between them can't leave un-restorable changes.
    from backend.services.fr_snapshots import record_snapshot  # noqa: PLC0415
    target_label = (
        "both"
        if {"translated_text", "refined_text"} <= set(query.target_cols)
        else (query.target_cols[0] if query.target_cols else "both")
    )
    snapshot_ids: list[int] = []
    for novel_id, payload in snapshot_payloads.items():
        sid = await record_snapshot(
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
        if sid is not None:
            snapshot_ids.append(sid)

    await conn.commit()

    # Token is single-use — drop after a successful commit so the user
    # can't replay against a now-different DB.
    _PREVIEW_STORE.pop(token, None)
    return CommitResult(
        chapters_updated=len({cid for cid in current.keys()
                              if cid in stored.chapter_hashes}),
        rows_updated_translated=rows_translated,
        rows_updated_refined=rows_refined,
        snapshot_ids=snapshot_ids,
    )


# ---------------------------------------------------------------------------
# Glossary integration — apply a term_en change in-place
# ---------------------------------------------------------------------------


def _split_aliases(s: str) -> list[str]:
    """Split a slash-aliased term into its alternative renderings.

    `term_en` / `term_zh` carry alternatives separated by "/" throughout this
    app; the editor splits them the same way (`zhAliases` in editor-tools.js),
    so the two ends must agree. Trims each piece, drops empties, dedupes while
    preserving first-seen order. The FIRST surviving alias is the primary
    rendering.
    """
    out: list[str] = []
    for piece in (s or "").split("/"):
        alias = piece.strip()
        if alias and alias not in out:
            out.append(alias)
    return out


async def _count_translating_in_scope(
    conn: aiosqlite.Connection, novel_id: int | None
) -> int:
    """In-scope chapters currently mid-translate. Mirrors the scope predicate
    of `_select_chapters_for_scope` (including the archived-novel exclusion)
    but on status='translating' instead of 'done'."""
    sql = (
        "SELECT COUNT(*) AS n FROM chapters c JOIN novels n ON n.id = c.novel_id "
        "WHERE c.status = 'translating' AND n.deleted_at IS NULL"
    )
    params: list = []
    if novel_id is not None:
        sql += " AND c.novel_id = ?"
        params.append(novel_id)
    cur = await conn.execute(sql, params)
    return (await cur.fetchone())["n"]


async def apply_in_place_for_glossary_term(
    conn: aiosqlite.Connection,
    old_en: str,
    new_en: str,
    novel_id: int | None,
) -> CommitResult:
    """Rewrite a renamed glossary term across the relevant chapters' bodies
    and titles. Used by the glossary apply-in-place routes when the user picks
    "Apply to existing translations" after editing a `term_en`.

    Scope: a non-None `novel_id` restricts to that novel; None means every
    active novel (the global-glossary case). Archived novels are always out of
    scope. Case-sensitive (English proper nouns are meaning-bearing) and
    word-boundary anchored (editing "Bai Xiaochun" leaves "Bai Xiaochuns'"
    alone). Bypasses the preview/token gate: the glossary dialog has already
    shown the user the impact.

    ALIAS SEMANTICS. Both sides are slash-split into alias lists. Every OLD
    alias that is absent from the NEW set is a rename target and collapses
    onto the new PRIMARY (first) alias; aliases that survive into the new set
    are left alone. Matching runs as ONE compiled longest-first alternation
    with a CALLABLE replacement, which buys three things a per-alias loop
    cannot:
      * overlapping aliases resolve correctly. With "Bai Xiaochun / Xiaochun",
        a sequential loop that hit the short alias first would turn
        "Bai Xiaochun" into "Bai <new>"; longest-first consumes the long form
        as one match.
      * surviving aliases are protected. They ride the alternation as no-op
        branches (the callable returns the matched text unchanged), so a short
        rename target nested inside a surviving alias cannot corrupt it, for
        example renaming bare "Xiaochun" while "Bai Xiaochun" survives.
      * the replacement is literal. Passing the new rendering as a regex
        template made a backslash in `term_en` raise `re.error: bad escape`
        (a 500 out of the route) and would have read "\\1" as a capture
        reference.

    NOT IDEMPOTENT in one corner: renaming old "A" to new "A B" rewrites each
    "A" to "A B", so a second run over the same pair yields "A B B". The UI
    never re-runs the same pair (it sends the old and new values of a single
    edit), and detecting the already-applied state is not decidable from text
    alone. Do not loop this function over the same rename.

    TRANSACTION GUARANTEE (race fixed 2026-08-08). The scope read
    (`_select_chapters_for_scope`), the refining-skip check, every UPDATE, and
    the snapshot write all run inside ONE `BEGIN IMMEDIATE` transaction opened
    right before the scope SELECT. SQLite serializes writers, so a translate
    worker's claim/success UPDATE or a refiner's commit can no longer land
    in the window between this function's read and its write: whichever
    transaction starts first runs to completion, and the other blocks (up to
    `PRAGMA busy_timeout`) until it can proceed against the
    post-transaction state. A worker's freshly committed body can therefore
    never be overwritten by a stale pre-substitution snapshot computed before
    that commit landed, and the `fr_snapshots` before-image recorded here can
    never go stale either. Precedent: `uploads.py::_append_with_offset`
    (docs/gotchas.md, "Concurrent appends") closes an analogous read-then-write
    race the same way. `commit_preview` in this file instead uses a
    hash-based drift check, because ITS preview/commit split spans a
    user-visible pause that no held transaction can cover; this function's
    read and write happen back to back inside one call, so the stronger,
    cheaper transactional guarantee is available here instead. The
    per-chapter loop is pure DB and CPU work (regex substitution,
    `reproject_from_body`, `record_snapshot`); it makes no LLM or network
    call, so the write-lock hold stays short. The workers' own multi-minute
    LLM calls happen OUTSIDE their transactions (see
    `queue.py::_translate_chapter_in_db`'s claim/success split), so briefly
    blocking on a worker's short claim or success UPDATE, or the reverse, is
    the expected and bounded cost of this fix.

    SKIPS, and how the user finds out. Chapters mid-refinement
    (refinement_status pending/in_progress) are skipped ENTIRELY, body and
    title: the refiner's commit rematerializes the body from its own merge, so
    a rewrite landing now would be clobbered on machine rows. Chapters
    mid-translate are out of scope (the scope SELECT pins status='done'), and
    the transaction above holds the write lock for the SELECT's entire
    lifetime, so a chapter's status or refinement_status cannot flip while
    this function is running either; the refining-claim race that used to
    exist between the scope SELECT and the per-chapter UPDATE is closed for
    the same reason. Both counters exist so the caller can say so out loud
    instead of silently under-delivering. There is deliberately NO auto
    re-apply queue: the editing surface already re-flags these chapters,
    because the consistency glossary tier recomputes live against the current
    glossary on every request and a PATCH implicitly locks the edited entry,
    so a skipped chapter surfaces in the EDITOR'S MISSING-LOCKED TIER on its
    next open. Discovery is automatic; the fix stays a deliberate user action.

    The two skip counts are deliberately asymmetric:
      * `skipped_refining` counts only chapters the rewrite WOULD have
        changed. Their text is in hand (they are status='done'), so the id
        list can be a precise re-apply worklist rather than padded with
        chapters that never contained the term.
      * `skipped_translating` is a plain in-scope status count, no matching.
        A mid-translate row's text is ambiguous: a retranslate still holds the
        OLD body, but a first translation holds NULL, so a match-filtered
        count would report zero for exactly the chapters most likely to emerge
        containing the old rendering. Over-reporting is recoverable; silently
        under-reporting defeats the purpose of the counter.

    Records one find_replace_snapshots row per touched novel before the
    commit, so a rename is undoable from the existing Find/Replace History
    tab. `snapshot_ids` can come back SHORTER than the touched-novel count
    when a novel's payload exceeds the size cap (existing record_snapshot
    contract); callers must not promise a restore point on a short list.

    Aliases whose edges are CJK or punctuation silently match nothing, since
    \\b does not fire between two non-word characters. Pre-existing caveat,
    now evaluated per alias rather than for the whole string.
    """
    old_aliases = _split_aliases(old_en)
    new_aliases = _split_aliases(new_en)
    if not old_aliases or not new_aliases:
        return CommitResult(0, 0, 0)
    # Preserve the size contract the old `_build_pattern` route enforced.
    if len(old_en.encode("utf-8")) > MAX_PATTERN_BYTES:
        raise InvalidPatternError(
            f"find string exceeds {MAX_PATTERN_BYTES}-byte cap"
        )
    if len(new_en.encode("utf-8")) > MAX_REPLACEMENT_BYTES:
        raise InvalidPatternError(
            f"replacement string exceeds {MAX_REPLACEMENT_BYTES}-byte cap"
        )
    new_primary = new_aliases[0]
    new_set = set(new_aliases)
    targets = {a for a in old_aliases if a not in new_set}
    if not targets:
        # Pure reordering / no-op rename (this also covers old_en == new_en):
        # nothing to write, so no snapshot either.
        return CommitResult(0, 0, 0)
    # Survivors ride the alternation as no-op branches so a nested target
    # cannot eat into them; longest-first makes the longer form win.
    alternation = sorted(old_aliases, key=len, reverse=True)
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(a) for a in alternation) + r")\b"
    )

    def _replace(m: re.Match) -> str:
        matched = m.group(0)
        return new_primary if matched in targets else matched

    def _sub(text: str | None) -> tuple[str | None, bool]:
        """Returns (new_text, changed). Uniform across the write path and the
        refining skip count, so a chapter containing only SURVIVING aliases
        counts as unchanged in both."""
        if not text:
            return text, False
        out = pattern.sub(_replace, text)
        return out, out != text

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
    # BEGIN IMMEDIATE opens the write lock BEFORE the scope SELECT (race fixed
    # 2026-08-08). The read, the refining-skip check, every UPDATE, and the
    # snapshot write below all ride this ONE transaction; see the
    # "TRANSACTION GUARANTEE" section of this docstring for the full
    # rationale. Everything from here to the matching commit/rollback is
    # pure DB and CPU work (no LLM/network await), so the hold is short.
    await conn.execute("BEGIN IMMEDIATE")
    try:
        rows = await _select_chapters_for_scope(conn, query)
        rows_translated = 0
        rows_refined = 0
        rows_titles = 0
        skipped_refining = 0
        skipped_refining_ids: list[int] = []
        chapters_touched: set[int] = set()
        body_changed_ids: list[int] = []
        snapshot_payloads: dict[int, dict[str, dict]] = {}
        for r in rows:
            translated = r["translated_text"]
            refined = r["refined_text"]
            title_en = r["title_en"]
            new_translated, change_translated = _sub(translated)
            new_refined, change_refined = _sub(refined)
            new_title, change_title = _sub(title_en)
            if not (change_translated or change_refined or change_title):
                continue
            if r["refinement_status"] in ("pending", "in_progress"):
                # Skip the chapter whole: a partial write here would be undone by
                # the refiner's merge anyway. Counted only because it WOULD have
                # changed, which keeps the id list a usable worklist.
                skipped_refining += 1
                skipped_refining_ids.append(r["id"])
                continue
            chapters_touched.add(r["id"])
            if change_translated or change_refined:
                body_changed_ids.append(r["id"])
            # Capture the pre-substitution values for restore; only the columns
            # this run actually changes go in, which keeps the payload small.
            before: dict[str, str | None] = {}
            if change_translated:
                before["translated_before"] = translated
            if change_refined:
                before["refined_before"] = refined
            if change_title:
                before["title_before"] = title_en
            snapshot_payloads.setdefault(r["novel_id"], {})[str(r["id"])] = before
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
        # CAT Phase 3: status-preserving segment re-sync in the same transaction
        # (title-only changes never touch the displayed body, so they skip it).
        from backend.services import segments as segments_svc  # noqa: PLC0415
        for cid in body_changed_ids:
            await segments_svc.reproject_from_body(conn, cid)

        # One snapshot per touched novel, inside the same transaction as the
        # UPDATEs so a crash between them can't leave un-restorable changes. One
        # token across all novels keeps a multi-novel global apply one logical
        # undo group, matching commit_preview's per-commit token.
        from backend.services.fr_snapshots import record_snapshot  # noqa: PLC0415
        commit_token = f"glossary-{secrets.token_urlsafe(16)}"
        snapshot_ids: list[int] = []
        for touched_novel_id, payload in snapshot_payloads.items():
            sid = await record_snapshot(
                conn,
                novel_id=touched_novel_id,
                commit_token=commit_token,
                find_pattern=old_en,
                replace_pattern=new_en,
                target="both",
                scope="novel" if novel_id is not None else "all",
                chapters_changed=len(payload),
                payload=payload,
            )
            if sid is not None:
                snapshot_ids.append(sid)

        skipped_translating = await _count_translating_in_scope(conn, novel_id)
        await conn.commit()
    except Exception:
        # Exception (not BaseException) so signal-driven shutdown propagates
        # immediately without a cooperative rollback round-trip, matching
        # uploads.py::create_novel_and_chapters / append_with_offset.
        await conn.rollback()
        raise
    return CommitResult(
        chapters_updated=len(chapters_touched),
        rows_updated_translated=rows_translated,
        rows_updated_refined=rows_refined,
        rows_updated_titles=rows_titles,
        snapshot_ids=snapshot_ids,
        skipped_translating=skipped_translating,
        skipped_refining=skipped_refining,
        skipped_refining_chapter_ids=skipped_refining_ids,
    )
