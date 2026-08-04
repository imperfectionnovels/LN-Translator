"""Normalizer between the raw detect_* observer outputs and the
chapter_observations storage shape.

The QA dashboard (Initiative 1) persists deterministic observer hits as
discrete rows so the reader can render them per-chapter, with dismissal,
and so the library can show an aggregate badge.

Observer functions in `text_observers.py` return `list[str]` — for some
detectors each list item is one flagged span, for others it's one combined
advisory message covering several spans. The normalizer absorbs that
diversity into a uniform `NormalizedObservation` record. Paragraph indexing
is best-effort: v1 leaves it NULL for every observer (the reader sidebar
hides the jump affordance when it's NULL) and a future revision will
extend individual observers to expose match offsets.

The normalizer is intentionally NOT responsible for calling observers — the
queue worker does that and hands the raw outputs in. Keeping the call sites
in the worker keeps the existing chapter-level log line intact (single
source for the 'logged, not retried' telemetry) while this layer owns
storage shape.

Post-pivot gap audit (2026-08-04): `recheck_body_observations` adds the one
NON-translate-time writer, a pull-based re-run of the body-correctness
observers against the chapter's current displayed body so a finding the
user fixed in the CAT editor clears without a manual dismiss. Scoped
strictly to the kinds that recheck can itself recompute; everything the
queue alone can know stays untouched.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterable

import aiosqlite
from fastapi.concurrency import run_in_threadpool

from backend.services import segments as segments_svc
from backend.services.text_observers import body_correctness_observations

logger = logging.getLogger(__name__)


class ObservationRecheckBusyError(Exception):
    """Recheck refused because the chapter is mid-translation: the queue's
    success commit owns the observation rows then (full DELETE+INSERT), so a
    concurrent recheck would race it for no benefit. Routes map this to 409
    with the structured detail {message, error_kind: 'chapter_translating'}
    (the segments-route convention)."""


def parse_disabled_observers(raw: str | None) -> set[str]:
    """Parse a novel's `disabled_observers` JSON blob into a set of muted
    observation kinds.

    Fails open: a NULL / empty value, or a malformed blob, yields the empty
    set (no mutes) so a user can never accidentally suppress every observer
    by storing bad JSON. Keeping the parse in one shared helper means any
    future consumer of the column cannot drift from the queue worker's
    interpretation (recheck_body_observations below is the second consumer;
    the retired edit-paragraph re-run path was its predecessor)."""
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning("malformed disabled_observers JSON, treating as no mutes: %s", e)
        return set()
    if not isinstance(data, list):
        return set()
    return {kind for kind in data if isinstance(kind, str)}


@dataclass(frozen=True)
class NormalizedObservation:
    """One row destined for chapter_observations.

    `kind` is the observer name (or one of the two implicit kinds —
    'translation_degraded', 'glossary_merge_error') so the frontend can
    style per-kind. `paragraph_index` is None until an observer is
    extended to expose match offsets; the reader sidebar treats None as
    chapter-level.
    """
    kind: str
    severity: str  # 'warn' | 'info'
    paragraph_index: int | None
    excerpt: str


# F26 (2026-05-25): observation severity tiering. Semantic observers
# (missing locked glossary terms, predicate loss, implicit
# translation_degraded + glossary_merge_error) are signals of
# possible meaning loss; the user usually wants to act on them. Stylistic
# observers (MT-texture tics, double possessives, mid-sentence breaks,
# intensifier inflation) are surface-prose
# advisories — often false positives and lower priority. The library
# badge splits into '⚠ N semantic / ⓘ N stylistic' so high-priority
# issues don't get drowned in stylistic noise.
SEMANTIC_KINDS = frozenset({
    "missing_glossary_term",
    "missing_title_glossary_term",
    "glossary_predicate_loss",
    "translation_degraded",
    "glossary_merge_error",
    "tm_inconsistency",
})
STYLISTIC_KINDS = frozenset({
    "mt_texture",
    "double_possessive",
    "mid_sentence_paragraph_break",
    "intensifier_inflation",
    "paragraph_count_drift",
})


def severity_tier_for(kind: str) -> str:
    """Return 'semantic' or 'stylistic' for the given observation kind.
    Unknown kinds default to 'stylistic' (less alarming) — a new
    observer added without updating these tables surfaces as a quieter
    badge rather than spuriously alarming."""
    if kind in SEMANTIC_KINDS:
        return "semantic"
    return "stylistic"


# Map from the queue worker's observation-string prefix to a structured
# kind name. The worker builds strings like "missing locked glossary term
# '昂霄' → 'Soaring Firmament'" — we recover the kind from the prefix.
_KIND_PREFIXES: tuple[tuple[str, str], ...] = (
    ("missing locked glossary term", "missing_glossary_term"),
    ("missing title glossary term", "missing_title_glossary_term"),
    ("mt-texture tics:", "mt_texture"),
    ("Double possessive on a name:", "double_possessive"),
    ("Mid-sentence paragraph break", "mid_sentence_paragraph_break"),
    ("Intensifier inflation", "intensifier_inflation"),
    ("Predicate loss near", "glossary_predicate_loss"),
)


def _kind_for(message: str) -> str:
    """Match a raw observer-output string to a kind label.

    Falls back to 'observation' when no prefix matches — keeps storage
    well-defined even if a new observer is added without updating the
    prefix table."""
    for prefix, kind in _KIND_PREFIXES:
        if message.startswith(prefix):
            return kind
    return "observation"


def normalize_observer_outputs(
    raw_messages: Iterable[str],
) -> list[NormalizedObservation]:
    """Convert the queue worker's flat list of observation strings into
    storage-ready records. One string → one record.

    Severity is 'warn' for every detect_* output (the observers only fire
    on actionable issues; nothing they emit is purely informational).
    Paragraph index is None for v1 — a future revision can extend the
    individual observers to return match offsets and feed them through
    this layer."""
    out: list[NormalizedObservation] = []
    for msg in raw_messages:
        if not msg or not msg.strip():
            continue
        out.append(
            NormalizedObservation(
                kind=_kind_for(msg),
                severity="warn",
                paragraph_index=None,
                excerpt=msg.strip(),
            )
        )
    return out


def implicit_observation_translation_degraded() -> NormalizedObservation:
    """Synthesize the implicit 'translation_degraded' observation.

    The queue worker calls this when result.degraded is True so the panel
    can render the fallback-path warning uniformly with detect_* hits."""
    return NormalizedObservation(
        kind="translation_degraded",
        severity="warn",
        paragraph_index=None,
        excerpt=(
            "Translation came from the plain-text fallback path — the "
            "translator's structured envelope failed to parse twice and "
            "the body was salvaged as raw text. Re-translation may "
            "produce a cleaner result."
        ),
    )


def implicit_observation_tm_inconsistency(
    source_text: str,
    paragraph_index: int,
    renderings: list[str],
) -> NormalizedObservation:
    """Initiative 5 — synthesize a TM-inconsistency observation.

    Fired by the queue worker when this chapter's freshly-populated TM
    rows include a source paragraph whose previously-stored renderings
    don't all match this chapter's. The user can decide whether to
    standardize via Initiative 4's find/replace.

    The excerpt summarizes the renderings inline so the panel can render
    without a second fetch. Truncated when the rendering set is long;
    the TM concordance endpoint can pull the full rendering set when
    the user wants to drill in."""
    short_source = source_text.strip()
    if len(short_source) > 80:
        short_source = short_source[:77] + "…"
    # Show up to 3 distinct renderings inline. More than that is rare
    # enough that the truncation note ("+N more") doesn't hurt.
    distinct = []
    for r in renderings:
        if r not in distinct:
            distinct.append(r)
    shown = distinct[:3]
    more = len(distinct) - len(shown)
    rendered_list = "; ".join(f'"{r[:60]}{"…" if len(r) > 60 else ""}"' for r in shown)
    if more > 0:
        rendered_list += f" (+{more} more)"
    return NormalizedObservation(
        kind="tm_inconsistency",
        severity="warn",
        paragraph_index=paragraph_index,
        excerpt=(
            f"TM inconsistency: source paragraph \"{short_source}\" has "
            f"been translated {len(distinct)} different ways across this "
            f"novel — {rendered_list}. Open the concordance to compare."
        ),
    )


def implicit_observation_paragraph_count_drift(
    got: int, expected: int, stage: str,
) -> NormalizedObservation:
    """Synthesize the implicit 'paragraph_count_drift' observation (CAT
    Phase 1).

    Fired by the queue worker when the COMMITTED body's blank-line paragraph
    count diverges from the pre-joined source paragraph count. Two ways in:
    the model's count violation was accepted on the second miss, or a
    post-validation deterministic fixup (comma-break weld, leading-title
    strip) changed the count after validation passed. Observation only, no
    retry: it keeps the 1:1 violation rate measurable end to end, and marks
    chapters whose positional paragraph map Phase 2 must not trust.
    `stage` is 'translation' or 'refinement' so the two passes stay
    distinguishable in the panel."""
    return NormalizedObservation(
        kind="paragraph_count_drift",
        severity="info",
        paragraph_index=None,
        excerpt=(
            f"Paragraph count drift ({stage}): the committed body has {got} "
            f"blank-line-separated paragraphs but the source map expects "
            f"{expected}. Paragraph positions in this chapter cannot be "
            f"trusted as a 1:1 map."
        ),
    )


# Every kind text_observers.body_correctness_observations can produce through
# normalize_observer_outputs' prefix table, including the 'observation'
# fallback (residual-CJK / what-cleft / orphan-'Which' messages carry no
# prefix entry; nothing outside the body observers writes that kind). This is
# recheck_body_observations' scoped-DELETE ownership set. Kinds only the
# translate/refine commits can know (translation_degraded, tm_inconsistency,
# glossary_merge_error, paragraph_count_drift, missing_title_glossary_term)
# stay untouched. Keep this in lockstep with body_correctness_observations
# and _KIND_PREFIXES; test_observations_recheck.py pins the mapping.
BODY_RECHECK_KINDS = frozenset({
    "missing_glossary_term",
    "mt_texture",
    "double_possessive",
    "mid_sentence_paragraph_break",
    "intensifier_inflation",
    "glossary_predicate_loss",
    "observation",
})

# glossary_predicate_loss is the one kind shared between the body observers
# and the queue's title-targeted call (detect_glossary_predicate_loss with
# source_label='chapter title'); the label rides the excerpt as
# '... in <source_label>: ...', so title rows are carved out of the scoped
# DELETE by this LIKE pattern (pinned against the real message format).
_TITLE_PREDICATE_EXCERPT_LIKE = "% in chapter title: %"


async def recheck_body_observations(
    conn: aiosqlite.Connection,
    chapter_row,
    glossary,
) -> bool:
    """Pull-based QA refresh: re-run the deterministic body-correctness
    observers against the chapter's CURRENT displayed body and replace the
    persisted rows of exactly the kinds this recheck recomputes
    (BODY_RECHECK_KINDS; scoped DELETE + INSERT, the refinement-drift
    scoped-replace precedent in queue.py). A finding the user fixed in the
    CAT editor clears without a manual dismiss; translate-time-only kinds
    and title-targeted rows are untouched.

    `chapter_row` must carry id, novel_id, status, refinement_status,
    original_text, translated_text, refined_text (displayed_body reads the
    last two).
    Honors the novel's disabled_observers mutes. Dismissals carry over: a
    fresh finding identical on (kind, excerpt) to a previously stored
    dismissed row keeps its dismissed_at, so re-checking never resurrects a
    judgment call the user already waved off.

    Raises ObservationRecheckBusyError while the chapter is translating OR
    while a refinement is pending / in progress (the queue owns the rows
    then, and the refine commit is about to replace the displayed body:
    the same mid-transition window get_segments refuses to rebuild in; bug
    hunt 2026-08-04, B6). Returns False without touching anything when
    there is no displayed body to check (never translated, or blanked):
    observers against an empty body would spuriously flag every glossary
    term as missing. Returns True when the recheck ran. The caller owns the
    commit."""
    if chapter_row["status"] == "translating":
        raise ObservationRecheckBusyError(
            "this chapter is translating; its findings refresh when the "
            "translation finishes."
        )
    if (chapter_row["refinement_status"] or "none") in (
        "pending", "in_progress",
    ):
        raise ObservationRecheckBusyError(
            "this chapter is refining; its findings refresh when the "
            "refinement finishes."
        )
    chapter_id = chapter_row["id"]
    _variant, body = segments_svc.displayed_body(chapter_row)
    if not body.strip():
        return False

    # Pure CPU scan offloaded off the event loop (the diagnostics /
    # quality_dashboard precedent): a large glossary union over a full
    # chapter body is regex-heavy and would stall concurrent requests.
    fresh = normalize_observer_outputs(
        await run_in_threadpool(
            body_correctness_observations,
            chapter_row["original_text"] or "", body, glossary,
        )
    )
    cur = await conn.execute(
        "SELECT disabled_observers FROM novels WHERE id = ?",
        (chapter_row["novel_id"],),
    )
    mute_row = await cur.fetchone()
    muted = parse_disabled_observers(
        mute_row["disabled_observers"] if mute_row else None
    )
    if muted:
        fresh = [o for o in fresh if o.kind not in muted]

    # Union hardening: the static set clears fixed findings even when a run
    # produces none of a kind; unioning in the kinds THIS run produced makes
    # duplicate accumulation impossible for a future body observer that was
    # not added to the static set (its rows are replaced, not appended).
    kinds = sorted(BODY_RECHECK_KINDS | {o.kind for o in fresh})
    placeholders = ",".join("?" * len(kinds))
    cur = await conn.execute(
        f"SELECT kind, excerpt, dismissed_at FROM chapter_observations "
        f"WHERE chapter_id = ? AND kind IN ({placeholders}) "
        f"AND dismissed_at IS NOT NULL",
        (chapter_id, *kinds),
    )
    dismissed_at_of: dict[tuple[str, str], str] = {
        (r["kind"], r["excerpt"]): r["dismissed_at"]
        for r in await cur.fetchall()
    }
    await conn.execute(
        f"DELETE FROM chapter_observations "
        f"WHERE chapter_id = ? AND kind IN ({placeholders}) "
        f"AND NOT (kind = 'glossary_predicate_loss' AND excerpt LIKE ?)",
        (chapter_id, *kinds, _TITLE_PREDICATE_EXCERPT_LIKE),
    )
    if fresh:
        await conn.executemany(
            "INSERT INTO chapter_observations "
            "(chapter_id, kind, severity, paragraph_index, excerpt, "
            " dismissed_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    chapter_id, obs.kind, obs.severity, obs.paragraph_index,
                    obs.excerpt,
                    dismissed_at_of.get((obs.kind, obs.excerpt)),
                )
                for obs in fresh
            ],
        )
    return True


def implicit_observation_glossary_merge_error(error_msg: str) -> NormalizedObservation:
    """Synthesize the implicit 'glossary_merge_error' observation.

    Written by the queue worker after the post-translation glossary merge
    raises — the translation itself is committed, but new-term auto-merge
    did not run, so the chapter may be missing the glossary entries the
    translator produced."""
    return NormalizedObservation(
        kind="glossary_merge_error",
        severity="warn",
        paragraph_index=None,
        excerpt=(
            f"Glossary auto-merge failed after the chapter committed: "
            f"{error_msg.strip() or 'unknown error'}. The translation is "
            f"safe; new glossary terms from this chapter were not added."
        ),
    )
