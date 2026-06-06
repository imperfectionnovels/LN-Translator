"""Tests for POST /api/translate/insert/{novel_id}: inserting a chapter into
the MIDDLE of a novel (filling one missed during import).

Covers:
- gap-fill: an existing numbering gap at the target slot is filled with NO
  tail renumbering
- mid-novel insert: the tail shifts up by the insert count, reading order is
  preserved, UNIQUE(novel_id, chapter_num) holds, and shifted bodies move with
  their rows (chapters.id is stable)
- insert at the very front (after_chapter_num=0)
- multi-chapter paste insert
- child rows keyed on chapters.id (bookmarks) survive a renumber unchanged
- novels.last_read_chapter_num bumps only when it sits at/after the insert
- out-of-range after_chapter_num -> 400
"""

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import SCHEMA
from backend.main import app

DB_PATH = Path(os.environ["DB_PATH"])

BODY = "正文内容。" * 80  # comfortably above MIN_CHAPTER_CHARS so each heading stands alone


@pytest.fixture
def client(monkeypatch):
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    # Keep any spawned worker away from a real translator.
    async def _fake_translate(original, title_zh, glossary, **kwargs):
        from backend.models import TranslationResult
        return TranslationResult(title_en="EN", translated_text="translated", new_terms=[])

    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake_translate)
    with TestClient(app) as c:
        yield c


def _rows(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _exec(sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _nums(novel_id: int) -> list[int]:
    return [
        r["chapter_num"]
        for r in _rows(
            "SELECT chapter_num FROM chapters WHERE novel_id = ? ORDER BY chapter_num",
            (novel_id,),
        )
    ]


def _make_novel(client: TestClient, headings: list[str]) -> int:
    """Create a novel via the paste route from the given chapter headings."""
    text = "\n\n".join(f"{h}\n{BODY}" for h in headings)
    r = client.post("/api/translate/paste", json={"title": "T", "text": text})
    assert r.status_code == 200, r.text
    return r.json()["novel_id"]


def _insert(client: TestClient, novel_id: int, after: int, text: str, title=None):
    body = {"after_chapter_num": after, "text": text}
    if title is not None:
        body["title"] = title
    return client.post(f"/api/translate/insert/{novel_id}", json=body)


# --------------------------------------------------------------------------- #


def test_insert_fills_gap_without_renumbering(client: TestClient) -> None:
    """A genuine numbering gap at the target slot is filled directly; later
    chapters keep their numbers and their bodies."""
    nid = _make_novel(client, ["第一章 甲", "第二章 乙", "第四章 丁"])  # nums 1,2,4 (gap at 3)
    assert _nums(nid) == [1, 2, 4]
    body4_before = _rows(
        "SELECT id, original_text FROM chapters WHERE novel_id=? AND chapter_num=4", (nid,)
    )[0]

    r = _insert(client, nid, after=2, text=f"第三章 新\n{BODY}")
    assert r.status_code == 200, r.text
    assert r.json()["added_chapters"] == 1
    assert r.json()["first_new_chapter"] == 3

    assert _nums(nid) == [1, 2, 3, 4]
    body4_after = _rows(
        "SELECT id, original_text FROM chapters WHERE novel_id=? AND chapter_num=4", (nid,)
    )[0]
    # Chapter 4 untouched: same row id, same body (no shift happened).
    assert body4_after["id"] == body4_before["id"]
    assert body4_after["original_text"] == body4_before["original_text"]
    new = _rows("SELECT status FROM chapters WHERE novel_id=? AND chapter_num=3", (nid,))[0]
    assert new["status"] == "pending"


def test_insert_mid_novel_shifts_tail(client: TestClient) -> None:
    """No gap: inserting after 1 shifts 2->3, 3->4 and lands the new chapter
    at 2. Bodies move with their stable row ids; numbers stay unique."""
    nid = _make_novel(client, ["第一章 甲", "第二章 乙", "第三章 丙"])
    id_old2 = _rows("SELECT id FROM chapters WHERE novel_id=? AND chapter_num=2", (nid,))[0]["id"]
    id_old3 = _rows("SELECT id FROM chapters WHERE novel_id=? AND chapter_num=3", (nid,))[0]["id"]

    r = _insert(client, nid, after=1, text=f"插入\n{BODY}")
    assert r.status_code == 200, r.text
    assert r.json()["first_new_chapter"] == 2

    assert _nums(nid) == [1, 2, 3, 4]
    assert len(set(_nums(nid))) == 4  # UNIQUE holds
    # The old chapter rows kept their ids but their numbers shifted up by 1.
    assert _rows("SELECT chapter_num FROM chapters WHERE id=?", (id_old2,))[0]["chapter_num"] == 3
    assert _rows("SELECT chapter_num FROM chapters WHERE id=?", (id_old3,))[0]["chapter_num"] == 4
    # The new chapter sits at 2 and is the only fresh row.
    new = _rows("SELECT id, status FROM chapters WHERE novel_id=? AND chapter_num=2", (nid,))[0]
    assert new["id"] not in (id_old2, id_old3)
    assert new["status"] == "pending"


def test_insert_at_front(client: TestClient) -> None:
    """after_chapter_num=0 inserts before chapter 1, shifting everything up."""
    nid = _make_novel(client, ["第一章 甲", "第二章 乙"])
    r = _insert(client, nid, after=0, text=f"序\n{BODY}", title="Prologue")
    assert r.status_code == 200, r.text
    assert r.json()["first_new_chapter"] == 1
    assert _nums(nid) == [1, 2, 3]
    front = _rows(
        "SELECT title_zh FROM chapters WHERE novel_id=? AND chapter_num=1", (nid,)
    )[0]
    assert front["title_zh"] == "Prologue"  # user title override applied


def test_insert_multiple_chapters(client: TestClient) -> None:
    """A multi-heading paste inserts as a contiguous block; the tail shifts by
    the count."""
    nid = _make_novel(client, ["第一章 甲", "第二章 乙", "第三章 丙"])
    r = _insert(client, nid, after=1, text=f"第二章 X\n{BODY}\n\n第三章 Y\n{BODY}")
    assert r.status_code == 200, r.text
    assert r.json()["added_chapters"] == 2
    assert r.json()["first_new_chapter"] == 2
    assert _nums(nid) == [1, 2, 3, 4, 5]


def test_insert_into_partial_gap_shifts(client: TestClient) -> None:
    """Two-chapter insert where only the first target slot is free still shifts
    (the whole window must be clear), staying collision-free."""
    nid = _make_novel(client, ["第一章 甲", "第二章 乙", "第四章 丁"])  # 1,2,4 (gap at 3)
    r = _insert(client, nid, after=2, text=f"第二章 X\n{BODY}\n\n第三章 Y\n{BODY}")  # count=2, target=3
    assert r.status_code == 200, r.text
    # slot 3 free but window [3,5) hits existing 4 -> shift 4->6, insert 3,4.
    assert _nums(nid) == [1, 2, 3, 4, 6]


def test_insert_preserves_bookmark_by_chapter_id(client: TestClient) -> None:
    """A bookmark keyed on chapters.id keeps pointing at the same chapter after
    a renumber (the row id is stable; only chapter_num shifts)."""
    nid = _make_novel(client, ["第一章 甲", "第二章 乙", "第三章 丙"])
    id_ch3 = _rows("SELECT id FROM chapters WHERE novel_id=? AND chapter_num=3", (nid,))[0]["id"]
    _exec(
        "INSERT INTO bookmarks (novel_id, chapter_id, paragraph_index, note) VALUES (?,?,?,?)",
        (nid, id_ch3, 0, "keep me"),
    )

    r = _insert(client, nid, after=1, text=f"插入\n{BODY}")
    assert r.status_code == 200, r.text

    bm = _rows("SELECT chapter_id, note FROM bookmarks WHERE novel_id=?", (nid,))[0]
    assert bm["chapter_id"] == id_ch3  # unchanged
    assert bm["note"] == "keep me"
    # That same row is now chapter 4.
    assert _rows("SELECT chapter_num FROM chapters WHERE id=?", (id_ch3,))[0]["chapter_num"] == 4


def test_insert_bumps_last_read_only_when_at_or_after(client: TestClient) -> None:
    """last_read_chapter_num shifts with the renumber when it sits at/after the
    insert point, and stays put when it sits before it."""
    nid = _make_novel(client, ["第一章 甲", "第二章 乙", "第三章 丙"])

    # Reader is on chapter 3; insert after 1 (target=2) -> chapter 3 becomes 4.
    _exec("UPDATE novels SET last_read_chapter_num=3 WHERE id=?", (nid,))
    r = _insert(client, nid, after=1, text=f"插入\n{BODY}")
    assert r.status_code == 200, r.text
    lr = _rows("SELECT last_read_chapter_num FROM novels WHERE id=?", (nid,))[0]
    assert lr["last_read_chapter_num"] == 4

    # Reader is on chapter 1; insert after 4 (at the end) -> position unchanged.
    _exec("UPDATE novels SET last_read_chapter_num=1 WHERE id=?", (nid,))
    r2 = _insert(client, nid, after=4, text=f"末\n{BODY}")
    assert r2.status_code == 200, r2.text
    lr2 = _rows("SELECT last_read_chapter_num FROM novels WHERE id=?", (nid,))[0]
    assert lr2["last_read_chapter_num"] == 1


def test_insert_out_of_range_rejected(client: TestClient) -> None:
    """after_chapter_num beyond MAX(chapter_num) is a 400; the novel is
    untouched."""
    nid = _make_novel(client, ["第一章 甲", "第二章 乙", "第三章 丙"])
    r = _insert(client, nid, after=9, text=f"x\n{BODY}")
    assert r.status_code == 400, r.text
    assert _nums(nid) == [1, 2, 3]  # no partial write


def test_insert_unknown_novel_404(client: TestClient) -> None:
    r = _insert(client, 999, after=0, text=f"x\n{BODY}")
    assert r.status_code == 404, r.text
