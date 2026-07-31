"""HTTP-level tests for GET /novels/{id}/chapters/{n}/segments.

Service behavior (alignment, self-heal, displayed-body routing) is covered by
test_segments_service.py; this pins the route reshaping into the
SegmentListResponse Pydantic model, the 404 contract, the per-chapter-status
payload shapes, and that the lazy backfill performed on the GET is actually
COMMITTED (a later reader sees the stored rows and the stamped state).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DB_PATH = Path(os.environ["DB_PATH"])

_SRC = "甲甲甲甲甲甲甲甲甲。\n\n乙乙乙乙乙乙乙乙乙。\n\n丙丙丙丙丙丙丙丙丙。"
_TGT = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph here."


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


def _seed_chapter(
    original: str = _SRC,
    translated: str | None = _TGT,
    status: str = "done",
    error_msg: str | None = None,
) -> tuple[int, int]:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO novels (title, source_type) VALUES ('N', 'paste')")
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, title_zh, title_en, original_text, "
            " translated_text, status, error_msg) "
            "VALUES (?, 1, '第一章 起始', 'Chapter 1: Start', ?, ?, ?, ?)",
            (novel_id, original, translated, status, error_msg),
        )
        chapter_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return novel_id, chapter_id


def test_done_chapter_full_payload_shape(client):
    novel_id, _ = _seed_chapter()
    r = client.get(f"/api/novels/{novel_id}/chapters/1/segments")
    assert r.status_code == 200
    data = r.json()
    assert data["chapter_num"] == 1
    assert data["title_en"] == "Chapter 1: Start"
    assert data["title_zh"] == "第一章 起始"
    assert data["chapter_status"] == "done"
    assert data["variant"] == "draft"
    assert data["segments_state"] == "ok"
    assert data["aligned"] is True
    assert data["progress"] == {"confirmed": 0, "total": 3}
    assert data["next_unconfirmed_index"] == 0
    assert isinstance(data["chapter_rev"], str) and len(data["chapter_rev"]) == 16
    assert len(data["segments"]) == 3
    seg = data["segments"][0]
    for field in ("index", "source_text", "target_text", "source_hash",
                  "target_hash", "status", "origin", "aligned", "confirmed_at"):
        assert field in seg
    assert len(seg["source_hash"]) == 16
    assert len(seg["target_hash"]) == 16
    assert seg["status"] == "machine"
    assert seg["origin"] == "aligned_backfill"


def test_pending_chapter_status_only(client):
    novel_id, _ = _seed_chapter(translated=None, status="pending")
    r = client.get(f"/api/novels/{novel_id}/chapters/1/segments")
    assert r.status_code == 200
    data = r.json()
    assert data["chapter_status"] == "pending"
    assert data["segments"] == []
    assert data["segments_state"] is None
    assert data["chapter_rev"] is None
    assert data["progress"] == {"confirmed": 0, "total": 0}
    assert data["next_unconfirmed_index"] is None


def test_translating_and_error_chapters_status_only(client):
    for status in ("translating", "error"):
        novel_id, chapter_id = _seed_chapter(translated=None, status=status,
                                             error_msg="boom" if status == "error" else None)
        r = client.get(f"/api/novels/{novel_id}/chapters/1/segments")
        assert r.status_code == 200
        data = r.json()
        assert data["chapter_status"] == status
        assert data["segments"] == []
        # Status-only reads never build rows.
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute(
            "SELECT COUNT(*) FROM chapter_segments WHERE chapter_id = ?",
            (chapter_id,),
        ).fetchone()[0]
        conn.close()
        assert n == 0


def test_unaligned_chapter_shape(client):
    src = "\n\n".join("甲乙丙丁戊己"[i] * 9 + "。" for i in range(6))
    novel_id, chapter_id = _seed_chapter(original=src,
                                         translated="One lonely paragraph.")
    r = client.get(f"/api/novels/{novel_id}/chapters/1/segments")
    assert r.status_code == 200
    data = r.json()
    assert data["chapter_status"] == "done"
    assert data["segments_state"] == "unaligned"
    assert data["aligned"] is False
    assert data["segments"] == []
    # The unaligned verdict is persisted for the UI's retranslate CTA.
    conn = sqlite3.connect(DB_PATH)
    state = conn.execute(
        "SELECT segments_state FROM chapters WHERE id = ?", (chapter_id,)
    ).fetchone()[0]
    conn.close()
    assert state == "unaligned"


def test_404_for_missing_chapter_and_novel(client):
    novel_id, _ = _seed_chapter()
    assert client.get(f"/api/novels/{novel_id}/chapters/99/segments").status_code == 404
    assert client.get(f"/api/novels/{novel_id + 1000}/chapters/1/segments").status_code == 404


# ---------------------------------------------------------------------------
# Phase 3: PATCH + confirm-all
# ---------------------------------------------------------------------------


def _get_payload(client, novel_id):
    r = client.get(f"/api/novels/{novel_id}/chapters/1/segments")
    assert r.status_code == 200
    return r.json()


def _patch_seg(client, novel_id, data, seg_index, action, after=None,
               rev=None, before_hash=None):
    seg = next(
        (s for s in data["segments"] if s["index"] == seg_index), None
    )
    body = {
        "action": action,
        "chapter_rev": rev if rev is not None else data["chapter_rev"],
        "before_target_hash": (
            before_hash if before_hash is not None
            else (seg["target_hash"] if seg else "0" * 16)
        ),
    }
    if after is not None:
        body["after_text"] = after
    return client.patch(
        f"/api/novels/{novel_id}/chapters/1/segments/{seg_index}", json=body
    )


def test_patch_save_response_shape_and_persistence(client):
    novel_id, chapter_id = _seed_chapter()
    data = _get_payload(client, novel_id)
    r = _patch_seg(client, novel_id, data, 1, "save", after="Edited middle.")
    assert r.status_code == 200, r.text
    out = r.json()
    seg = out["segment"]
    assert seg["index"] == 1
    assert seg["status"] == "edited"
    assert seg["origin"] == "human"
    assert seg["target_text"] == "Edited middle."
    assert len(seg["target_hash"]) == 16
    assert out["segments_state"] == "ok"
    assert out["progress"] == {"confirmed": 0, "total": 3}
    assert out["next_unconfirmed_index"] == 0
    assert len(out["chapter_rev"]) == 16
    assert out["chapter_rev"] != data["chapter_rev"]
    # The write committed: body + segment row visible outside the request.
    conn = sqlite3.connect(DB_PATH)
    body, = conn.execute(
        "SELECT translated_text FROM chapters WHERE id = ?", (chapter_id,)
    ).fetchone()
    status, = conn.execute(
        "SELECT status FROM chapter_segments "
        "WHERE chapter_id = ? AND seg_index = 1",
        (chapter_id,),
    ).fetchone()
    conn.close()
    assert body == (
        "First paragraph here.\n\nEdited middle.\n\nThird paragraph here."
    )
    assert status == "edited"


def test_patch_confirm_and_unconfirm(client):
    novel_id, _ = _seed_chapter()
    data = _get_payload(client, novel_id)
    r = _patch_seg(client, novel_id, data, 0, "confirm")
    assert r.status_code == 200
    assert r.json()["segment"]["status"] == "confirmed"
    assert r.json()["progress"] == {"confirmed": 1, "total": 3}
    # chapter_rev unchanged by a non-text action; reuse it to unconfirm.
    data = _get_payload(client, novel_id)
    r = _patch_seg(client, novel_id, data, 0, "unconfirm")
    assert r.status_code == 200
    assert r.json()["segment"]["status"] == "edited"


def test_patch_save_and_confirm_then_revert(client):
    novel_id, _ = _seed_chapter()
    data = _get_payload(client, novel_id)
    r = _patch_seg(client, novel_id, data, 2, "save_and_confirm",
                   after="Confirmed rewrite.")
    assert r.status_code == 200
    assert r.json()["segment"]["status"] == "confirmed"
    data = _get_payload(client, novel_id)
    r = _patch_seg(client, novel_id, data, 2, "revert_machine")
    assert r.status_code == 200
    seg = r.json()["segment"]
    assert seg["status"] == "machine"
    assert seg["target_text"] == "Third paragraph here."


def test_patch_400s(client):
    novel_id, _ = _seed_chapter()
    data = _get_payload(client, novel_id)
    assert _patch_seg(client, novel_id, data, 0, "explode").status_code == 400
    assert _patch_seg(
        client, novel_id, data, 0, "save", after="   "
    ).status_code == 400
    assert _patch_seg(client, novel_id, data, 0, "unconfirm").status_code == 400
    assert _patch_seg(
        client, novel_id, data, 0, "revert_machine"
    ).status_code == 400


def test_patch_409_stale_chapter(client):
    novel_id, _ = _seed_chapter()
    data = _get_payload(client, novel_id)
    r = _patch_seg(client, novel_id, data, 0, "save", after="x", rev="f" * 16)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error_kind"] == "stale_chapter"
    assert detail["message"]


def test_patch_409_stale_segment(client):
    novel_id, _ = _seed_chapter()
    data = _get_payload(client, novel_id)
    r = _patch_seg(client, novel_id, data, 0, "save", after="x",
                   before_hash="f" * 16)
    assert r.status_code == 409
    assert r.json()["detail"]["error_kind"] == "stale_segment"


def test_patch_409_chapter_translating(client):
    novel_id, chapter_id = _seed_chapter()
    data = _get_payload(client, novel_id)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE chapters SET status = 'translating' WHERE id = ?",
        (chapter_id,),
    )
    conn.commit()
    conn.close()
    r = _patch_seg(client, novel_id, data, 0, "save", after="x")
    assert r.status_code == 409
    assert r.json()["detail"]["error_kind"] == "chapter_translating"


def test_patch_404s(client):
    novel_id, _ = _seed_chapter()
    data = _get_payload(client, novel_id)
    r = _patch_seg(client, novel_id + 1000, data, 0, "save", after="x")
    assert r.status_code == 404
    r = _patch_seg(client, novel_id, data, 99, "save", after="x")
    assert r.status_code == 404


def test_confirm_all_route(client):
    novel_id, chapter_id = _seed_chapter()
    data = _get_payload(client, novel_id)
    r = client.post(
        f"/api/novels/{novel_id}/chapters/1/segments/confirm-all",
        json={"chapter_rev": data["chapter_rev"]},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["confirmed"] == 3
    assert out["progress"] == {"confirmed": 3, "total": 3}
    assert out["next_unconfirmed_index"] is None
    conn = sqlite3.connect(DB_PATH)
    n, = conn.execute(
        "SELECT COUNT(*) FROM chapter_segments "
        "WHERE chapter_id = ? AND status = 'confirmed'",
        (chapter_id,),
    ).fetchone()
    conn.close()
    assert n == 3


def test_confirm_all_route_409_and_400(client):
    novel_id, _ = _seed_chapter()
    _get_payload(client, novel_id)
    r = client.post(
        f"/api/novels/{novel_id}/chapters/1/segments/confirm-all",
        json={"chapter_rev": "f" * 16},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error_kind"] == "stale_chapter"
    data = _get_payload(client, novel_id)
    r = client.post(
        f"/api/novels/{novel_id}/chapters/1/segments/confirm-all",
        json={"chapter_rev": data["chapter_rev"], "statuses": ["confirmed"]},
    )
    assert r.status_code == 400


def test_lazy_backfill_is_persisted(client):
    novel_id, chapter_id = _seed_chapter()
    first = client.get(f"/api/novels/{novel_id}/chapters/1/segments").json()
    assert first["segments_state"] == "ok"

    # The GET's writes were committed: rows + state visible on a fresh
    # connection outside the request.
    conn = sqlite3.connect(DB_PATH)
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM chapter_segments WHERE chapter_id = ? ORDER BY seg_index",
        (chapter_id,),
    ).fetchall()]
    state, version = conn.execute(
        "SELECT segments_state, segmentation_version FROM chapters WHERE id = ?",
        (chapter_id,),
    ).fetchone()
    conn.close()
    assert len(ids) == 3
    assert state == "ok"
    assert version is not None

    # A second GET serves the STORED rows (ids stable, no rebuild).
    second = client.get(f"/api/novels/{novel_id}/chapters/1/segments").json()
    assert second == first
    conn = sqlite3.connect(DB_PATH)
    ids_after = [r[0] for r in conn.execute(
        "SELECT id FROM chapter_segments WHERE chapter_id = ? ORDER BY seg_index",
        (chapter_id,),
    ).fetchall()]
    conn.close()
    assert ids_after == ids
