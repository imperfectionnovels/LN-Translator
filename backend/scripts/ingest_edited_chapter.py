"""Learn-from-edits batch ingestion: take a fully edited chapter, three-way-diff
it against the stored draft, and route each changed paragraph to the layer that
should absorb it (glossary / brief / engine fixup / style_edits). Review-first:
it prints a routed report and writes NOTHING unless --apply is given, which
stages ONLY style_edits rows (never overwrites the chapter body).

    python -m backend.scripts.ingest_edited_chapter --novel-id N --chapter C \
        (--edited-file PATH | --edited-stdin) [--source draft|refined] \
        [--report] [--apply]

Why this exists: edits made OUTSIDE the app (in a word processor, a pasted doc)
never reach the style_edits table, so the glossary and the per-novel brief never
learn from them. This is the bulk counterpart to the in-app POST /edit-paragraph
and the automated form of the manual learn-from-edits pass.

Routing (heuristic; the report is the product, a human confirms each move):
  - GLOSSARY (casing): a glossary term changed case only. If the matching row is
    a half-applied lowercase hatch, that is named so it can be repaired.
  - GLOSSARY (term): a glossary term_en was swapped for different wording.
  - MECHANICAL: replaying the deterministic text_fixups on the draft paragraph
    already reaches (or moves toward) the edit, so a fixup owns it. Names the
    fixup; "already handled" means a retranslate fixes it for free.
  - STYLE: a genuine per-paragraph rewrite. These are the style_edits rows.
Aggregate signals (exclamation density, dash substitution, honorific repetition)
are surfaced as custom_style_brief candidates, not per-paragraph rows.

DB target: uses the ambient config DB (dev data/novels.db unless
LN_TRANSLATOR_DATA points the data root at the live store). Default mode is
read-only; --apply refuses to run unless you confirm the DB path.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import sys
from datetime import datetime

from backend.config import DB_PATH, PROJECT_ROOT
from backend.db import open_conn
from backend.scripts.ab_style_edits import _clip
from backend.services import global_glossary as global_glossary_svc
from backend.services.glossary import is_half_applied_lowercase_hatch
from backend.services.text_fixups import (
    build_glossary_term_set,
    enforce_brackets,
    enforce_em_dash,
    enforce_lowercase_locked_terms,
    enforce_spaced_hyphen_dash,
)


def _split_paras(text: str) -> list[str]:
    return [p.strip() for p in (text or "").split("\n\n") if p.strip()]


def _norm_ws(s: str) -> str:
    return " ".join((s or "").split())


def _align_pairs(
    draft: list[str], edited: list[str]
) -> tuple[list[tuple[str, str]], int, int]:
    """Pair changed draft paragraphs with their edited counterparts using
    difflib opcodes over the paragraph lists. Returns (changed_pairs,
    n_inserted, n_deleted). A 'replace' block is paired greedily by best
    similarity so paragraph-count drift inside the block is tolerated."""
    sm = difflib.SequenceMatcher(None, draft, edited, autojunk=False)
    pairs: list[tuple[str, str]] = []
    inserted = deleted = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        d_block = draft[i1:i2]
        e_block = edited[j1:j2]
        if tag == "insert":
            inserted += len(e_block)
            continue
        if tag == "delete":
            deleted += len(d_block)
            continue
        # replace: greedily pair each edited para to its closest draft para.
        remaining = list(d_block)
        for e in e_block:
            if not remaining:
                inserted += 1
                continue
            best = max(
                remaining,
                key=lambda d: difflib.SequenceMatcher(None, d, e).ratio(),
            )
            remaining.remove(best)
            if _norm_ws(best) != _norm_ws(e):
                pairs.append((best, e))
        deleted += len(remaining)
    return pairs, inserted, deleted


def _casing_only(before: str, after: str) -> bool:
    return before != after and before.lower() == after.lower()


def _changed_case_terms(before: str, after: str, term_lower: set[str]) -> list[str]:
    """Tokens that differ only in case between before/after and whose lowercase
    form is a known glossary term. Word-aligned; tolerant of length drift by
    comparing the lowercased token multiset positions."""
    bt = before.split()
    at = after.split()
    out: list[str] = []
    for b, a in zip(bt, at):
        bare_b = b.strip(".,;:!?\"'()[]")
        bare_a = a.strip(".,;:!?\"'()[]")
        if bare_b != bare_a and bare_b.lower() == bare_a.lower():
            if bare_b.lower() in term_lower or bare_a.lower() in term_lower:
                out.append(f"{bare_b} -> {bare_a}")
    return out


def _apply_fixups(text: str, glossary) -> tuple[str, list[str]]:
    """Replay the glossary-independent + glossary-aware deterministic fixups,
    returning the rewritten text and the names of the ones that changed it."""
    fired: list[str] = []
    out = text
    for name, fn in (
        ("enforce_em_dash", lambda t: enforce_em_dash(t)),
        ("enforce_spaced_hyphen_dash", lambda t: enforce_spaced_hyphen_dash(t)),
        ("enforce_lowercase_locked_terms",
         lambda t: enforce_lowercase_locked_terms(t, glossary)),
        ("enforce_brackets", lambda t: enforce_brackets(t, glossary=glossary)),
    ):
        new, n = fn(out)
        if n and new != out:
            fired.append(name)
            out = new
    return out, fired


def _glossary_terms_in(text: str, term_en_list: list[str]) -> list[str]:
    low = text.lower()
    return [t for t in term_en_list if t and t.lower() in low]


def _classify(before, after, term_lower, term_en_list, by_en, glossary):
    if _casing_only(before, after):
        terms = _changed_case_terms(before, after, term_lower)
        hatches = []
        for pair in terms:
            en = pair.split(" -> ")[0].lower()
            g = by_en.get(en)
            if g is not None and is_half_applied_lowercase_hatch(g):
                hatches.append(g.term_en)
        return "glossary-casing", {"terms": terms, "half_applied": hatches}
    fixed, fired = _apply_fixups(before, glossary)
    if fired and _norm_ws(fixed) == _norm_ws(after):
        return "mechanical", {"fixups": fired, "complete": True}
    if fired and (
        difflib.SequenceMatcher(None, fixed, after).ratio()
        > difflib.SequenceMatcher(None, before, after).ratio()
    ):
        return "mechanical", {"fixups": fired, "complete": False}
    before_terms = set(_glossary_terms_in(before, term_en_list))
    after_terms = set(_glossary_terms_in(after, term_en_list))
    dropped = before_terms - after_terms
    if dropped:
        return "glossary-term", {"dropped": sorted(dropped)}
    return "style", {}


def _aggregate_signals(pairs: list[tuple[str, str]]) -> list[str]:
    """Brief-candidate signals: edit patterns that repeat across paragraphs."""
    sigs: list[str] = []
    bang = sum(b.count("!") - a.count("!") for b, a in pairs)
    if bang >= 3:
        sigs.append(
            f"Exclamation density: the edit removed {bang} '!' overall. Candidate "
            "shared rule (base.md) or brief note: do not carry source ! one-for-one."
        )
    dash = sum((b.count(" - ") - a.count(" - ")) for b, a in pairs)
    if dash >= 2:
        sigs.append(
            f"Spaced-hyphen dashes: the edit removed {dash} ' - '. "
            "enforce_spaced_hyphen_dash now handles these on retranslate."
        )
    humble = sum(
        b.lower().count("this humble one") - a.lower().count("this humble one")
        for b, a in pairs
    )
    if humble >= 2:
        sigs.append(
            f"Honorific repetition: 'this humble one' dropped {humble} times. "
            "Candidate brief note: thin out 在下 to 'I' in running dialogue."
        )
    return sigs


async def run(novel_id, chapter_num, edited_text, source, apply, write_report):
    async with open_conn() as conn:
        ch = await (await conn.execute(
            "SELECT id, translated_text, refined_text FROM chapters "
            "WHERE novel_id=? AND chapter_num=?",
            (novel_id, chapter_num),
        )).fetchone()
        if ch is None:
            print(f"ERROR: novel {novel_id} has no chapter {chapter_num}.")
            return 2
        draft_text = ch["refined_text"] if source == "refined" else ch["translated_text"]
        if not draft_text:
            print(f"ERROR: chapter {chapter_num} has no {source} text to diff against.")
            return 2
        glossary = await global_glossary_svc.list_for_novel_with_globals(conn, novel_id)
        chapter_id = ch["id"]

    term_en_list = sorted(
        {(g.term_en or "").strip() for g in glossary if (g.term_en or "").strip()},
        key=len, reverse=True,
    )
    term_lower = {t.lower() for t in build_glossary_term_set(glossary)}
    by_en = {(g.term_en or "").strip().lower(): g for g in glossary}

    draft_paras = _split_paras(draft_text)
    edited_paras = _split_paras(edited_text)
    pairs, inserted, deleted = _align_pairs(draft_paras, edited_paras)

    buckets: dict[str, list] = {
        "glossary-casing": [], "glossary-term": [], "mechanical": [], "style": [],
    }
    for before, after in pairs:
        route, detail = _classify(
            before, after, term_lower, term_en_list, by_en, glossary
        )
        buckets[route].append((before, after, detail))
    signals = _aggregate_signals(pairs)

    lines: list[str] = []
    p = lines.append
    p(f"# Learn-from-edits report: novel {novel_id} chapter {chapter_num}")
    p("")
    p(f"- source draft: {source}")
    p(f"- changed paragraphs: {len(pairs)}  (inserted {inserted}, deleted {deleted})")
    for k in ("glossary-casing", "glossary-term", "mechanical", "style"):
        p(f"- {k}: {len(buckets[k])}")
    p("")
    if signals:
        p("## Brief / shared-rule candidates (repeating patterns)")
        p("")
        for s in signals:
            p(f"- {s}")
        p("")

    half = sorted({
        t for _, _, d in buckets["glossary-casing"] for t in d.get("half_applied", [])
    })
    if half:
        p("## Half-applied lowercase hatches to repair (glossary)")
        p("")
        for t in half:
            p(f"- {t!r}: note says lowercase but term_en is still Title-Cased; "
              "lower term_en so the down-caser fires.")
        p("")

    for route, title in (
        ("glossary-casing", "GLOSSARY (casing) deltas"),
        ("glossary-term", "GLOSSARY (terminology) deltas"),
        ("mechanical", "MECHANICAL deltas (a fixup owns these)"),
        ("style", "STYLE deltas (per-paragraph rewrites -> style_edits)"),
    ):
        items = buckets[route]
        if not items:
            continue
        p(f"## {title}  ({len(items)})")
        p("")
        for i, (before, after, detail) in enumerate(items, 1):
            tag = ""
            if route == "glossary-casing":
                tag = "  terms: " + "; ".join(detail.get("terms", []) or ["(none matched)"])
            elif route == "glossary-term":
                tag = "  dropped: " + ", ".join(detail.get("dropped", []))
            elif route == "mechanical":
                done = "already handled" if detail.get("complete") else "partial"
                tag = f"  fixups: {', '.join(detail.get('fixups', []))} ({done})"
            p(f"### {route} {i}{tag}")
            p("")
            p(f"- before: {_clip(before)}")
            p(f"- after:  {_clip(after)}")
            p("")

    report = "\n".join(lines)
    print(report)

    if write_report:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = PROJECT_ROOT / "data" / f"ingest_n{novel_id}_c{chapter_num}_{stamp}.md"
        out.write_text(report, encoding="utf-8")
        print(f"\nReport written: {out}")

    if apply:
        style_pairs = [(b, a) for b, a, _ in buckets["style"]]
        if not style_pairs:
            print("\n--apply: no STYLE-route rewrites to stage.")
            return 0
        print(f"\n--apply: about to stage {len(style_pairs)} style_edits row(s) "
              f"into:\n  {DB_PATH}")
        print("These feed FUTURE prompts; the chapter body is NOT touched.")
        resp = input("Type the chapter number to confirm: ").strip()
        if resp != str(chapter_num):
            print("Confirmation mismatch; nothing written.")
            return 1
        async with open_conn() as conn:
            for before, after in style_pairs:
                await conn.execute(
                    "INSERT INTO style_edits (novel_id, chapter_id, before_text, "
                    "after_text) VALUES (?, ?, ?, ?)",
                    (novel_id, chapter_id, before, after),
                )
            await conn.commit()
        print(f"Staged {len(style_pairs)} style_edits row(s).")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--novel-id", type=int, required=True)
    ap.add_argument("--chapter", type=int, required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--edited-file", help="path to the edited chapter text (UTF-8)")
    src.add_argument("--edited-stdin", action="store_true", help="read edited text from stdin")
    ap.add_argument("--source", choices=["draft", "refined"], default="draft",
                    help="which stored body to diff against (default draft = translated_text)")
    ap.add_argument("--report", action="store_true", help="also write the report under data/")
    ap.add_argument("--apply", action="store_true",
                    help="stage STYLE-route rewrites as style_edits (prompts for confirmation)")
    args = ap.parse_args()
    if args.edited_stdin:
        edited = sys.stdin.read()
    else:
        with open(args.edited_file, encoding="utf-8") as fh:
            edited = fh.read()
    code = asyncio.run(run(
        args.novel_id, args.chapter, edited, args.source, args.apply, args.report
    ))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
