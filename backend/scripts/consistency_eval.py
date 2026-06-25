"""Consistency eval harness (Phase 0, read-only).

Computes baseline cross-chapter consistency metrics for a novel from
already-stored data, with NO LLM calls and NO DB writes. This is the
instrument the CAT-pipeline phases are graduated against; the binding rule is
no default flip without a single-variable A/B vs a ground-truth chapter, so a
trustworthy baseline number has to exist first.

Metrics
-------
- Term-Consistency Rate (TCR): per locked glossary term, the fraction of done
  chapters whose source contains it where the expected English rendering is
  present. Reuses the production presence logic (canonical_zh,
  _source_has_checkable_term, _check_variants_for_entry, english_term_present,
  is_atomic_case_locked_term) so the number matches what the translator
  pipeline would count as a hit. Reported overall and per category, with the
  worst-scoring terms listed so they can be sent to glossary hygiene.

- Segment-reuse hit rate: simulates the CAT pipeline's deterministic reuse
  over the corpus. For each chapter in reading order, what fraction of its
  source paragraphs already appeared in an earlier chapter (recurrence) and,
  of those, how many also have a stored target in tm_segments and could be
  served from memory with no LLM call (reusable). The gap between the two is
  the alignment loss (a paragraph that recurs but was never cleanly aligned
  has no reusable target). Reported count- and char-weighted, all segments vs
  substantive (>= _SUBSTANTIVE_CHARS) only, since reusing a one-line sound
  effect is not the same win as reusing a real paragraph.

- Bracketed-block identity rate: among recurring '[...]' system-panel source
  paragraphs, the fraction rendered identically every time.

- LLM-coverage: the char-weighted fraction of a chapter that would still need
  the LLM after deterministic reuse (1 - reusable char rate).

Also ships the A/B significance helpers (McNemar, bootstrap CI) the later
phases use at their graduation gate.

The output is a human-readable report (stdout) plus a baseline JSON (--out,
defaults under the data root). The substantive segment-reuse hit rate is the
go/no-go signal for Phase 3 (translate-only-new-segments): near zero means
that lever is not worth building for this corpus.

Database selection follows the app (see backend.scripts._db_banner): the dev
data/novels.db by default; set LN_TRANSLATOR_DATA to target the live store.
Best run with the app closed so the read does not contend with the writer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
from dataclasses import dataclass

from backend.config import USER_DATA_ROOT
from backend.db import open_conn
from backend.models import GlossaryEntry
from backend.scripts._db_banner import print_db_banner
from backend.services import glossary as glossary_svc
from backend.services import tm as tm_svc
from backend.services.glossary import (
    _check_variants_for_entry,
    _source_has_checkable_term,
    canonical_zh,
    english_term_present,
    is_atomic_case_locked_term,
    split_aliases,
)

# A source paragraph this long (in characters) or more is "substantive": a
# real paragraph rather than a sound effect, interjection, or one-line beat.
# Reuse of substantive paragraphs is the win that matters; short-line reuse
# mostly flattens correct variation.
_SUBSTANTIVE_CHARS = 30


# ---------------------------------------------------------------------------
# Pure metric helpers (no DB; unit-tested directly)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    source_hash: str
    length: int
    bracketed: bool
    substantive: bool


def segment_source(source_text: str) -> list[Segment]:
    """Segment a chapter source the same way the TM keys it.

    Splits on blank lines and drops a leading Chinese heading (mirrors
    tm.replace_chapter_segments' source side) so a computed source_hash
    matches the stored tm_segments.source_hash for the same paragraph.
    """
    paras = tm_svc._drop_leading_heading(tm_svc._split_paragraphs(source_text))
    out: list[Segment] = []
    for p in paras:
        out.append(
            Segment(
                source_hash=tm_svc._hash_source(p),
                length=len(p),
                bracketed=("【" in p),  # CJK left black lenticular bracket
                substantive=len(p) >= _SUBSTANTIVE_CHARS,
            )
        )
    return out


def segment_reuse_stats(
    chapters_in_order: list[tuple[int, list[Segment]]],
    stored_first_chapter: dict[str, int],
) -> dict:
    """Simulate deterministic segment reuse over the corpus.

    chapters_in_order: (chapter_num, segments) sorted by reading order.
    stored_first_chapter: source_hash -> earliest chapter_num that has a
      stored target in tm_segments (i.e. a reusable rendering exists).

    A segment in chapter N is:
      - recurring  if its hash appeared in an earlier chapter's source.
      - reusable   if recurring AND a stored target exists from a chapter
                   strictly before N (stored_first_chapter[hash] < N).
    Returns count- and char-weighted rates over all segments and over the
    substantive subset, plus llm_coverage (1 - reusable char rate).
    """
    seen_before: set[str] = set()
    agg = {
        scope: {
            "total": 0,
            "total_chars": 0,
            "recurring": 0,
            "recurring_chars": 0,
            "reusable": 0,
            "reusable_chars": 0,
        }
        for scope in ("all", "substantive")
    }

    for num, segs in chapters_in_order:
        for seg in segs:
            recurring = seg.source_hash in seen_before
            first = stored_first_chapter.get(seg.source_hash)
            reusable = recurring and first is not None and first < num
            scopes = ["all"] + (["substantive"] if seg.substantive else [])
            for scope in scopes:
                a = agg[scope]
                a["total"] += 1
                a["total_chars"] += seg.length
                if recurring:
                    a["recurring"] += 1
                    a["recurring_chars"] += seg.length
                if reusable:
                    a["reusable"] += 1
                    a["reusable_chars"] += seg.length
        # Only after scoring the chapter does its own text become "seen".
        for seg in segs:
            seen_before.add(seg.source_hash)

    def _rates(a: dict) -> dict:
        total = a["total"] or 1
        total_chars = a["total_chars"] or 1
        return {
            "segments": a["total"],
            "recurrence_rate": a["recurring"] / total,
            "reuse_rate": a["reusable"] / total,
            "reuse_rate_chars": a["reusable_chars"] / total_chars,
            "llm_coverage_chars": 1.0 - (a["reusable_chars"] / total_chars),
        }

    return {scope: _rates(agg[scope]) for scope in ("all", "substantive")}


def mcnemar(b: int, c: int) -> tuple[float, float]:
    """Paired binary significance test on the two discordant counts.

    b = pairs where arm A is correct and arm B is wrong; c = the reverse.
    Concordant pairs carry no information and are excluded. Returns
    (statistic, p_value). Uses the exact two-sided binomial for small
    discordant totals and the continuity-corrected chi-square otherwise.
    This is the right test for "did the A/B change flip per-paragraph
    consistency outcomes", which is what the graduation gate measures.
    """
    n = b + c
    if n == 0:
        return 0.0, 1.0
    if n < 25:
        k = min(b, c)
        tail = sum(math.comb(n, i) for i in range(k + 1)) * (0.5**n)
        p = min(1.0, 2.0 * tail)
        # Report the continuity-corrected chi-square alongside for context.
        stat = (abs(b - c) - 1) ** 2 / n if n > 0 else 0.0
        return float(stat), float(p)
    stat = (abs(b - c) - 1) ** 2 / n
    # Survival of chi-square with 1 dof: erfc(sqrt(stat/2)).
    p = math.erfc(math.sqrt(stat / 2.0))
    return float(stat), float(p)


def bootstrap_ci(
    values: list[float],
    *,
    n_resamples: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile bootstrap confidence interval for the mean of `values`.

    Deterministic given `seed` so a report is reproducible. Used for the
    continuous metrics (reuse rates, HHI) where a paired test does not apply.
    """
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(n_resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int((alpha / 2) * n_resamples)]
    hi = means[min(n_resamples - 1, int((1 - alpha / 2) * n_resamples))]
    return (lo, hi)


def tcr_for_glossary(
    glossary: list[GlossaryEntry],
    chapters: list[tuple[int, str, str]],
) -> dict:
    """Term-Consistency Rate over locked glossary terms.

    chapters: (chapter_num, source_text, translated_text), done chapters only.
    A (locked term, chapter) pair is "checkable" when the term has a real
    (not substring-false) source occurrence in that chapter; "consistent"
    when an expected English variant is present per the production casing
    rule. Returns overall and per-category rates plus the worst terms.
    """
    locked = [g for g in glossary if g.locked]
    locked_alias_canons = [
        canonical_zh(zh)
        for g in locked
        for zh, _ in split_aliases(g.term_zh or "", g.term_en or "")
        if zh and len(zh) >= 2
    ]

    by_cat: dict[str, list[int]] = {}  # cat -> [checkable, consistent]
    per_term: dict[int, list] = {}  # entry id -> [zh, en, checkable, consistent]
    checkable_total = 0
    consistent_total = 0

    for _num, src, tgt in chapters:
        if not src or not tgt:
            continue
        src_canon = canonical_zh(src)
        tgt_lower = tgt.lower()
        for g in locked:
            atomic = is_atomic_case_locked_term(g)
            seen_en: set[str] = set()
            for zh, en in split_aliases(g.term_zh or "", g.term_en or ""):
                if not zh or not en or len(zh) < 2:
                    continue
                cz = canonical_zh(zh)
                if not cz or en in seen_en:
                    continue
                # Fast reject before the alias-aware checkable scan.
                if cz not in src_canon:
                    continue
                if not _source_has_checkable_term(
                    src_canon, cz, locked_alias_canons
                ):
                    continue
                seen_en.add(en)
                variants = _check_variants_for_entry(g, en)
                if not variants:
                    continue
                if atomic:
                    ok = any(english_term_present(v, tgt) for v in variants)
                else:
                    ok = any(v.lower() in tgt_lower for v in variants)

                checkable_total += 1
                cat = g.category or "other"
                slot = by_cat.setdefault(cat, [0, 0])
                slot[0] += 1
                rec = per_term.setdefault(
                    g.id, [g.term_zh, g.term_en, 0, 0]
                )
                rec[2] += 1
                if ok:
                    consistent_total += 1
                    slot[1] += 1
                    rec[3] += 1

    def _rate(consistent: int, checkable: int) -> float:
        return consistent / checkable if checkable else 1.0

    categories = {
        cat: {
            "checkable": v[0],
            "consistent": v[1],
            "tcr": _rate(v[1], v[0]),
        }
        for cat, v in sorted(by_cat.items())
    }
    # Worst offenders: terms checked at least 3 times with the lowest TCR.
    # The glossary entry id rides along so a UI can deep-link straight to the
    # term's editor for triage (per_term is keyed by g.id).
    worst = sorted(
        (
            {
                "id": tid,
                "term_zh": zh,
                "term_en": en,
                "checkable": chk,
                "consistent": con,
                "tcr": _rate(con, chk),
            }
            for tid, (zh, en, chk, con) in per_term.items()
            if chk >= 3
        ),
        key=lambda d: (d["tcr"], -d["checkable"]),
    )
    return {
        "overall_tcr": _rate(consistent_total, checkable_total),
        "checkable": checkable_total,
        "consistent": consistent_total,
        "by_category": categories,
        "worst_terms": worst[:25],
    }


def bracketed_identity_rate(
    tm_rows: list[tuple[str, str, str]],
) -> dict:
    """Identity rate among recurring system-panel ('[...]') source paragraphs.

    tm_rows: (source_hash, source_text, target_text). A group is the set of
    rows sharing a source_hash whose source contains a CJK lenticular bracket
    and which occurs more than once. Identical = exactly one distinct target.
    """
    groups: dict[str, dict] = {}
    for source_hash, source_text, target_text in tm_rows:
        if "【" not in (source_text or ""):
            continue
        g = groups.setdefault(source_hash, {"targets": set(), "count": 0})
        g["targets"].add(target_text)
        g["count"] += 1
    recurring = [g for g in groups.values() if g["count"] > 1]
    identical = [g for g in recurring if len(g["targets"]) == 1]
    rate = len(identical) / len(recurring) if recurring else 1.0
    return {
        "recurring_blocks": len(recurring),
        "identical_blocks": len(identical),
        "identity_rate": rate,
    }


# ---------------------------------------------------------------------------
# DB loaders + report assembly
# ---------------------------------------------------------------------------


async def _load(novel_id: int) -> dict:
    async with open_conn() as conn:
        glossary = await glossary_svc.list_for_novel(conn, novel_id)

        cur = await conn.execute(
            "SELECT chapter_num, original_text, translated_text "
            "FROM chapters WHERE novel_id = ? AND status = 'done' "
            "AND translated_text IS NOT NULL ORDER BY chapter_num",
            (novel_id,),
        )
        chapters = [
            (r["chapter_num"], r["original_text"] or "", r["translated_text"] or "")
            for r in await cur.fetchall()
        ]

        cur = await conn.execute(
            "SELECT t.source_hash, t.source_text, t.target_text, c.chapter_num "
            "FROM tm_segments t JOIN chapters c ON c.id = t.chapter_id "
            "WHERE t.novel_id = ? ORDER BY c.chapter_num, t.paragraph_index",
            (novel_id,),
        )
        tm_rows = await cur.fetchall()

    stored_first: dict[str, int] = {}
    tm_triples: list[tuple[str, str, str]] = []
    for r in tm_rows:
        h = r["source_hash"]
        num = r["chapter_num"]
        if h not in stored_first or num < stored_first[h]:
            stored_first[h] = num
        tm_triples.append((h, r["source_text"], r["target_text"]))

    chapters_in_order = [
        (num, segment_source(src)) for (num, src, _tgt) in chapters
    ]

    return {
        "glossary": glossary,
        "chapters": chapters,
        "chapters_in_order": chapters_in_order,
        "stored_first": stored_first,
        "tm_triples": tm_triples,
    }


def _build_report(novel_id: int, data: dict) -> dict:
    tcr = tcr_for_glossary(data["glossary"], data["chapters"])
    reuse = segment_reuse_stats(data["chapters_in_order"], data["stored_first"])
    brackets = bracketed_identity_rate(data["tm_triples"])
    return {
        "novel_id": novel_id,
        "chapters_done": len(data["chapters"]),
        "glossary_terms": len(data["glossary"]),
        "glossary_locked": sum(1 for g in data["glossary"] if g.locked),
        "tcr": tcr,
        "segment_reuse": reuse,
        "bracketed_blocks": brackets,
    }


async def compute_consistency(novel_id: int) -> dict:
    """Public callable core: the full consistency report for a novel.

    Same dict `_build_report` returns (TCR overall + by-category + worst_terms,
    segment reuse, bracketed-panel identity). Importable by the app's quality
    service and by tests; the CLI (`main`) keeps its own load/print path so it
    is unaffected. Opens its own read connection (a full-novel scan belongs on a
    fresh connection, not the request connection).
    """
    return _build_report(novel_id, await _load(novel_id))


def _print_report(report: dict) -> None:
    pct = lambda x: f"{100 * x:.1f}%"  # noqa: E731
    print("=" * 64)
    print(f"  Consistency baseline: novel {report['novel_id']}")
    print(
        f"  chapters done: {report['chapters_done']}   "
        f"glossary: {report['glossary_terms']} "
        f"({report['glossary_locked']} locked)"
    )
    print("=" * 64)

    tcr = report["tcr"]
    print(
        f"\nTerm-Consistency Rate (TCR): {pct(tcr['overall_tcr'])}  "
        f"({tcr['consistent']}/{tcr['checkable']} checkable pairs)"
    )
    for cat, v in tcr["by_category"].items():
        print(
            f"    {cat:<12} {pct(v['tcr']):>7}  "
            f"({v['consistent']}/{v['checkable']})"
        )
    if tcr["worst_terms"]:
        print("\n  Worst terms (checked >= 3x):")
        for t in tcr["worst_terms"][:10]:
            print(
                f"    {pct(t['tcr']):>7}  {t['term_zh']} -> {t['term_en']}  "
                f"({t['consistent']}/{t['checkable']})"
            )

    reuse = report["segment_reuse"]
    print("\nSegment-reuse simulation (deterministic CAT reuse):")
    for scope in ("all", "substantive"):
        r = reuse[scope]
        print(
            f"    {scope:<12} segments={r['segments']:>7}  "
            f"recurrence={pct(r['recurrence_rate'])}  "
            f"reuse={pct(r['reuse_rate'])}  "
            f"reuse(chars)={pct(r['reuse_rate_chars'])}  "
            f"llm-coverage(chars)={pct(r['llm_coverage_chars'])}"
        )

    b = report["bracketed_blocks"]
    print(
        f"\nBracketed-block identity: {pct(b['identity_rate'])}  "
        f"({b['identical_blocks']}/{b['recurring_blocks']} recurring blocks)"
    )
    print(
        "\nGo/no-go for Phase 3 (translate-only-new-segments): the substantive "
        "reuse(chars) rate above. Near zero means the LLM-reduction lever is "
        "not worth building for this corpus; the win is the termbase."
    )


async def _run(novel_id: int, out_path: str | None) -> None:
    # The report prints CJK term names; a cp1252 Windows console would crash
    # on them. Force UTF-8 with replacement so the run never dies on output.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    print_db_banner(mutates=False)
    data = await _load(novel_id)
    report = _build_report(novel_id, data)
    _print_report(report)

    if out_path is None:
        out_path = str(USER_DATA_ROOT / f"consistency_baseline_novel{novel_id}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"\nbaseline written: {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Read-only consistency baseline for a novel."
    )
    ap.add_argument("--novel", type=int, required=True, help="novel id")
    ap.add_argument(
        "--out",
        default=None,
        help="baseline JSON path (default: <data root>/consistency_baseline_novelN.json)",
    )
    args = ap.parse_args()
    asyncio.run(_run(args.novel, args.out))


if __name__ == "__main__":
    main()
