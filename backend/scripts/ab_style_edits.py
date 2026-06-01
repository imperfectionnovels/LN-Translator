"""A/B harness: does the style-edits prompt block move drafts toward the
user's own edits?

Phase 0 of the edit-mode style-feedback plan. The "teach future translation
your style" claim has never been measured against ground truth. This is a
single-variable A/B on the USER STYLE PREFERENCES block, scored against the
user's OWN held-out edits, which are simultaneously the style signal and the
target.

Run:
    python -m backend.scripts.ab_style_edits --novel-id N --chapter C

It re-translates held-out chapter C of novel N twice against the novel's
configured provider:
  - arm A: the novel's captured style edits injected, with the held-out
    chapter's OWN edits excluded so the answer key never leaks into the
    prompt.
  - arm B: no style edits at all (parity with PROMPT_INCLUDE_STYLE_EDITS off).
Every other prompt input (glossary, previous-chapter tail, style note, free
draft, genre, provider) is gathered exactly as the queue worker does and held
identical across both arms, so style edits are the only variable.

Scoring: for each paragraph the user edited in chapter C, find the arm's
draft paragraph that best matches the ORIGINAL draft (before_text), then
measure how close that paragraph is to the user's EDIT (after_text) via
difflib ratio. arm A beating arm B means the style edits pulled the fresh
draft toward what the user actually wanted.

Caveats:
  - Burns the provider's quota: two full-chapter translations, real backend,
    needs internet for API/CLI backends. Not a CI test.
  - If the novel uses a refiner, the held-out edits may have been made against
    refined text while this harness compares against fresh DRAFTS. That lowers
    the absolute ratios for both arms equally; the arm-A-minus-arm-B DELTA
    stays a clean read of the style-edit effect on the draft.
  - This deliberately bypasses fetch_style_edits / the global flag and feeds
    style_edits directly, because fetch_style_edits has no held-out-chapter
    exclusion and would leak the ground truth.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
from datetime import datetime

from backend.config import PROJECT_ROOT, PROMPT_INCLUDE_FREE_DRAFT
from backend.db import open_conn
from backend.services import global_glossary as global_glossary_svc
from backend.services.prompt_inputs import (
    STYLE_EDIT_LIMIT,
    fetch_novel_genre_brief,
    fetch_previous_chapter_tail,
    fetch_style_note,
    resolve_translator_provider,
)
from backend.services.translators import translate_chapter


def _dedupe_pairs(rows) -> list[tuple[str, str]]:
    """Mirror fetch_style_edits' within-window dedup of (before, after)."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for r in rows:
        pair = (r["before_text"], r["after_text"])
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out


async def _training_edits(
    conn, novel_id: int, exclude_chapter_id: int, limit: int
) -> list[tuple[str, str]]:
    """The style edits arm A injects: same recency + dedup as the worker's
    fetch_style_edits, but with the held-out chapter's own edits removed so
    the ground truth can't leak into the prompt."""
    cur = await conn.execute(
        "SELECT before_text, after_text FROM style_edits "
        "WHERE novel_id = ? AND (chapter_id IS NULL OR chapter_id != ?) "
        "ORDER BY id DESC LIMIT ?",
        (novel_id, exclude_chapter_id, limit),
    )
    return _dedupe_pairs(await cur.fetchall())


async def _ground_truth(conn, chapter_id: int) -> list[tuple[str, str]]:
    """The held-out chapter's edits: (original draft, user edit) pairs."""
    cur = await conn.execute(
        "SELECT before_text, after_text FROM style_edits "
        "WHERE chapter_id = ? ORDER BY id",
        (chapter_id,),
    )
    return _dedupe_pairs(await cur.fetchall())


def _score(
    paragraphs: list[str], before_text: str, after_text: str
) -> tuple[float, str]:
    """Closeness of an arm's draft to the user's edit for one edited paragraph.

    Aligns by content: pick the draft paragraph most similar to the ORIGINAL
    draft (before_text), since both arms render the same source. Then return
    its similarity to the user's EDIT (after_text). Robust to paragraph-count
    drift between the stored draft and a fresh re-translation."""
    if not paragraphs:
        return 0.0, ""
    best = max(
        paragraphs,
        key=lambda p: difflib.SequenceMatcher(None, p, before_text).ratio(),
    )
    return difflib.SequenceMatcher(None, best, after_text).ratio(), best


def _clip(text: str, n: int = 220) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n] + "..."


async def run(novel_id: int, chapter_num: int, limit: int, write_report: bool) -> int:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT id, title, refinement_provider_id FROM novels WHERE id = ?",
            (novel_id,),
        )
        novel = await cur.fetchone()
        if novel is None:
            print(f"ERROR: novel_id {novel_id} not found.")
            return 2

        cur = await conn.execute(
            "SELECT id, chapter_num, title_zh, original_text, translated_text, "
            "       refined_text, free_draft_text "
            "FROM chapters WHERE novel_id = ? AND chapter_num = ?",
            (novel_id, chapter_num),
        )
        ch = await cur.fetchone()
        if ch is None:
            print(f"ERROR: novel {novel_id} has no chapter {chapter_num}.")
            return 2
        if not (ch["translated_text"] or ch["refined_text"]):
            print(
                f"ERROR: chapter {chapter_num} has not been translated yet "
                "(no draft to derive the original paragraphs from)."
            )
            return 2

        ground_truth = await _ground_truth(conn, ch["id"])
        if not ground_truth:
            print(
                f"ERROR: chapter {chapter_num} has no captured edits to score "
                "against. Edit a few paragraphs in this chapter first (this is "
                "the held-out ground truth), then re-run."
            )
            return 2

        training = await _training_edits(conn, novel_id, ch["id"], limit)
        if not training:
            print(
                "ERROR: no style edits on OTHER chapters of this novel, so arm "
                "A and arm B would be identical. Make edits on earlier chapters "
                "(the training signal), then re-run."
            )
            return 2

        glossary = await global_glossary_svc.list_for_novel_with_globals(
            conn, novel_id
        )
        previous_context = await fetch_previous_chapter_tail(
            conn, novel_id, chapter_num
        )
        style_note = await fetch_style_note(conn, novel_id)
        provider = await resolve_translator_provider(conn, novel_id)
        novel_meta = await fetch_novel_genre_brief(conn, novel_id)
        free_draft = ch["free_draft_text"] if PROMPT_INCLUDE_FREE_DRAFT else None

        # Snapshot every field needed outside the connection scope.
        original_text = ch["original_text"]
        title_zh = ch["title_zh"]
        novel_title = novel["title"]
        uses_refiner = novel["refinement_provider_id"] is not None

    print("=" * 72)
    print(f"A/B style-edits harness  novel={novel_id} ({novel_title!r})  "
          f"chapter={chapter_num}")
    prov_label = (
        f"{provider.provider_type}/{provider.model_id}"
        if provider is not None
        else "(env-default factory)"
    )
    print(f"provider={prov_label}")
    print(f"training edits injected (arm A): {len(training)}  "
          f"(limit {limit}, held-out chapter {chapter_num} excluded)")
    print(f"ground-truth edited paragraphs (chapter {chapter_num}): "
          f"{len(ground_truth)}")
    if uses_refiner:
        print("NOTE: this novel uses a refiner. Absolute ratios may be low "
              "(comparing fresh drafts to possibly-refined edits); read the "
              "DELTA, not the absolutes.")
    print("=" * 72)
    print("Translating arm A (with style edits)...", flush=True)

    common = dict(
        previous_context=previous_context,
        use_cache=False,
        style_note=style_note,
        provider=provider,
        genre=novel_meta["genre"],
        custom_brief=novel_meta["custom_style_brief"],
        free_draft=free_draft,
        source_language=novel_meta["source_language"],
    )
    res_a = await translate_chapter(
        original_text, title_zh, glossary, style_edits=training, **common
    )
    print("Translating arm B (no style edits)...", flush=True)
    res_b = await translate_chapter(
        original_text, title_zh, glossary, style_edits=[], **common
    )

    paras_a = [p for p in (res_a.translated_text or "").split("\n\n") if p.strip()]
    paras_b = [p for p in (res_b.translated_text or "").split("\n\n") if p.strip()]

    rows = []
    for i, (before, after) in enumerate(ground_truth, start=1):
        ra, best_a = _score(paras_a, before, after)
        rb, best_b = _score(paras_b, before, after)
        rows.append({
            "i": i, "before": before, "after": after,
            "ratio_a": ra, "ratio_b": rb, "delta": ra - rb,
            "best_a": best_a, "best_b": best_b,
        })

    mean_a = sum(r["ratio_a"] for r in rows) / len(rows)
    mean_b = sum(r["ratio_b"] for r in rows) / len(rows)
    mean_delta = mean_a - mean_b
    wins = sum(1 for r in rows if r["delta"] > 1e-6)
    losses = sum(1 for r in rows if r["delta"] < -1e-6)
    ties = len(rows) - wins - losses

    print()
    print(f"{'para':>4}  {'arm A':>7}  {'arm B':>7}  {'delta':>8}")
    print("-" * 32)
    for r in rows:
        print(f"{r['i']:>4}  {r['ratio_a']:>7.3f}  {r['ratio_b']:>7.3f}  "
              f"{r['delta']:>+8.3f}")
    print("-" * 32)
    print(f"mean  {mean_a:>7.3f}  {mean_b:>7.3f}  {mean_delta:>+8.3f}")
    print(f"\nper-paragraph: A better x{wins}, B better x{losses}, tie x{ties}")

    if mean_delta > 0.01:
        verdict = (f"STYLE EDITS HELP: arm A is {mean_delta:+.3f} closer to "
                   "your edits on average.")
    elif mean_delta < -0.01:
        verdict = (f"REGRESSION: arm A is {mean_delta:+.3f} FARTHER from your "
                   "edits. The block is hurting on this chapter.")
    else:
        verdict = (f"NO MEASURABLE EFFECT: delta {mean_delta:+.3f} is within "
                   "noise. The English-only pairs are not moving the draft.")
    print(f"\nVERDICT: {verdict}")
    print("\nGraduation rule: cite the per-paragraph pairs below in any commit "
          "that acts on this result.")

    report_path = None
    if write_report:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        report_path = (
            PROJECT_ROOT / "data" / f"ab_style_edits_n{novel_id}_c{chapter_num}_{stamp}.md"
        )
        lines = [
            f"# A/B style-edits report: novel {novel_id} chapter {chapter_num}",
            "",
            f"- generated: {stamp}",
            f"- provider: {prov_label}",
            f"- training edits injected (arm A): {len(training)}",
            f"- ground-truth edited paragraphs: {len(ground_truth)}",
            f"- mean ratio arm A (with edits): {mean_a:.3f}",
            f"- mean ratio arm B (no edits): {mean_b:.3f}",
            f"- **mean delta (A - B): {mean_delta:+.3f}**",
            f"- per-paragraph: A better x{wins}, B better x{losses}, tie x{ties}",
            "",
            f"**Verdict:** {verdict}",
            "",
            "## Per-paragraph detail",
            "",
        ]
        for r in rows:
            lines += [
                f"### Paragraph {r['i']}  (A={r['ratio_a']:.3f} B={r['ratio_b']:.3f} "
                f"delta={r['delta']:+.3f})",
                "",
                f"- ORIGINAL draft (before): {_clip(r['before'])}",
                f"- USER edit (after, ground truth): {_clip(r['after'])}",
                f"- ARM A draft (with edits): {_clip(r['best_a'])}",
                f"- ARM B draft (no edits): {_clip(r['best_b'])}",
                "",
            ]
        report_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"\nReport written: {report_path}")

    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Single-variable A/B on the style-edits prompt block, "
        "scored against the user's own held-out edits."
    )
    ap.add_argument("--novel-id", type=int, required=True)
    ap.add_argument(
        "--chapter", type=int, required=True,
        help="held-out chapter number to re-translate and score (must already "
        "be translated AND have user edits)",
    )
    ap.add_argument(
        "--limit", type=int, default=STYLE_EDIT_LIMIT,
        help=f"style-edit window size for arm A (default {STYLE_EDIT_LIMIT}, "
        "matches the worker's STYLE_EDIT_LIMIT)",
    )
    ap.add_argument(
        "--no-report", action="store_true",
        help="skip writing the markdown report under data/",
    )
    args = ap.parse_args()
    code = asyncio.run(
        run(args.novel_id, args.chapter, args.limit, not args.no_report)
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
