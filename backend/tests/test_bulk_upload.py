"""Tests for the bulk multi-file upload endpoints.

Verifies that POST /api/translate/bulk and /append/{id}/bulk:
- create one chapter per file in upload order, with status='pending'
- do NOT auto-queue translation
- skip empty files but report the count
- reject oversize files
- offset chapter_num correctly when appending

The "queue everything pending" /start endpoint no longer exists — translation
is per-chapter now, triggered via /api/novels/{id}/chapters/{n}/retranslate.
"""

import os
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import SCHEMA
from backend.main import app

DB_PATH = Path(os.environ["DB_PATH"])


@pytest.fixture
def client(monkeypatch):
    # Reset DB
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    # Stub the translator so any background task spawned by the queue service
    # doesn't try to reach Gemini.
    async def _fake_translate(original, title_zh, glossary, **kwargs):
        from backend.models import TranslationResult
        return TranslationResult(title_en="EN", translated_text="translated", new_terms=[])

    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake_translate)

    # Stub the queue's _run_translate so it doesn't race with assertions on
    # translate_queued after a POST /retranslate. The route still sets the
    # flag synchronously via queue_translation's DB write; we just suppress
    # the worker that would clear it.
    async def _noop_run(novel_id, chapter_id):
        return

    monkeypatch.setattr("backend.services.queue._run_translate", _noop_run)

    return TestClient(app)


def _row(table_query: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(table_query, params).fetchall())
    finally:
        conn.close()


def _file(name: str, content: str) -> tuple[str, tuple[str, BytesIO, str]]:
    return ("files", (name, BytesIO(content.encode("utf-8")), "text/plain"))


def _upload_file(name: str, content: str) -> tuple[str, tuple[str, BytesIO, str]]:
    """Single-upload variant. /translate/upload declares `file: UploadFile`
    (singular field name), while /bulk reads multipart parts named "files"
    via request.form(). Using _file (plural) for /upload yields 422 because
    the required "file" field is missing."""
    return ("file", (name, BytesIO(content.encode("utf-8")), "text/plain"))


def test_bulk_creates_chapters_in_upload_order(client: TestClient) -> None:
    files = [
        _file("alpha.txt", "第一章\n内容一" * 50),
        _file("beta.txt", "第二章\n内容二" * 50),
        _file("gamma.txt", "第三章\n内容三" * 50),
    ]
    r = client.post("/api/translate/bulk", data={"title": "Test Novel"}, files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added_chapters"] == 3
    assert body["skipped_files"] == 0
    assert body["translation_queued"] is False
    novel_id = body["novel_id"]

    rows = _row(
        "SELECT chapter_num, title_zh, original_text, status, translated_text "
        "FROM chapters WHERE novel_id = ? ORDER BY chapter_num",
        (novel_id,),
    )
    assert len(rows) == 3
    assert [r["chapter_num"] for r in rows] == [1, 2, 3]
    # Each file's first line is a 第N章 heading — it becomes title_zh and is
    # stripped from the body, in preference to the filename.
    assert [r["title_zh"] for r in rows] == ["第一章", "第二章", "第三章"]
    assert all(not r["original_text"].startswith("第") for r in rows)
    assert all(r["status"] == "pending" for r in rows)
    assert all(r["translated_text"] is None for r in rows)


def test_bulk_uses_heading_over_filename_and_drops_numeric_names(
    client: TestClient,
) -> None:
    # 0001.txt has a real heading on line 1 → heading wins. 0002.txt is a bare
    # body with a numeric filename → no heading, numeric stem discarded (NULL).
    files = [
        _file("0001.txt", "第一章 启程\n" + "内容" * 80),
        _file("0002.txt", "内容" * 80 + "\n" + "更多" * 40),
    ]
    r = client.post("/api/translate/bulk", data={"title": "Numeric"}, files=files)
    assert r.status_code == 200, r.text
    novel_id = r.json()["novel_id"]
    rows = _row(
        "SELECT chapter_num, title_zh FROM chapters "
        "WHERE novel_id = ? ORDER BY chapter_num",
        (novel_id,),
    )
    assert rows[0]["title_zh"] == "第一章 启程"
    assert rows[1]["title_zh"] is None


def test_bulk_does_not_auto_translate(client: TestClient) -> None:
    files = [_file("a.txt", "原文" * 100)]
    r = client.post("/api/translate/bulk", data={"title": "Quiet"}, files=files)
    assert r.status_code == 200
    novel_id = r.json()["novel_id"]
    rows = _row("SELECT status FROM chapters WHERE novel_id = ?", (novel_id,))
    assert [row["status"] for row in rows] == ["pending"]


def test_append_bulk_offsets_chapter_nums(client: TestClient) -> None:
    files = [_file(f"ch{i}.txt", f"内容{i}" * 50) for i in range(1, 6)]
    r = client.post("/api/translate/bulk", data={"title": "Series"}, files=files)
    novel_id = r.json()["novel_id"]
    assert r.json()["added_chapters"] == 5

    more = [_file("ch6.txt", "六" * 50), _file("ch7.txt", "七" * 50)]
    r2 = client.post(f"/api/translate/append/{novel_id}/bulk", files=more)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["added_chapters"] == 2
    assert body["first_new_chapter"] == 6
    assert body["translation_queued"] is False

    rows = _row(
        "SELECT chapter_num, title_zh FROM chapters WHERE novel_id = ? ORDER BY chapter_num",
        (novel_id,),
    )
    assert [r["chapter_num"] for r in rows] == [1, 2, 3, 4, 5, 6, 7]
    assert rows[5]["title_zh"] == "ch6"
    assert rows[6]["title_zh"] == "ch7"


def test_append_bulk_uses_printed_chapter_numbers(client: TestClient) -> None:
    """When every appended file carries a 第N章 heading whose numbers sit
    above the novel's current max, the chapters land at those printed numbers
    — not max+1. A partial novel (chapters 10-14) appended with 第15章/第16章
    keeps a clean 10..16 run."""
    files = [
        _file(f"f{i}.txt", f"第{i}章 标题\n" + "内容" * 80)
        for i in range(10, 15)
    ]
    r = client.post("/api/translate/bulk", data={"title": "Partial"}, files=files)
    assert r.status_code == 200, r.text
    novel_id = r.json()["novel_id"]
    rows = _row(
        "SELECT chapter_num FROM chapters WHERE novel_id = ? ORDER BY chapter_num",
        (novel_id,),
    )
    assert [r["chapter_num"] for r in rows] == [10, 11, 12, 13, 14]

    more = [
        _file("f15.txt", "第15章 标题\n" + "内容" * 80),
        _file("f16.txt", "第16章 标题\n" + "内容" * 80),
    ]
    r2 = client.post(f"/api/translate/append/{novel_id}/bulk", files=more)
    assert r2.status_code == 200, r2.text
    assert r2.json()["first_new_chapter"] == 15

    rows = _row(
        "SELECT chapter_num FROM chapters WHERE novel_id = ? ORDER BY chapter_num",
        (novel_id,),
    )
    assert [r["chapter_num"] for r in rows] == [10, 11, 12, 13, 14, 15, 16]


def test_bulk_skips_empty_files(client: TestClient) -> None:
    files = [
        _file("first.txt", "第一章 内容" * 50),
        _file("empty.txt", ""),
        _file("third.txt", "第三章 内容" * 50),
    ]
    r = client.post("/api/translate/bulk", data={"title": "Sparse"}, files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added_chapters"] == 2
    assert body["skipped_files"] == 1
    novel_id = body["novel_id"]

    rows = _row(
        "SELECT chapter_num, title_zh FROM chapters WHERE novel_id = ? ORDER BY chapter_num",
        (novel_id,),
    )
    assert [r["chapter_num"] for r in rows] == [1, 2]
    assert [r["title_zh"] for r in rows] == ["first", "third"]


def test_bulk_skips_author_note_files(client: TestClient) -> None:
    """A heading-less file that reads as an author update post must not become
    a chapter — otherwise it takes a number and shifts every chapter after it."""
    files = [
        _file("c1.txt", "第一章 标题\n" + "正文内容" * 80),
        _file("note.txt", "求月票！\n\n大家好，今天五更放到晚上八点，求一波月票支持。"),
        _file("c2.txt", "第二章 标题\n" + "正文内容" * 80),
    ]
    r = client.post("/api/translate/bulk", data={"title": "Notes"}, files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added_chapters"] == 2
    assert body["skipped_nonchapter"] == 1
    rows = _row(
        "SELECT chapter_num FROM chapters WHERE novel_id = ? ORDER BY chapter_num",
        (body["novel_id"],),
    )
    assert [r["chapter_num"] for r in rows] == [1, 2]


def test_bulk_all_empty_rejected(client: TestClient) -> None:
    files = [_file("a.txt", ""), _file("b.txt", "")]
    r = client.post("/api/translate/bulk", data={"title": "Nothing"}, files=files)
    assert r.status_code == 400


def test_bulk_oversize_file_rejected(client: TestClient, monkeypatch) -> None:
    """Per-file MAX_UPLOAD_BYTES enforcement. Patch the cap down so the test
    doesn't have to materialise a 50 MB+ string just to trip the limit."""
    monkeypatch.setattr("backend.services.uploads.MAX_UPLOAD_BYTES", 1_000_000)
    big = "x" * 1_500_000  # > patched cap
    files = [_file("huge.txt", big)]
    r = client.post("/api/translate/bulk", data={"title": "Big"}, files=files)
    assert r.status_code == 413


def test_bulk_imports_with_zero_queued(client: TestClient) -> None:
    """Bulk upload imports chapters as raw — none should land in the queue
    automatically. The user has to click Translate on each chapter."""
    files = [_file(f"ch{i}.txt", f"内容{i}" * 50) for i in range(1, 4)]
    r = client.post("/api/translate/bulk", data={"title": "Mix"}, files=files)
    assert r.status_code == 200
    novel_id = r.json()["novel_id"]

    rows = _row(
        "SELECT status, translate_queued FROM chapters WHERE novel_id = ?",
        (novel_id,),
    )
    assert len(rows) == 3
    for row in rows:
        assert row["status"] == "pending"
        assert row["translate_queued"] == 0


def test_retranslate_queues_one_chapter(client: TestClient) -> None:
    """POST /retranslate flips translate_queued=1 on exactly one chapter."""
    files = [_file(f"ch{i}.txt", f"内容{i}" * 50) for i in range(1, 4)]
    r = client.post("/api/translate/bulk", data={"title": "QueueOne"}, files=files)
    novel_id = r.json()["novel_id"]

    r2 = client.post(f"/api/novels/{novel_id}/chapters/2/retranslate")
    assert r2.status_code == 200
    assert r2.json()["status"] == "queued"

    rows = _row(
        "SELECT chapter_num, translate_queued FROM chapters WHERE novel_id = ? "
        "ORDER BY chapter_num",
        (novel_id,),
    )
    queued = {r["chapter_num"]: r["translate_queued"] for r in rows}
    assert queued == {1: 0, 2: 1, 3: 0}


def test_bulk_no_files_rejected(client: TestClient) -> None:
    # FastAPI requires at least one file when files=List[UploadFile]=File(...);
    # sending zero files yields a 422 (Pydantic/FastAPI validation), which is acceptable.
    r = client.post("/api/translate/bulk", data={"title": "Empty"})
    assert r.status_code in (400, 422)


# --- New tests for the Phase 1 hardening (H4 / H5 / H7 / H8 / H9 / H2). ---


def test_bulk_rejects_non_txt_extension(client: TestClient) -> None:
    """H4: a .pdf/.docx/etc. file is rejected with 400. The frontend
    <input accept='.txt'> is client-side only — a curl or drag-drop could
    otherwise commit binary garbage as a chapter."""
    files = [
        _file("real.txt", "第一章 标题\n" + "内容" * 80),
        # Non-.txt extension; content shape doesn't matter, the check is on
        # the filename.
        _file("evil.pdf", "%PDF-1.4\n" + "garbage" * 100),
    ]
    r = client.post("/api/translate/bulk", data={"title": "Mix"}, files=files)
    assert r.status_code == 400, r.text
    assert ".txt" in r.json()["detail"]


def test_upload_rejects_non_txt_extension(client: TestClient) -> None:
    """H4 on the single-upload route."""
    files = [_upload_file("doc.docx", "fake docx")]
    r = client.post(
        "/api/translate/upload", data={"title": "X"}, files=files,
    )
    assert r.status_code == 400


def test_bulk_strips_volume_divider_before_heading(client: TestClient) -> None:
    """H7: a file starting with 第N卷 ... 第N章 ... must land at the printed
    chapter number with the volume line removed from the body. Before the
    fix, _files_to_chapters never saw _VOLUME_RE so the volume line was
    treated as the first line and split_leading_heading failed."""
    body = "正文内容。" * 80
    files = [
        _file("c296.txt", f"第一卷 风起云涌\n第296章 甲\n{body}"),
        _file("c297.txt", f"第297章 乙\n{body}"),
    ]
    r = client.post("/api/translate/bulk", data={"title": "Vol"}, files=files)
    assert r.status_code == 200, r.text
    novel_id = r.json()["novel_id"]
    rows = _row(
        "SELECT chapter_num, title_zh, original_text FROM chapters "
        "WHERE novel_id = ? ORDER BY chapter_num",
        (novel_id,),
    )
    assert [r["chapter_num"] for r in rows] == [296, 297]
    assert rows[0]["title_zh"] == "第296章 甲"
    # Volume divider must not leak into the body of the first chapter.
    assert "第一卷" not in rows[0]["original_text"]


def test_bulk_skips_numberless_heading_author_note(client: TestClient) -> None:
    """H8: a file titled 番外+活动预告 with author-note vocabulary in the body
    used to slip through as a real chapter because split_leading_heading
    matched the prologue token. Now it's caught by the secondary author-note
    check on numberless-heading files."""
    files = [
        _file("c1.txt", "第一章 标题\n" + "正文" * 80),
        _file(
            "extra.txt",
            "番外+活动预告！\n\n大家好，今天五更放到晚上八点，求一波月票支持。",
        ),
        _file("c2.txt", "第二章 标题\n" + "正文" * 80),
    ]
    r = client.post("/api/translate/bulk", data={"title": "WithNote"}, files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added_chapters"] == 2
    assert body["skipped_nonchapter"] == 1


def test_bulk_does_not_skip_numbered_chapter_with_announcement_vocab(
    client: TestClient,
) -> None:
    """H8 negative case: a numbered 第N章 file is NEVER skipped even when its
    body matches author-note vocabulary. Real chapters can mention 求月票 etc."""
    files = [
        _file("c1.txt", "第一章 标题\n" + "求月票！正文内容继续" * 60),
    ]
    r = client.post("/api/translate/bulk", data={"title": "Numbered"}, files=files)
    assert r.status_code == 200, r.text
    assert r.json()["added_chapters"] == 1
    assert r.json()["skipped_nonchapter"] == 0


def test_paste_create_returns_first_chapter(client: TestClient) -> None:
    """H9: /paste response carries first_chapter so the frontend can land
    the reader on the real first chapter (e.g. 296 for a partial raw)."""
    body = "正文内容。" * 80
    text = f"第296章 甲\n{body}\n\n第297章 乙\n{body}\n\n第298章 丙\n{body}"
    r = client.post(
        "/api/translate/paste", json={"title": "Partial", "text": text},
    )
    assert r.status_code == 200, r.text
    assert r.json()["first_chapter"] == 296


def test_bulk_create_returns_first_chapter(client: TestClient) -> None:
    """H9 on /bulk: response carries first_chapter from the lowest reconciled
    chapter number in the batch."""
    files = [
        _file(f"f{i}.txt", f"第{i}章 标题\n{'内容' * 80}")
        for i in range(10, 13)
    ]
    r = client.post("/api/translate/bulk", data={"title": "Bulk"}, files=files)
    assert r.status_code == 200, r.text
    assert r.json()["first_chapter"] == 10


def test_novels_list_exposes_first_chapter_num(client: TestClient) -> None:
    """H11 backend: /api/novels returns first_chapter_num per row so the
    library Open link can land on a real chapter for partial-import novels."""
    body = "正文内容。" * 80
    r = client.post(
        "/api/translate/paste",
        json={"title": "Partial2", "text": f"第296章 甲\n{body}\n\n第297章 乙\n{body}"},
    )
    novel_id = r.json()["novel_id"]
    r2 = client.get("/api/novels")
    assert r2.status_code == 200, r2.text
    novels = r2.json()
    novel = next(n for n in novels if n["id"] == novel_id)
    assert novel["first_chapter_num"] == 296


def test_append_paste_collision_surfaces_flag(client: TestClient) -> None:
    """H2: appending chapters whose printed numbers collide with existing
    rows used to silently shift to offset-mode. Now the response carries
    chapter_num_collision=True and the existing chapters are not overwritten."""
    body = "正文内容。" * 80
    # Create novel with chapters 1-3.
    r = client.post(
        "/api/translate/paste",
        json={
            "title": "Collide",
            "text": f"第一章 甲\n{body}\n\n第二章 乙\n{body}\n\n第三章 丙\n{body}",
        },
    )
    assert r.status_code == 200, r.text
    novel_id = r.json()["novel_id"]
    # Re-append chapters 2-3 with fresh body — would collide if inserted at
    # printed numbers. Expect: offset-mode kicks in, new chapters land at
    # 5, 6 (existing max=3 + reconciled 2,3), original 2 and 3 untouched.
    original_body_for_ch2 = _row(
        "SELECT original_text FROM chapters WHERE novel_id = ? AND chapter_num = 2",
        (novel_id,),
    )[0]["original_text"]
    r2 = client.post(
        f"/api/translate/append/{novel_id}/paste",
        json={"text": f"第二章 重 复\n{body}\n\n第三章 重 复\n{body}"},
    )
    assert r2.status_code == 200, r2.text
    payload = r2.json()
    assert payload["chapter_num_collision"] is True
    assert payload["added_chapters"] == 2
    assert payload["first_new_chapter"] == 5
    # Verify the original chapter 2 body is unchanged.
    cur_body = _row(
        "SELECT original_text FROM chapters WHERE novel_id = ? AND chapter_num = 2",
        (novel_id,),
    )[0]["original_text"]
    assert cur_body == original_body_for_ch2
    # And the new appended rows landed at 5 and 6.
    nums = [
        r["chapter_num"]
        for r in _row(
            "SELECT chapter_num FROM chapters WHERE novel_id = ? ORDER BY chapter_num",
            (novel_id,),
        )
    ]
    assert nums == [1, 2, 3, 5, 6]


def test_append_paste_no_collision_when_above_max(client: TestClient) -> None:
    """H2 negative: when printed numbers sit above existing max, the
    printed-direct path runs and collision is False."""
    body = "正文内容。" * 80
    r = client.post(
        "/api/translate/paste",
        json={
            "title": "Sequential",
            "text": f"第一章 甲\n{body}\n\n第二章 乙\n{body}",
        },
    )
    novel_id = r.json()["novel_id"]
    r2 = client.post(
        f"/api/translate/append/{novel_id}/paste",
        json={"text": f"第三章 丙\n{body}\n\n第四章 丁\n{body}"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["chapter_num_collision"] is False
    assert r2.json()["first_new_chapter"] == 3


def test_paste_title_whitespace_rejected(client: TestClient) -> None:
    """H5: whitespace-only title is rejected. PasteRequest's Pydantic
    min_length doesn't catch '   ' (it's three chars); normalize_title
    strips and rejects."""
    r = client.post(
        "/api/translate/paste", json={"title": "   ", "text": "第一章\n内容" * 50},
    )
    # Pydantic min_length=1 lets "   " through; normalize_title raises 400.
    assert r.status_code == 400, r.text


def test_upload_title_whitespace_rejected(client: TestClient) -> None:
    """H5: same enforcement on /upload, which used to skip validation
    entirely (Form(...) has no validators)."""
    files = [_upload_file("a.txt", "第一章\n内容" * 50)]
    r = client.post("/api/translate/upload", data={"title": "  "}, files=files)
    assert r.status_code == 400


def test_bulk_title_whitespace_rejected(client: TestClient) -> None:
    """H5: same enforcement on /bulk."""
    files = [_file("a.txt", "第一章\n内容" * 50)]
    r = client.post("/api/translate/bulk", data={"title": ""}, files=files)
    assert r.status_code == 400


def test_paste_oversize_rejected(client: TestClient) -> None:
    """H6: paste body over MAX_PASTE_CHARS is rejected. We patch the
    Pydantic max_length down for the test so we don't have to actually
    send 25M chars."""
    # Pydantic enforces max_length at validation time; if it's set to a
    # very small number on the test model, anything larger fails 422.
    # Easiest path: rely on the real cap. Skip if we'd have to send too much.
    from backend.models import MAX_PASTE_CHARS
    # Send slightly over the cap. Avoid materialising 25M-char strings in
    # CI — use the explicit cap-aware construction.
    over = "a" * (MAX_PASTE_CHARS + 1)
    r = client.post("/api/translate/paste", json={"title": "Big", "text": over})
    assert r.status_code == 422  # Pydantic Field(max_length=…) violation


def test_bulk_aggregate_bytes_capped(client: TestClient, monkeypatch) -> None:
    """H3: aggregate decoded-byte cap fires on the offending file with 413.
    Override MAX_BULK_TOTAL_BYTES to a small value so the test stays fast."""
    monkeypatch.setattr(
        "backend.routes.translate.MAX_BULK_TOTAL_BYTES", 100_000,
    )
    monkeypatch.setattr(
        "backend.services.uploads.MAX_BULK_TOTAL_BYTES", 100_000,
    )
    # Each file ~60 KB of CJK; second file pushes total over 100 KB.
    blob = "内容" * 15_000  # ~30 KB UTF-8 encoded
    files = [
        _file("a.txt", blob),
        _file("b.txt", blob),
        _file("c.txt", blob),  # this one tips the total over 100 KB
        _file("d.txt", blob),
    ]
    r = client.post(
        "/api/translate/bulk", data={"title": "Cap"}, files=files,
    )
    assert r.status_code == 413, r.text
    assert "total size cap" in r.json()["detail"]
