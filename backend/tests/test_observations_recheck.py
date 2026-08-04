"""Post-pivot gap audit (Item C): pull-based observation recheck.

Pre-pivot the edit-paragraph endpoint re-ran the observers per edit so fixed
findings auto-cleared from the QA panel; the pivot left the panel
manual-dismiss-only. `recheck_body_observations` restores the loop, pull-based.
Contracts pinned here:

  service (services/observations.py):
    - a fixed finding auto-clears; a still-present finding survives without
      duplication; a dismissed still-present finding keeps its dismissed_at.
    - scoped replace: only BODY_RECHECK_KINDS rows are owned; translate-time
      kinds (tm_inconsistency, glossary_merge_error, paragraph_count_drift,
      translation_degraded, missing_title_glossary_term) and title-targeted
      glossary_predicate_loss rows are untouched.
    - BODY_RECHECK_KINDS covers exactly what body_correctness_observations
      can normalize to, and the title carve-out LIKE matches the real
      detect_glossary_predicate_loss title-mode message.
    - disabled_observers honored; displayed body (refined over draft) is the
      checked text; empty body no-ops; 'translating' raises busy.

  route (routes/observations.py):
    - POST .../observations/recheck returns the refreshed panel payload with
      the same shape as the GET; 409 {message, error_kind} while
      translating; 404 on unknown chapter.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import init_db, open_conn
from backend.models import GlossaryEntry
from backend.services import observations as obs_svc
from backend.services.observations import (
    BODY_RECHECK_KINDS,
    ObservationRecheckBusyError,
    normalize_observer_outputs,
    recheck_body_observations,
)
from backend.services.text_observers import detect_glossary_predicate_loss

DB_PATH = Path(os.environ["DB_PATH"])

TERM_ZH = "昂霄"
TERM_EN = "Soaring Firmament"
SOURCE_WITH_TERM = f"他远远看去，{TERM_ZH}静静矗立。"
BODY_MISSING_TERM = "He looked from afar; the ancient peak stood silent."
BODY_WITH_TERM = f"He looked from afar; {TERM_EN} stood silent."


def _glossary(novel_id: int) -> list[GlossaryEntry]:
    return [
        GlossaryEntry(
            id=1, novel_id=novel_id, term_zh=TERM_ZH, term_en=TERM_EN,
            category="place", notes=None, auto_detected=False, locked=True,
        )
    ]


async def _reset_db() -> None:
    async with open_conn() as conn:
        for table in (
            "chapter_observations", "glossary_entries", "chapters", "novels",
        ):
            try:
                await conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        await conn.commit()


@pytest.fixture(autouse=True)
async def fresh_db():
    await init_db()
    await _reset_db()
    yield
    await _reset_db()


async def _seed_chapter(
    *,
    translated_text: str | None = BODY_MISSING_TERM,
    refined_text: str | None = None,
    status: str = "done",
    disabled_observers: str | None = None,
) -> tuple[int, int]:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, disabled_observers) "
            "VALUES ('N', 'paste', ?)",
            (disabled_observers,),
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, refined_text, status) VALUES (?, 1, ?, ?, ?, ?)",
            (novel_id, SOURCE_WITH_TERM, translated_text, refined_text, status),
        )
        chapter_id = cur.lastrowid
        await conn.commit()
    return novel_id, chapter_id


async def _chapter_row(chapter_id: int):
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT id, novel_id, status, original_text, translated_text, "
            "refined_text FROM chapters WHERE id = ?",
            (chapter_id,),
        )
        return await cur.fetchone()


async def _run_recheck(novel_id: int, chapter_id: int) -> bool:
    row = await _chapter_row(chapter_id)
    async with open_conn() as conn:
        ran = await recheck_body_observations(conn, row, _glossary(novel_id))
        await conn.commit()
    return ran


async def _obs_rows(chapter_id: int) -> list[dict]:
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT kind, excerpt, dismissed_at FROM chapter_observations "
            "WHERE chapter_id = ? ORDER BY id",
            (chapter_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Core replace semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fixed_finding_auto_clears():
    novel_id, chapter_id = await _seed_chapter()
    assert await _run_recheck(novel_id, chapter_id) is True
    rows = await _obs_rows(chapter_id)
    assert [r["kind"] for r in rows] == ["missing_glossary_term"]

    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapters SET translated_text = ? WHERE id = ?",
            (BODY_WITH_TERM, chapter_id),
        )
        await conn.commit()
    assert await _run_recheck(novel_id, chapter_id) is True
    assert await _obs_rows(chapter_id) == []


@pytest.mark.asyncio
async def test_still_present_finding_survives_without_duplication():
    novel_id, chapter_id = await _seed_chapter()
    await _run_recheck(novel_id, chapter_id)
    await _run_recheck(novel_id, chapter_id)
    rows = await _obs_rows(chapter_id)
    assert len(rows) == 1
    assert rows[0]["kind"] == "missing_glossary_term"
    assert TERM_ZH in rows[0]["excerpt"]


@pytest.mark.asyncio
async def test_dismissed_still_present_finding_stays_dismissed():
    novel_id, chapter_id = await _seed_chapter()
    await _run_recheck(novel_id, chapter_id)
    async with open_conn() as conn:
        await conn.execute(
            "UPDATE chapter_observations SET dismissed_at = datetime('now') "
            "WHERE chapter_id = ?",
            (chapter_id,),
        )
        await conn.commit()
    await _run_recheck(novel_id, chapter_id)
    rows = await _obs_rows(chapter_id)
    assert len(rows) == 1
    assert rows[0]["dismissed_at"] is not None


@pytest.mark.asyncio
async def test_translate_time_kinds_and_title_rows_untouched():
    novel_id, chapter_id = await _seed_chapter(translated_text=BODY_WITH_TERM)
    title_predicate = (
        f'Predicate loss near "{TERM_EN}" in chapter title: source segment '
        f'"再遇{TERM_ZH}" carries the action encounter/meet.'
    )
    body_predicate = (
        f'Predicate loss near "{TERM_EN}" in chapter body: source segment '
        f'"再遇{TERM_ZH}" carries the action encounter/meet.'
    )
    keep = [
        ("tm_inconsistency", "TM inconsistency: rendering drift."),
        ("glossary_merge_error", "Glossary auto-merge failed."),
        ("paragraph_count_drift", "Paragraph count drift (translation): 3 vs 4."),
        ("translation_degraded", "Fallback path used."),
        ("missing_title_glossary_term", f"missing title glossary term {TERM_ZH!r}"),
        ("glossary_predicate_loss", title_predicate),
    ]
    async with open_conn() as conn:
        for kind, excerpt in [*keep, ("glossary_predicate_loss", body_predicate)]:
            await conn.execute(
                "INSERT INTO chapter_observations (chapter_id, kind, excerpt) "
                "VALUES (?, ?, ?)",
                (chapter_id, kind, excerpt),
            )
        await conn.commit()

    await _run_recheck(novel_id, chapter_id)
    rows = await _obs_rows(chapter_id)
    # The stale body-labeled predicate row is owned (and cleared: the current
    # body carries no predicate loss); every translate-time row survives.
    assert [(r["kind"], r["excerpt"]) for r in rows] == keep


@pytest.mark.asyncio
async def test_title_carveout_matches_real_observer_message():
    """Pin the LIKE carve-out against the actual title-mode message shape:
    if detect_glossary_predicate_loss ever reformats its output, this fails
    before the carve-out silently stops matching."""
    issues = detect_glossary_predicate_loss(
        f"再遇{TERM_ZH}", f"{TERM_EN} Once Again", _glossary(1),
        source_label="chapter title",
    )
    assert issues, "fixture stopped firing the title-mode predicate observer"
    assert " in chapter title: " in issues[0]
    assert obs_svc._TITLE_PREDICATE_EXCERPT_LIKE == "% in chapter title: %"


@pytest.mark.asyncio
async def test_body_recheck_kinds_cover_normalizer_taxonomy():
    """Every message shape body_correctness_observations emits must normalize
    into BODY_RECHECK_KINDS (else recheck would insert rows it can never
    clear); the translate-time kinds must stay outside it."""
    samples = [
        "missing locked glossary term '昂霄' → 'Soaring Firmament'",
        "mt-texture tics: \"could not help but\" (4x)",
        "residual CJK in output: '昂霄' (1x)",           # 'observation' fallback
        "What-cleft structure(s): \"What he wanted was\"",  # fallback
        "Orphan 'Which' clause(s): \"Which meant\"",        # fallback
        "Double possessive on a name: \"Sea's Roar's\"",
        "Intensifier inflation on a locked glossary term: \"mighty X\"",
        "Mid-sentence paragraph break(s) detected",
        'Predicate loss near "Soaring Firmament" in chapter body: ...',
    ]
    kinds = {o.kind for o in normalize_observer_outputs(samples)}
    assert kinds <= BODY_RECHECK_KINDS
    assert kinds >= {
        "missing_glossary_term", "mt_texture", "observation",
        "double_possessive", "intensifier_inflation",
        "mid_sentence_paragraph_break", "glossary_predicate_loss",
    }
    for translate_only in (
        "tm_inconsistency", "glossary_merge_error", "paragraph_count_drift",
        "translation_degraded", "missing_title_glossary_term",
    ):
        assert translate_only not in BODY_RECHECK_KINDS


# ---------------------------------------------------------------------------
# Guards + inputs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_observers_honored():
    novel_id, chapter_id = await _seed_chapter(
        disabled_observers='["missing_glossary_term"]',
    )
    await _run_recheck(novel_id, chapter_id)
    assert await _obs_rows(chapter_id) == []


@pytest.mark.asyncio
async def test_recheck_checks_displayed_refined_body():
    """The draft still misses the term but the displayed (refined) body has
    it: the recheck must see what the reader sees and flag nothing."""
    novel_id, chapter_id = await _seed_chapter(
        translated_text=BODY_MISSING_TERM, refined_text=BODY_WITH_TERM,
    )
    await _run_recheck(novel_id, chapter_id)
    assert await _obs_rows(chapter_id) == []


@pytest.mark.asyncio
async def test_translating_chapter_raises_busy():
    novel_id, chapter_id = await _seed_chapter(status="translating")
    row = await _chapter_row(chapter_id)
    async with open_conn() as conn:
        with pytest.raises(ObservationRecheckBusyError):
            await recheck_body_observations(conn, row, _glossary(novel_id))


@pytest.mark.asyncio
async def test_empty_body_noops_and_keeps_rows():
    novel_id, chapter_id = await _seed_chapter(
        translated_text=None, status="pending",
    )
    async with open_conn() as conn:
        await conn.execute(
            "INSERT INTO chapter_observations (chapter_id, kind, excerpt) "
            "VALUES (?, 'mt_texture', 'stale but untouchable: no body')",
            (chapter_id,),
        )
        await conn.commit()
    assert await _run_recheck(novel_id, chapter_id) is False
    rows = await _obs_rows(chapter_id)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    if DB_PATH.exists():
        DB_PATH.unlink()

    async def _no_probe(default_provider):
        return None

    async def _no_drain():
        return None

    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)

    from backend.main import app

    with TestClient(app) as c:
        yield c


def _seed_http(status: str = "done") -> tuple[int, int]:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('N', 'paste')"
        )
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status) VALUES (?, 1, ?, ?, ?)",
            (novel_id, SOURCE_WITH_TERM, BODY_MISSING_TERM, status),
        )
        chapter_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO glossary_entries (novel_id, term_zh, term_en, "
            "category, auto_detected, locked) VALUES (?, ?, ?, 'place', 0, 1)",
            (novel_id, TERM_ZH, TERM_EN),
        )
        # A stale finding the current body no longer justifies.
        conn.execute(
            "INSERT INTO chapter_observations (chapter_id, kind, excerpt) "
            "VALUES (?, 'double_possessive', 'stale: Sea''s Roar''s blade')",
            (chapter_id,),
        )
        conn.commit()
        return novel_id, chapter_id
    finally:
        conn.close()


def test_recheck_route_returns_refreshed_get_shape(client: TestClient) -> None:
    novel_id, _chapter_id = _seed_http()
    r = client.post(f"/api/novels/{novel_id}/chapters/1/observations/recheck")
    assert r.status_code == 200
    rows = r.json()
    # The stale double_possessive cleared; the real miss is present.
    assert [row["kind"] for row in rows] == ["missing_glossary_term"]
    get_rows = client.get(
        f"/api/novels/{novel_id}/chapters/1/observations"
    ).json()
    assert rows == get_rows  # same payload shape AND content as the GET
    assert set(rows[0]) == {
        "id", "chapter_id", "kind", "severity", "severity_tier",
        "paragraph_index", "excerpt", "created_at", "dismissed_at",
    }
    assert rows[0]["severity_tier"] == "semantic"


def test_recheck_route_409_while_translating(client: TestClient) -> None:
    novel_id, _chapter_id = _seed_http(status="translating")
    r = client.post(f"/api/novels/{novel_id}/chapters/1/observations/recheck")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error_kind"] == "chapter_translating"
    assert "translating" in detail["message"]


def test_recheck_route_404_unknown_chapter(client: TestClient) -> None:
    novel_id, _chapter_id = _seed_http()
    r = client.post(f"/api/novels/{novel_id}/chapters/99/observations/recheck")
    assert r.status_code == 404
