"""Pre-translation local checks (Section 6.2).

Local-only heuristics that flag suspicious chapters BEFORE the user fires
the paid LLM call. The cheapest token saved is the one you don't burn on
bad input.

Surfaced via GET /api/novels/{id}/chapters/{n}/pre-check; the reader
shows the list inline next to the translate button so the user can
either fix the chapter (re-import, edit) or proceed knowingly.

Invariant (audited 2026-05-23): `chapters.original_text` is set on
INSERT during import (translate.py) and is never updated by any live
runtime path — only one-shot migration scripts mutate it. So pre-check
inputs are stable across retranslates as long as the user doesn't run
a migration; we don't need to track an `original_text_edited_at`
signal or worry about flags re-surfacing on a manually-corrected
chapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import aiosqlite

from backend.services import glossary as glossary_svc
from backend.services.glossary_filters import detect_candidate_terms

# A "tiny" chapter — almost certainly a parse error or an author-note row.
_TINY_BODY_CHARS = 200

# Threshold for surfacing the glossary-candidates warning. Fewer than this
# many out-of-glossary terms isn't worth interrupting the user.
_GLOSSARY_CANDIDATE_THRESHOLD = 6

# CJK character ranges considered "expected content" for a Chinese chapter.
# Used to compute the non-CJK proportion as an OCR-shape signal.
_CJK_RE = re.compile(r"[一-鿿〇〤㐀-䶿]")

# Bracket pairs we expect to balance. 【】 is the system-interface marker
# the glossary auto-extractor keys off; an unbalanced one usually means a
# tag got truncated by an upstream scraper.
_BRACKET_PAIRS = (
    ("【", "】"),
    ("「", "」"),
    ("『", "』"),
    ("《", "》"),
    ('"', '"'),
    ("'", "'"),
)


@dataclass
class PreCheckWarning:
    code: str
    severity: str  # "info" | "warn" | "alert"
    message: str
    count: int | None = None

    def to_dict(self) -> dict:
        d = {"code": self.code, "severity": self.severity, "message": self.message}
        if self.count is not None:
            d["count"] = self.count
        return d


async def chapter_pre_check(
    conn: aiosqlite.Connection, novel_id: int, chapter_num: int
) -> list[dict]:
    """Run the pre-flight heuristics for one chapter and return the list of
    warnings as JSON-serialisable dicts.

    Empty list means "all clear, nothing flagged"; the reader treats that
    as the happy path and shows no panel."""
    cur = await conn.execute(
        "SELECT original_text, status FROM chapters "
        "WHERE novel_id = ? AND chapter_num = ?",
        (novel_id, chapter_num),
    )
    row = await cur.fetchone()
    if row is None:
        return []
    body = row["original_text"] or ""
    warnings: list[PreCheckWarning] = []

    # 1. Length sanity.
    stripped = body.strip()
    if not stripped:
        warnings.append(PreCheckWarning(
            "empty_body", "alert",
            "Source body is empty. Translating would produce nothing usable.",
            count=0,
        ))
    elif len(stripped) < _TINY_BODY_CHARS:
        warnings.append(PreCheckWarning(
            "tiny_body", "warn",
            f"Source body is only {len(stripped)} characters — usually a "
            f"parse error or stray author-note row, not a real chapter.",
            count=len(stripped),
        ))

    # 2. Glossary saturation — count proper-noun candidates not yet glossed.
    if stripped:
        existing = await _existing_glossary_terms(conn, novel_id)
        candidates = detect_candidate_terms(body, existing)
        if len(candidates) >= _GLOSSARY_CANDIDATE_THRESHOLD:
            top = ", ".join(c["term"] for c in candidates[:5])
            warnings.append(PreCheckWarning(
                "glossary_candidates", "info",
                f"{len(candidates)} likely proper nouns not in glossary "
                f"(e.g. {top}). Add the important ones first for consistent "
                f"rendering across chapters.",
                count=len(candidates),
            ))

    # 3. Unbalanced brackets — usually a scrape that dropped a closing tag.
    for opener, closer in _BRACKET_PAIRS:
        n_open = body.count(opener)
        n_close = body.count(closer)
        if opener == closer:
            # Same-character pair: only flag if it's an odd count.
            if n_open % 2 != 0:
                warnings.append(PreCheckWarning(
                    "unbalanced_punctuation", "info",
                    f"Odd number of {opener!r} marks ({n_open}); the chapter "
                    f"may have a truncated quotation.",
                    count=n_open,
                ))
            continue
        if n_open != n_close:
            warnings.append(PreCheckWarning(
                "unbalanced_punctuation", "info",
                f"Bracket count mismatch: {n_open}×{opener!r} vs "
                f"{n_close}×{closer!r}. Often means an upstream scraper "
                f"truncated a tag.",
                count=abs(n_open - n_close),
            ))

    # 4. OCR / mojibake shape — a Chinese chapter that's mostly NON-CJK is
    # almost always a scrape that pulled the wrong element (English ads,
    # navigation, error-page boilerplate).
    if stripped:
        total = len(stripped)
        cjk_count = len(_CJK_RE.findall(stripped))
        if total >= 200 and (cjk_count / total) < 0.3:
            pct = round(100 * cjk_count / total)
            warnings.append(PreCheckWarning(
                "low_cjk_ratio", "warn",
                f"Only {pct}% of the body is CJK characters — the import "
                f"may have grabbed boilerplate or hit a paywall page "
                f"instead of the actual chapter.",
                count=cjk_count,
            ))

    return [w.to_dict() for w in warnings]


async def _existing_glossary_terms(
    conn: aiosqlite.Connection, novel_id: int
) -> set[str]:
    entries = await glossary_svc.list_for_novel(conn, novel_id)
    out: set[str] = set()
    for e in entries:
        if e.term_zh:
            out.add(e.term_zh)
    return out
