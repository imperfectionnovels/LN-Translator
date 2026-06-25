"""In-app learn-from-edits loop (the prose lever).

The reader's edit mode already captures each paragraph rewrite as a style_edits
row (routes/chapters.py::edit_paragraph), and those rows already feed future
prompts (prompt_inputs.py). What is NOT captured automatically is the
cross-paragraph LEARNING: voice patterns worth promoting to the per-novel brief,
and glossary terms the user recased. This service derives those from the
captured edits as a STAGED proposal (writes nothing), then applies only the
human-confirmed subset.

Stage -> confirm -> commit is mandatory; commit re-derives the proposal
server-side and honors only ids that re-derive, so a client cannot forge a write
to an arbitrary glossary entry. Glossary writes go through glossary_svc.update_entry
(lock-on-edit, never deletes, never clobbers a locked rendering with auto-detect);
the brief is append-only. The MECHANICAL bucket is intentionally absent: a fixup
already owns those on retranslate, so re-teaching them as style edits is wrong.

The brief-candidate signals reuse the pure scorer from the batch CLI
(scripts/ingest_edited_chapter._aggregate_signals); the CLI is unchanged.
"""

from __future__ import annotations

import aiosqlite

from backend.scripts.ingest_edited_chapter import _aggregate_signals
from backend.services import glossary as glossary_svc


def _detect_casing_changes(pairs: list[tuple[str, str]], glossary) -> list[dict]:
    """Per-novel glossary terms the user recased (same letters, different case).

    For each entry whose term_en occurs in both the before and after text, if the
    after rendering carries a different casing, that is a recase the glossary
    should absorb so the term renders consistently everywhere. One proposal per
    entry (first occurrence wins). Robust for multi-word terms because it matches
    the full term_en, not single tokens.
    """
    seen: dict[int, dict] = {}
    for before, after in pairs:
        bl, al = (before or "").lower(), (after or "").lower()
        for g in glossary:
            entry_id = getattr(g, "id", None)
            en = (g.term_en or "").strip()
            if entry_id is None or len(en) < 2:
                continue
            enl = en.lower()
            if entry_id in seen or enl not in bl or enl not in al:
                continue
            idx = al.find(enl)
            after_cased = after[idx:idx + len(en)]
            if after_cased != en and after_cased.lower() == enl:
                seen[entry_id] = {
                    "id": f"gloss-{entry_id}",
                    "entry_id": entry_id,
                    "term_zh": g.term_zh,
                    "term_en": en,
                    "proposed_en": after_cased,
                    "default": False,
                }
    return list(seen.values())


async def _captured_pairs(
    conn: aiosqlite.Connection, novel_id: int, chapter_id: int
) -> list[tuple[str, str]]:
    cur = await conn.execute(
        "SELECT before_text, after_text FROM style_edits "
        "WHERE novel_id = ? AND chapter_id = ? ORDER BY id",
        (novel_id, chapter_id),
    )
    return [(r["before_text"], r["after_text"]) for r in await cur.fetchall()]


async def build_proposal(
    conn: aiosqlite.Connection, novel_id: int, chapter_num: int
) -> dict | None:
    """Staged proposal from this chapter's captured edits. Writes nothing.

    Returns None when the chapter does not exist. An empty proposal (no captured
    edits) is a valid result the UI renders as "nothing to learn yet".
    """
    row = await (await conn.execute(
        "SELECT id FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )).fetchone()
    if row is None:
        return None
    chapter_id = row["id"]

    pairs = await _captured_pairs(conn, novel_id, chapter_id)
    glossary = await glossary_svc.list_for_novel(conn, novel_id)

    brief = [
        {"id": f"brief-{i}", "text": s, "default": False}
        for i, s in enumerate(_aggregate_signals(pairs))
    ]
    return {
        "novel_id": novel_id,
        "chapter_num": chapter_num,
        "captured_edits": len(pairs),
        "brief": brief,
        "glossary_casing": _detect_casing_changes(pairs, glossary),
    }


async def _append_brief(
    conn: aiosqlite.Connection, novel_id: int, additions: list[str]
) -> int:
    """Append confirmed signals to novels.custom_style_brief (append-only).

    Skips a signal already present verbatim so re-running the panel does not
    duplicate notes."""
    row = await (await conn.execute(
        "SELECT custom_style_brief FROM novels WHERE id = ?", (novel_id,)
    )).fetchone()
    current = (row["custom_style_brief"] or "") if row else ""
    fresh = [a for a in additions if a and a not in current]
    if not fresh:
        return 0
    block = "\n".join(f"- {a}" for a in fresh)
    new = (current.rstrip() + "\n" + block).strip() if current.strip() else block
    await conn.execute(
        "UPDATE novels SET custom_style_brief = ? WHERE id = ?", (new, novel_id)
    )
    return len(fresh)


async def _save_ground_truth(
    conn: aiosqlite.Connection, novel_id: int, chapter_num: int
) -> bool:
    """Persist the chapter's current (user-approved) body as a ground-truth
    reference for quality_report --diff. Captures COALESCE(refined_text,
    translated_text) to match what the reader shows, plus the config that
    produced it."""
    row = await (await conn.execute(
        "SELECT id, COALESCE(refined_text, translated_text) AS body, "
        "refined_text IS NOT NULL AS is_refined, prompt_config_snapshot "
        "FROM chapters WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )).fetchone()
    if row is None or not row["body"]:
        return False
    await conn.execute(
        "INSERT INTO ground_truth_edits "
        "(novel_id, chapter_id, chapter_num, source, edited_text, prompt_config_snapshot) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            novel_id, row["id"], chapter_num,
            "refined" if row["is_refined"] else "draft",
            row["body"], row["prompt_config_snapshot"],
        ),
    )
    return True


async def commit_proposal(
    conn: aiosqlite.Connection,
    novel_id: int,
    chapter_num: int,
    selection: dict,
) -> dict | None:
    """Apply the confirmed subset of a re-derived proposal.

    `selection` = {brief: [ids], glossary_casing: [ids], save_ground_truth: bool}.
    Re-derives the proposal so only ids that re-derive are honored (forge guard).
    Returns a summary, or None when the chapter does not exist.
    """
    proposal = await build_proposal(conn, novel_id, chapter_num)
    if proposal is None:
        return None

    want_brief = set(selection.get("brief", []))
    want_gloss = set(selection.get("glossary_casing", []))

    brief_texts = [b["text"] for b in proposal["brief"] if b["id"] in want_brief]
    applied_brief = await _append_brief(conn, novel_id, brief_texts)

    applied_gloss = 0
    for item in proposal["glossary_casing"]:
        if item["id"] not in want_gloss:
            continue
        proposed = item["proposed_en"]
        entry = await glossary_svc.get_one(conn, item["entry_id"])
        if entry is None:
            continue
        # When the user lowercased a Title-cased term, keep the down-caser
        # backstop honest by noting the lowercase intent (see glossary_casing).
        notes = entry.notes
        if proposed == proposed.lower() and (entry.term_en or "") != (entry.term_en or "").lower():
            base = (entry.notes or "").strip()
            if "lowercase" not in base.lower():
                notes = (base + " lowercase").strip() if base else "lowercase"
        await glossary_svc.update_entry(
            conn, item["entry_id"], term_en=proposed, category=None,
            notes=notes, locked=True,
        )
        applied_gloss += 1

    ground_truth_saved = False
    if selection.get("save_ground_truth"):
        ground_truth_saved = await _save_ground_truth(conn, novel_id, chapter_num)

    await conn.commit()
    return {
        "applied_brief": applied_brief,
        "applied_glossary": applied_gloss,
        "ground_truth_saved": ground_truth_saved,
    }
