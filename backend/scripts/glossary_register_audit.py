"""Read-only corpus-scale audit of the glossary lifecycle and the deterministic
fixup layer.

Background (see docs/translator-audit.md). The per-novel glossary is the shared
root behind two surface complaints — "the fixups mangle correct output" and "the
prose register feels off". One polluted artifact feeds both legs:

  * prose leg: ``enforce_locked_term_casing`` stamps each LOCKED atomic term's
    stored casing onto every occurrence in the body, so a generic word stored
    Title-Cased ("Perfection") gets force-capitalised mid-sentence.
  * register leg: ``format_glossary`` injects the WHOLE glossary (locked + auto)
    into every prompt with no casing guidance, training the model toward an
    over-capitalised, weighty register that compounds via the previous-chapter
    tail.

This script measures the root, not one chapter. It produces two reports:

1. GLOSSARY ROOT SIGNAL (cache-independent, always runs)
   - per-novel: how many entries would be force-cased into prose, how many
     Title-Cased entries are injected into prompts, and idiom rows whose stored
     English looks like a literal image rather than a plain sense.
   - corpus rollup: every ``term_en`` ranked by cross-novel document frequency.
     A term auto-extracted + Title-Cased across many distinct novels is generic
     by definition — this ranked list is the objective seed for an admission
     stop-list / casing down-pressure that replaces the hand-built
     ``GENERIC_LOWERCASE`` set.

2. FIXUP DELTA (best-effort, needs the on-disk llm_cache)
   - recovers the PRE-fixup model body from the translator cache (the queue
     stores it before fixups run), then runs each ``enforce_*`` transform in
     isolation over it and reports per-transform fire counts + sample rewrites.
     Coverage is partial: a chapter only resolves when its glossary/prompt still
     hash to the cached key (the glossary grows over time), so misses are
     reported, not hidden.

Everything is read-only: no DB writes, no LLM calls. Run against the live install:

    python -m backend.scripts.glossary_register_audit                # whole corpus
    python -m backend.scripts.glossary_register_audit --novel 7      # one novel
    python -m backend.scripts.glossary_register_audit --limit 50     # chapters/novel
    python -m backend.scripts.glossary_register_audit --no-fixup-delta
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import re
from collections import defaultdict

import aiosqlite

from backend.config import PROMPT_INCLUDE_FREE_DRAFT
from backend.db import open_conn
from backend.models import GlossaryEntry
from backend.services import global_glossary as global_glossary_svc
from backend.services import llm_cache, text_fixups
from backend.services.glossary_casing import (
    GENERIC_LOWERCASE,
    is_atomic_case_locked_term,
)
from backend.services.parser import (
    strip_heading_update_marker,
    strip_title_update_marker,
)
from backend.services.prompt_inputs import (
    fetch_novel_genre_brief,
    fetch_previous_chapter_tail,
    fetch_style_edits,
    fetch_style_note,
    resolve_translator_provider,
)

_VALID_CATEGORIES = {"character", "place", "technique", "item", "other", "idiom"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _is_title_cased(en: str) -> bool:
    """True when the English form carries proper-noun-shaped casing (some
    uppercase, not an all-caps acronym like 'BOOM')."""
    en = en.strip()
    if not en or en == en.lower() or en == en.upper():
        return False
    return any(c.isupper() for c in en)


def _row_to_entry(row: aiosqlite.Row) -> GlossaryEntry | None:
    """Build a GlossaryEntry from a raw glossary_entries row so the real
    casing predicates apply. Returns None on an unknown category value
    (legacy DBs) rather than raising."""
    cat = row["category"]
    if cat not in _VALID_CATEGORIES:
        return None
    return GlossaryEntry(
        id=row["id"],
        novel_id=row["novel_id"],
        term_zh=row["term_zh"],
        term_en=row["term_en"],
        category=cat,  # type: ignore[arg-type]
        notes=row["notes"],
        auto_detected=bool(row["auto_detected"]),
        locked=bool(row["locked"]),
    )


def _token_diffs(before: str, after: str, limit: int) -> list[tuple[str, str]]:
    """Extract up to `limit` changed (before→after) fragments. Tokens keep
    newline runs so a paragraph-join shows up as a removed '\\n\\n'."""
    btok = re.findall(r"\n+|\S+", before)
    atok = re.findall(r"\n+|\S+", after)
    out: list[tuple[str, str]] = []
    sm = difflib.SequenceMatcher(a=btok, b=atok, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        b = " ".join(btok[i1:i2]).replace("\n", "\\n")
        a = " ".join(atok[j1:j2]).replace("\n", "\\n")
        out.append((b or "∅", a or "∅"))
        if len(out) >= limit:
            break
    return out


# Body fixups that can mangle prose, paired with the glossary they need.
# Order matches queue._apply_text_fixups; each is run in ISOLATION on the raw
# body so a hit is attributable to one transform.
def _body_fixups(glossary: list[GlossaryEntry]):
    g = glossary
    return [
        ("enforce_locked_term_casing", lambda t: text_fixups.enforce_locked_term_casing(t, g)),
        ("enforce_lowercase_locked_terms", lambda t: text_fixups.enforce_lowercase_locked_terms(t, g)),
        ("enforce_stem_branch_casing", lambda t: text_fixups.enforce_stem_branch_casing(t)),
        ("enforce_em_dash", lambda t: text_fixups.enforce_em_dash(t)),
        ("enforce_spaced_hyphen_dash", lambda t: text_fixups.enforce_spaced_hyphen_dash(t)),
        ("enforce_brackets", lambda t: text_fixups.enforce_brackets(t, g)),
        ("enforce_balanced_emphasis", lambda t: text_fixups.enforce_balanced_emphasis(t)),
        ("enforce_sentence_initial_capitalization", lambda t: text_fixups.enforce_sentence_initial_capitalization(t)),
        ("enforce_mid_sentence_comma_break", lambda t: text_fixups.enforce_mid_sentence_comma_break(t)),
    ]


# --------------------------------------------------------------------------- #
# Report 1: glossary root signal
# --------------------------------------------------------------------------- #
async def _glossary_root_report(
    conn: aiosqlite.Connection, novel_filter: int | None
) -> None:
    sql = (
        "SELECT g.id, g.novel_id, g.term_zh, g.term_en, g.category, g.notes, "
        "       g.auto_detected, g.locked, n.title AS novel_title "
        "FROM glossary_entries g JOIN novels n ON n.id = g.novel_id"
    )
    params: tuple = ()
    if novel_filter is not None:
        sql += " WHERE g.novel_id = ?"
        params = (novel_filter,)
    rows = await (await conn.execute(sql, params)).fetchall()

    per_novel: dict[int, dict] = defaultdict(
        lambda: {"title": "", "enforced": [], "injected": [], "idiom_suspect": []}
    )
    # cross-novel document frequency of each Title-Cased term_en
    df_novels: dict[str, set[int]] = defaultdict(set)
    df_total: dict[str, int] = defaultdict(int)
    df_zh: dict[str, set[str]] = defaultdict(set)
    df_cat: dict[str, set[str]] = defaultdict(set)

    for row in rows:
        entry = _row_to_entry(row)
        if entry is None:
            continue
        nid = row["novel_id"]
        slot = per_novel[nid]
        slot["title"] = row["novel_title"] or f"novel {nid}"
        en = (entry.term_en or "").strip()
        en_l = en.lower()

        # register leg: any Title-Cased entry (locked or auto) reaches the prompt
        if _is_title_cased(en) and en_l not in GENERIC_LOWERCASE:
            slot["injected"].append(entry)
            df_novels[en_l].add(nid)
            df_total[en_l] += 1
            df_zh[en_l].add(entry.term_zh)
            df_cat[en_l].add(entry.category)

        # prose leg: would be force-cased into the body
        if is_atomic_case_locked_term(entry) and en_l not in GENERIC_LOWERCASE:
            slot["enforced"].append(entry)

        # idiom stored as image: idioms are policy-lowercase; a Title-Cased one
        # is a stored-image / policy-violation suspect to eyeball.
        if entry.category == "idiom" and _is_title_cased(en):
            slot["idiom_suspect"].append(entry)

    print("=" * 78)
    print("REPORT 1 — GLOSSARY ROOT SIGNAL")
    print("=" * 78)
    for nid in sorted(per_novel):
        s = per_novel[nid]
        print(
            f"\nnovel {nid}: {s['title']}\n"
            f"  enforced-into-prose (locked atomic, non-generic): {len(s['enforced'])}\n"
            f"  Title-Cased entries injected into prompts:        {len(s['injected'])}\n"
            f"  idiom rows Title-Cased (stored-image suspects):   {len(s['idiom_suspect'])}"
        )
        for e in s["enforced"][:8]:
            print(f"      [prose] {e.term_zh} -> {e.term_en}  ({e.category})")
        for e in s["idiom_suspect"][:5]:
            print(f"      [idiom] {e.term_zh} -> {e.term_en}")

    print("\n" + "-" * 78)
    print("CORPUS ROLLUP — Title-Cased term_en ranked by cross-novel frequency")
    print("(high document-frequency = generic by definition = stop-list candidate)")
    print("-" * 78)
    ranked = sorted(
        df_novels.items(),
        key=lambda kv: (len(kv[1]), df_total[kv[0]]),
        reverse=True,
    )
    multi = [(t, ns) for t, ns in ranked if len(ns) >= 2]
    if not multi:
        print("  (no term_en appears Title-Cased across 2+ novels)")
    print(f"  {'term_en':<28} {'#novels':>7} {'#rows':>6}  categories / sample zh")
    for term, novels in multi[:40]:
        cats = ",".join(sorted(df_cat[term]))
        zh = ",".join(sorted(df_zh[term])[:3])
        print(f"  {term:<28} {len(novels):>7} {df_total[term]:>6}  {cats}  [{zh}]")


# --------------------------------------------------------------------------- #
# Report 2: fixup delta (cache-dependent, best-effort)
# --------------------------------------------------------------------------- #
async def _fixup_delta_report(
    conn: aiosqlite.Connection, novel_filter: int | None, limit: int
) -> None:
    try:
        from backend.services.translators.factory import get_translator
    except Exception as e:  # pragma: no cover - depends on installed backends
        print(f"\n(skipping fixup-delta: backend import failed: {e})")
        return

    print("\n" + "=" * 78)
    print("REPORT 2 — FIXUP DELTA (pre-fixup model body vs each enforce_* transform)")
    print("=" * 78)

    novel_sql = "SELECT id, title FROM novels"
    nparams: tuple = ()
    if novel_filter is not None:
        novel_sql += " WHERE id = ?"
        nparams = (novel_filter,)
    novels = await (await conn.execute(novel_sql, nparams)).fetchall()

    fire_counts: dict[str, int] = defaultdict(int)
    examples: dict[str, list[tuple[str, str]]] = defaultdict(list)
    hits = misses = 0

    for nrow in novels:
        novel_id = nrow["id"]
        provider = await resolve_translator_provider(conn, novel_id)
        if provider is None:
            continue
        try:
            backend = get_translator(provider)
        except Exception as e:
            print(f"  novel {novel_id}: cannot build backend ({e}); skipping")
            continue

        glossary = await global_glossary_svc.list_for_novel_with_globals(
            conn, novel_id
        )
        meta = await fetch_novel_genre_brief(conn, novel_id)
        style_edits = await fetch_style_edits(conn, novel_id)
        style_note = await fetch_style_note(conn, novel_id)

        chapters = await (await conn.execute(
            "SELECT chapter_num, title_zh, original_text, free_draft_text "
            "FROM chapters WHERE novel_id = ? AND status = 'done' "
            "  AND translated_text IS NOT NULL AND translated_text != '' "
            "ORDER BY chapter_num LIMIT ?",
            (novel_id, limit),
        )).fetchall()

        fixups = _body_fixups(glossary)
        for c in chapters:
            if not (c["original_text"] or "").strip():
                continue
            prev = await fetch_previous_chapter_tail(
                conn, novel_id, c["chapter_num"]
            )
            free_draft = c["free_draft_text"] if PROMPT_INCLUDE_FREE_DRAFT else None
            prompt_title = strip_title_update_marker(c["title_zh"]) or None
            prompt_src = strip_heading_update_marker(c["original_text"])
            try:
                _prompt, key = backend._begin_chapter(
                    prompt_src, prompt_title, glossary, prev, style_edits,
                    style_note=style_note, genre=meta["genre"],
                    custom_brief=meta["custom_style_brief"], free_draft=free_draft,
                )
            except Exception:
                misses += 1
                continue
            cached = llm_cache.load_translation(key)
            if cached is None:
                misses += 1
                continue
            hits += 1
            body = cached.translated_text
            for name, fn in fixups:
                new, count = fn(body)
                if count and new != body:
                    fire_counts[name] += count
                    if len(examples[name]) < 12:
                        examples[name].extend(_token_diffs(body, new, 3))

    print(
        f"\ncache coverage: {hits} chapters resolved, {misses} missed "
        f"(miss = glossary/prompt no longer hashes to a cached key)."
    )
    if not hits:
        print("no pre-fixup bodies recovered; nothing to attribute.")
        return
    print("\nper-transform fire counts (changes the deterministic layer made):")
    for name, _fn in _body_fixups([]):
        print(f"  {name:<42} {fire_counts.get(name, 0):>6}")
    print("\nsample rewrites (before -> after):")
    for name, _fn in _body_fixups([]):
        ex = examples.get(name) or []
        if not ex:
            continue
        print(f"  [{name}]")
        for b, a in ex[:6]:
            print(f"      {b!r}  ->  {a!r}")


async def _amain(novel: int | None, limit: int, fixup_delta: bool) -> None:
    async with open_conn() as conn:
        await _glossary_root_report(conn, novel)
        if fixup_delta:
            await _fixup_delta_report(conn, novel, limit)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--novel", type=int, default=None,
                    help="narrow to one novel id (default: whole corpus)")
    ap.add_argument("--limit", type=int, default=25,
                    help="max chapters per novel for the fixup-delta pass")
    ap.add_argument("--no-fixup-delta", action="store_true",
                    help="skip the cache-dependent fixup-delta report")
    args = ap.parse_args()
    asyncio.run(_amain(args.novel, args.limit, not args.no_fixup_delta))


if __name__ == "__main__":
    main()
