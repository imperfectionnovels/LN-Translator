"""Repeatable translation-quality scorecard for a novel (read-only).

The recurring pain this removes: every prompt/pipeline change in this project's
history was judged by a manual, noisy, single-chapter A/B and sometimes reverted.
This is the meta-lever the user asked for -- it makes quality MEASURED instead of
vibes-based, by orchestrating tools that already exist and harvesting signals the
app already collects and currently throws away:

  - per-chapter rule-category compliance via backend.scripts.quality_metrics
    (the ported ww_metrics scorers);
  - the chapter_observations rows the queue writes on EVERY translate and never
    aggregates -- counted per observer kind across the range (zero re-translation);
  - novel-level consistency (TCR / segment reuse / bracket identity) reused from
    backend.scripts.consistency_eval;
  - grouping by chapters.prompt_config_snapshot so each prompt-config "arm"
    (template version + provider/model + refiner) gets its own aggregated column.
    That turns A/B into a QUERY over chapters already translated under different
    configs -- no quota burn, no single-chapter noise.

Two modes:

  Report:  python -m backend.scripts.quality_report --novel 2 [--chapters 800-880]
           Scores the range, prints a per-category matrix + per-config arms +
           observation hit-rates + consistency summary, and writes a JSON
           scorecard under <data root>/quality_reports/.

  Diff:    python -m backend.scripts.quality_report --diff A.json B.json
           Per-category delta between two saved scorecards, with a bootstrap CI
           on each arm's per-chapter violation rate. The mechanical form of the
           graduation gate ("did the change actually move the number, and is the
           move outside noise"), across many chapters instead of one.

Database selection follows the app (backend.scripts._db_banner): dev
data/novels.db by default; set LN_TRANSLATOR_DATA to target the live store. Run
with the app closed so the read does not contend with the writer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from backend.config import USER_DATA_ROOT
from backend.db import open_conn
from backend.scripts._db_banner import print_db_banner
from backend.scripts.consistency_eval import (
    _build_report as _consistency_report,
)
from backend.scripts.consistency_eval import (
    _load as _consistency_load,
)
from backend.scripts.consistency_eval import (
    bootstrap_ci,
)
from backend.scripts.quality_metrics import score_text
from backend.services import glossary as glossary_svc

_CATEGORY_ORDER = [
    "glossary_observers", "glossary_presence", "glossary_casing",
    "epithet_frequency", "thought_format", "sentence_shape",
    "punctuation_carry", "banned_words", "costume_constructions",
    "stock_phrases", "envelope_format", "unit_conversion",
]
_SURFACE_HEADLINE = [
    "semicolons_per_1k", "contractions_per_1k", "archaic_tells",
    "mean_words_per_sentence", "en_zh_sentence_ratio",
]
_FLOW_HEADLINE = [
    "anchor_rate", "given_link_rate", "max_same_opener_run",
    "opening_bigram_variety", "sentence_len_cv",
]


def _config_tag(snapshot_json: str | None) -> str:
    """Readable A/B arm label from a chapter's prompt_config_snapshot."""
    try:
        d = json.loads(snapshot_json) if snapshot_json else {}
        if not isinstance(d, dict):
            d = {}
    except (TypeError, ValueError):
        d = {}
    if not d:
        return "(no snapshot)"
    tag = (
        f"{d.get('prompt_template_version', '?')} | "
        f"{d.get('translator_provider_type', '?')}:"
        f"{d.get('translator_model_id', '?')}"
    )
    if d.get("refiner_model_id"):
        tag += f" +refine:{d['refiner_model_id']}"
    return tag


def _aggregate_categories(scored: list[tuple[int, dict]]) -> dict:
    """Sum category violations/reviews/opportunities across chapters and keep
    per-chapter violation rates (for the diff-mode bootstrap CI)."""
    agg: dict[str, dict] = {}
    for _num, score in scored:
        for cat in score["categories"]:
            name = cat["name"]
            a = agg.setdefault(name, {
                "violations": 0, "reviews": 0, "opportunities": 0,
                "per_chapter_rates": [], "examples": [],
            })
            a["violations"] += cat["violations"]
            a["reviews"] += cat["reviews"]
            a["opportunities"] += cat["opportunities"]
            opp = cat["opportunities"] or 1
            a["per_chapter_rates"].append(cat["violations"] / opp)
            for ex in cat["examples"]:
                if len(a["examples"]) < 6:
                    a["examples"].append(ex)
    for a in agg.values():
        a["rate"] = a["violations"] / (a["opportunities"] or 1)
    return agg


def _mean_block(scored: list[tuple[int, dict]], section: str, keys: list[str]) -> dict:
    out: dict[str, float] = {}
    for key in keys:
        vals = [s[section].get(key) for _n, s in scored if s[section].get(key) is not None]
        if vals:
            out[key] = sum(vals) / len(vals)
    return out


async def _load_range(novel_id: int, lo: int, hi: int) -> dict:
    async with open_conn() as conn:
        glossary = await glossary_svc.list_for_novel(conn, novel_id)
        cur = await conn.execute(
            "SELECT chapter_num, original_text, translated_text, prompt_config_snapshot "
            "FROM chapters WHERE novel_id = ? AND status = 'done' "
            "AND translated_text IS NOT NULL AND chapter_num BETWEEN ? AND ? "
            "ORDER BY chapter_num",
            (novel_id, lo, hi),
        )
        chapters = [
            (r["chapter_num"], r["original_text"] or "", r["translated_text"] or "",
             r["prompt_config_snapshot"])
            for r in await cur.fetchall()
        ]
        # Harvest the discarded observer signal: every translate writes these and
        # nothing ever aggregates them.
        cur = await conn.execute(
            "SELECT o.kind AS kind, COUNT(*) AS n, "
            "COUNT(DISTINCT o.chapter_id) AS chapters "
            "FROM chapter_observations o JOIN chapters c ON c.id = o.chapter_id "
            "WHERE c.novel_id = ? AND c.status = 'done' "
            "AND c.chapter_num BETWEEN ? AND ? "
            "GROUP BY o.kind ORDER BY n DESC",
            (novel_id, lo, hi),
        )
        observations = {
            r["kind"]: {"count": r["n"], "chapters": r["chapters"]}
            for r in await cur.fetchall()
        }
    return {"glossary": glossary, "chapters": chapters, "observations": observations}


def _build_scorecard(novel_id: int, lo: int, hi: int, data: dict, consistency: dict) -> dict:
    glossary = data["glossary"]
    scored: list[tuple[int, dict]] = []
    by_config: dict[str, list[tuple[int, dict]]] = {}
    for num, source, target, snapshot in data["chapters"]:
        score = score_text(target, source, glossary)
        scored.append((num, score))
        by_config.setdefault(_config_tag(snapshot), []).append((num, score))

    return {
        "novel_id": novel_id,
        "chapter_range": [lo, hi],
        "chapters_scored": len(scored),
        "chapter_nums": [n for n, _ in scored],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "categories": _aggregate_categories(scored),
        "by_config": {
            tag: {
                "chapters": [n for n, _ in chs],
                "categories": _aggregate_categories(chs),
            }
            for tag, chs in by_config.items()
        },
        "observations": data["observations"],
        "surface_mean": _mean_block(scored, "surface", _SURFACE_HEADLINE),
        "flow_mean": _mean_block(scored, "flow", _FLOW_HEADLINE),
        "consistency": {
            "overall_tcr": consistency["tcr"]["overall_tcr"],
            "tcr_checkable": consistency["tcr"]["checkable"],
            "segment_reuse_substantive_chars": consistency["segment_reuse"]["substantive"]["reuse_rate_chars"],
            "bracket_identity_rate": consistency["bracketed_blocks"]["identity_rate"],
        },
    }


def _print_matrix(title: str, categories: dict) -> None:
    print(f"\n{title}")
    print(f"  {'category':<22} {'viol':>5} {'rev':>5} {'opp':>6} {'rate':>7}")
    for name in _CATEGORY_ORDER:
        c = categories.get(name)
        if not c:
            continue
        print(
            f"  {name:<22} {c['violations']:>5} {c['reviews']:>5} "
            f"{c['opportunities']:>6} {100 * c['rate']:>6.1f}%"
        )


def _print_scorecard(card: dict) -> None:
    print("=" * 70)
    print(f"  Quality scorecard: novel {card['novel_id']}  "
          f"chapters {card['chapter_range'][0]}-{card['chapter_range'][1]}  "
          f"({card['chapters_scored']} scored)")
    print("=" * 70)
    _print_matrix("Rule-category compliance (aggregate):", card["categories"])

    if len(card["by_config"]) > 1:
        print("\nBy prompt-config arm (A/B over the back catalog):")
        for tag, blk in card["by_config"].items():
            chs = blk["chapters"]
            span = f"{min(chs)}-{max(chs)}" if chs else "-"
            print(f"\n  [{tag}]  {len(chs)} ch ({span})")
            _print_matrix("   ", blk["categories"])
    else:
        only = next(iter(card["by_config"]), "(none)")
        print(f"\n(single prompt-config arm: {only})")

    print("\nObserver hit-rates (harvested from chapter_observations):")
    if card["observations"]:
        for kind, v in card["observations"].items():
            print(f"  {kind:<34} {v['count']:>5} hits over {v['chapters']} ch")
    else:
        print("  (none recorded)")

    print("\nSurface / flow means:")
    for k, v in {**card["surface_mean"], **card["flow_mean"]}.items():
        print(f"  {k:<26} {v:.3f}")

    co = card["consistency"]
    print("\nConsistency (novel-level):")
    print(f"  TCR overall              {100 * co['overall_tcr']:.1f}% "
          f"({co['tcr_checkable']} checkable)")
    print(f"  substantive reuse(chars) {100 * co['segment_reuse_substantive_chars']:.1f}%")
    print(f"  bracket identity         {100 * co['bracket_identity_rate']:.1f}%")


async def _run_report(novel_id: int, lo: int, hi: int, out_path: str | None) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    print_db_banner(mutates=False)
    data = await _load_range(novel_id, lo, hi)
    if not data["chapters"]:
        raise SystemExit(f"no done chapters in novel {novel_id} range {lo}-{hi}")
    consistency = _consistency_report(novel_id, await _consistency_load(novel_id))
    card = _build_scorecard(novel_id, lo, hi, data, consistency)
    _print_scorecard(card)

    if out_path is None:
        out_dir = USER_DATA_ROOT / "quality_reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = str(out_dir / f"novel{novel_id}-{lo}-{hi}-{stamp}.json")
    Path(out_path).write_text(
        json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nscorecard written: {out_path}")


def _diff(baseline_path: str, against_path: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    base = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    against = json.loads(Path(against_path).read_text(encoding="utf-8"))
    print("=" * 78)
    print("  Quality A/B diff (per-category violation rate; lower is better)")
    print(f"  baseline: novel {base['novel_id']} ch {base['chapter_range']} "
          f"({base['chapters_scored']} ch)")
    print(f"  against : novel {against['novel_id']} ch {against['chapter_range']} "
          f"({against['chapters_scored']} ch)")
    print("=" * 78)
    print(f"  {'category':<22} {'base':>8} {'against':>9} {'delta':>8}   95% CIs (base / against)")
    for name in _CATEGORY_ORDER:
        b = base["categories"].get(name)
        a = against["categories"].get(name)
        if not b or not a:
            continue
        b_lo, b_hi = bootstrap_ci(b.get("per_chapter_rates", []))
        a_lo, a_hi = bootstrap_ci(a.get("per_chapter_rates", []))
        delta = a["rate"] - b["rate"]
        arrow = "better" if delta < 0 else ("worse" if delta > 0 else "flat")
        print(
            f"  {name:<22} {100 * b['rate']:>7.1f}% {100 * a['rate']:>8.1f}% "
            f"{100 * delta:>+7.1f}%  [{100 * b_lo:.1f},{100 * b_hi:.1f}]/"
            f"[{100 * a_lo:.1f},{100 * a_hi:.1f}] {arrow}"
        )
    print(
        "\nRead a delta as real when the arms' CIs separate. Same-chapter arms "
        "(retranslated) support a stronger paired test; cross-chapter arms are a "
        "rate comparison."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Translation-quality scorecard / A/B diff.")
    ap.add_argument("--novel", type=int, help="novel id (report mode)")
    ap.add_argument("--chapters", help="range LO-HI (default: all done chapters)")
    ap.add_argument("--out", default=None, help="scorecard JSON path")
    ap.add_argument("--diff", nargs=2, metavar=("BASELINE", "AGAINST"),
                    help="diff two saved scorecards instead of building one")
    args = ap.parse_args()

    if args.diff:
        _diff(args.diff[0], args.diff[1])
        return
    if args.novel is None:
        ap.error("--novel is required in report mode")
    lo, hi = 1, 10**9
    if args.chapters:
        try:
            lo_s, hi_s = args.chapters.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
        except ValueError:
            ap.error("--chapters must be LO-HI, e.g. 800-880")
    asyncio.run(_run_report(args.novel, lo, hi, args.out))


if __name__ == "__main__":
    main()
