"""Section 6.2: pre-translation local checks.

Verifies chapter_pre_check flags the cases the plan called out:
- empty / tiny body (length sanity)
- glossary saturation (proper nouns not yet glossed)
- unbalanced punctuation (truncated scrape)
- low CJK ratio (boilerplate / paywall page mis-scraped)
"""

from __future__ import annotations

import pytest

from backend.db import init_db, open_conn
from backend.services.pre_check import chapter_pre_check


async def _reset() -> None:
    async with open_conn() as conn:
        for t in ("chapters", "novels", "glossary_entries"):
            try:
                await conn.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        await conn.commit()


@pytest.fixture(autouse=True)
async def fresh_db():
    await init_db()
    await _reset()
    yield
    await _reset()


async def _make_novel_with_body(body: str) -> tuple[int, int]:
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, ?)",
            ("t", "paste"),
        )
        novel_id = cur.lastrowid
        await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, status) "
            "VALUES (?, 1, ?, 'pending')",
            (novel_id, body),
        )
        await conn.commit()
    return novel_id, 1


def _codes(ws: list[dict]) -> set[str]:
    return {w["code"] for w in ws}


async def test_empty_body_flagged_as_alert():
    novel_id, chap = await _make_novel_with_body("   ")
    async with open_conn() as conn:
        ws = await chapter_pre_check(conn, novel_id, chap)
    codes = _codes(ws)
    assert "empty_body" in codes
    severities = {w["severity"] for w in ws if w["code"] == "empty_body"}
    assert severities == {"alert"}


async def test_tiny_body_flagged_as_warn():
    novel_id, chap = await _make_novel_with_body("Short row.")
    async with open_conn() as conn:
        ws = await chapter_pre_check(conn, novel_id, chap)
    assert "tiny_body" in _codes(ws)


async def test_normal_chapter_no_flags():
    """Healthy CJK chapter with balanced punctuation and one proper noun
    repeated — no flags. The candidate-terms threshold (≥6) keeps a
    single recurring name from tripping the saturation warning."""
    body = "李青云走入山中。" * 40  # ~600 chars, all CJK, balanced.
    novel_id, chap = await _make_novel_with_body(body)
    async with open_conn() as conn:
        ws = await chapter_pre_check(conn, novel_id, chap)
    assert ws == []


async def test_low_cjk_ratio_flagged():
    """A 'chapter' that's mostly English (a paywall page or boilerplate)
    trips low_cjk_ratio."""
    body = (
        "Sign up for our newsletter to read this chapter. " * 30
    )
    novel_id, chap = await _make_novel_with_body(body)
    async with open_conn() as conn:
        ws = await chapter_pre_check(conn, novel_id, chap)
    assert "low_cjk_ratio" in _codes(ws)


async def test_unbalanced_brackets_flagged():
    """A truncated 【系统】 tag — the closer is missing."""
    body = "李青云走入山中。【系统提示：检测到陌生人入侵。" + "他停下脚步打量四周。" * 30
    novel_id, chap = await _make_novel_with_body(body)
    async with open_conn() as conn:
        ws = await chapter_pre_check(conn, novel_id, chap)
    assert "unbalanced_punctuation" in _codes(ws)


async def test_glossary_candidates_flagged_when_many_proper_nouns():
    """A chapter introducing 8+ recurring proper-noun candidates with empty
    glossary trips the saturation warning. detect_candidate_terms uses a
    CJK-run regex that stops at punctuation, so the test punctuates each
    name to isolate it from surrounding context."""
    names = [
        "李青云", "陈无极", "王慕雪", "苏明远",
        "周天行", "林若霜", "顾长风", "白衍墨",
    ]
    body = ""
    for n in names:
        # Each name appears twice as a standalone token so the
        # ≥2-recurrence gate admits it.
        body += f"{n}。看着山。{n}。停下脚步。"
    novel_id, chap = await _make_novel_with_body(body)
    async with open_conn() as conn:
        ws = await chapter_pre_check(conn, novel_id, chap)
    assert "glossary_candidates" in _codes(ws)


async def test_missing_chapter_returns_empty():
    """No chapter at that (novel, num) → empty list, not a crash."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, ?)",
            ("t", "paste"),
        )
        novel_id = cur.lastrowid
        await conn.commit()
        ws = await chapter_pre_check(conn, novel_id, 999)
    assert ws == []


@pytest.mark.asyncio
async def test_pre_check_endpoint_returns_warnings(monkeypatch):
    """GET /api/novels/{id}/chapters/{n}/pre-check returns the warning list."""
    from fastapi.testclient import TestClient
    async def _no_probe(_default):
        return None
    async def _no_drain():
        return None
    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)

    novel_id, chap = await _make_novel_with_body("Short row.")

    from backend.main import app
    with TestClient(app) as client:
        resp = client.get(f"/api/novels/{novel_id}/chapters/{chap}/pre-check")
    assert resp.status_code == 200
    body = resp.json()
    assert "warnings" in body
    assert any(w["code"] == "tiny_body" for w in body["warnings"])
