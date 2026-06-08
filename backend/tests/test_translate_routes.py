"""Direct coverage for backend/routes/translate.py.

The translate router is the HTTP entry surface for every import path: paste,
single-file upload, bulk multi-file upload, mid-novel insert, append, and the
recipe/generic /scrape dispatch. These tests drive the router through the real
FastAPI app against a fresh temp SQLite DB and assert the DB landing state.

Nothing here translates or hits the network:
- /paste and /upload import plain text; we assert chapters land 'pending' (the
  importer never auto-queues) and the response shape is correct.
- /scrape is exercised only through stubbed service boundaries: the recipe
  branch stubs scrape_jobs.create_job / spawn, and the generic branch stubs
  scrape_url. No real URL is ever fetched.

The module under test is imported at top level (`from backend.routes import
translate`) and asserted on directly so the coverage mapping owns it here.
"""

from __future__ import annotations

import os
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import SCHEMA
from backend.main import app

# Direct import of the module under test, this file is its owning test.
from backend.routes import translate

DB_PATH = Path(os.environ["DB_PATH"])


@pytest.fixture
def client(monkeypatch):
    # Fresh schema-only DB for every test.
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    # Belt-and-braces: if any background queue worker were ever spawned, keep
    # it off the network. The import paths under test never queue translation,
    # but stubbing the translator boundary guarantees no provider call escapes.
    async def _fake_translate(original, title_zh, glossary, **kwargs):
        from backend.models import TranslationResult

        return TranslationResult(title_en="EN", translated_text="t", new_terms=[])

    monkeypatch.setattr("backend.services.queue.translate_chapter", _fake_translate)

    async def _noop_run(novel_id, chapter_id):
        return

    monkeypatch.setattr("backend.services.queue._run_translate", _noop_run)

    return TestClient(app)


def _rows(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(query, params).fetchall())
    finally:
        conn.close()


def _txt_upload(name: str, content: str) -> tuple[str, tuple[str, BytesIO, str]]:
    """/upload declares `file: UploadFile` (singular field name)."""
    return ("file", (name, BytesIO(content.encode("utf-8")), "text/plain"))


# --------------------------------------------------------------------------- #
# Module-surface assertions (these own routes/translate.py for coverage).
# --------------------------------------------------------------------------- #


def test_router_exposes_expected_paths() -> None:
    """The router registers every documented import path with the right verb."""
    paths = {(r.path, tuple(sorted(r.methods))) for r in translate.router.routes}
    assert ("/paste", ("POST",)) in paths
    assert ("/upload", ("POST",)) in paths
    assert ("/bulk", ("POST",)) in paths
    assert ("/scrape", ("POST",)) in paths
    assert ("/insert/{novel_id}", ("POST",)) in paths
    assert ("/append/{novel_id}/paste", ("POST",)) in paths


def test_normalize_title_strips_and_truncates() -> None:
    """normalize_title trims surrounding whitespace and caps at MAX_TITLE_CHARS."""
    from backend.models import MAX_TITLE_CHARS

    assert translate.normalize_title("  Hello  ") == "Hello"
    long = translate.normalize_title("x" * (MAX_TITLE_CHARS + 50))
    assert len(long) == MAX_TITLE_CHARS
    assert long == "x" * MAX_TITLE_CHARS


def test_normalize_title_rejects_blank() -> None:
    """Whitespace-only and empty titles raise HTTPException(400)."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        translate.normalize_title("   ")
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException):
        translate.normalize_title(None)


def test_validate_upload_filename_gating() -> None:
    """The format-key resolver accepts the supported extensions, normalizes
    .htm -> html, defaults an empty name to txt, and 400s on unknown types."""
    from fastapi import HTTPException

    assert translate._validate_upload_filename("a.txt") == "txt"
    assert translate._validate_upload_filename("a.EPUB") == "epub"
    assert translate._validate_upload_filename("a.htm") == "html"
    assert translate._validate_upload_filename("") == "txt"
    with pytest.raises(HTTPException) as ei:
        translate._validate_upload_filename("a.pdf")
    assert ei.value.status_code == 400


def test_assert_txt_filename_rejects_non_txt() -> None:
    """The bulk TXT-only guard 400s on a non-.txt name and passes a .txt name."""
    from fastapi import HTTPException

    # No exception for a .txt name or an empty name.
    assert translate._assert_txt_filename("c.txt") is None
    assert translate._assert_txt_filename("") is None
    with pytest.raises(HTTPException) as ei:
        translate._assert_txt_filename("c.docx")
    assert ei.value.status_code == 400


# --------------------------------------------------------------------------- #
# /paste
# --------------------------------------------------------------------------- #


def test_paste_creates_pending_chapters(client: TestClient) -> None:
    """/paste parses chapters, persists them 'pending', and never auto-queues."""
    body = "正文内容。" * 80
    text = f"第一章 甲\n{body}\n\n第二章 乙\n{body}\n\n第三章 丙\n{body}"
    r = client.post("/api/translate/paste", json={"title": "Pasted", "text": text})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["first_chapter"] == 1
    novel_id = payload["novel_id"]
    assert isinstance(novel_id, int)

    rows = _rows(
        "SELECT chapter_num, status, translate_queued, translated_text "
        "FROM chapters WHERE novel_id = ? ORDER BY chapter_num",
        (novel_id,),
    )
    assert [r["chapter_num"] for r in rows] == [1, 2, 3]
    assert all(r["status"] == "pending" for r in rows)
    assert all(r["translate_queued"] == 0 for r in rows)
    assert all(r["translated_text"] is None for r in rows)


def test_paste_persists_source_type_and_title(client: TestClient) -> None:
    """The novel row records the user title and source_type='paste'."""
    r = client.post(
        "/api/translate/paste",
        json={"title": "MyNovel", "text": "第一章 标题\n" + "内容" * 80},
    )
    assert r.status_code == 200, r.text
    novel_id = r.json()["novel_id"]
    nov = _rows("SELECT title, source_type FROM novels WHERE id = ?", (novel_id,))
    assert len(nov) == 1
    assert nov[0]["title"] == "MyNovel"
    assert nov[0]["source_type"] == "paste"


def test_paste_blank_title_rejected(client: TestClient) -> None:
    """A whitespace-only title passes Pydantic min_length but normalize_title
    rejects it with 400 (the validation path covered by normalize_title)."""
    r = client.post(
        "/api/translate/paste",
        json={"title": "   ", "text": "第一章\n内容" * 50},
    )
    assert r.status_code == 400, r.text
    # No novel was written for the rejected paste.
    assert _rows("SELECT id FROM novels") == []


def test_paste_unknown_genre_rejected(client: TestClient) -> None:
    """An unrecognized genre key is rejected by _validate_genre with 400, and
    no novel is committed."""
    r = client.post(
        "/api/translate/paste",
        json={
            "title": "G",
            "text": "第一章\n内容" * 50,
            "genre": "not-a-real-genre",
        },
    )
    assert r.status_code == 400, r.text
    assert _rows("SELECT id FROM novels") == []


# --------------------------------------------------------------------------- #
# /upload (.txt)
# --------------------------------------------------------------------------- #


def test_upload_txt_decodes_and_inserts(client: TestClient) -> None:
    """A small in-memory .txt UploadFile is decoded and split into chapters,
    each landing 'pending'. The response carries the detected encoding and
    source_type='txt'."""
    body = "正文内容。" * 80
    content = f"第一章 甲\n{body}\n\n第二章 乙\n{body}"
    files = [_txt_upload("novel.txt", content)]
    r = client.post("/api/translate/upload", data={"title": "Uploaded"}, files=files)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["source_type"] == "txt"
    assert payload["first_chapter"] == 1
    assert payload["detected_encoding"]  # chardet reported something non-empty
    novel_id = payload["novel_id"]

    rows = _rows(
        "SELECT chapter_num, status FROM chapters WHERE novel_id = ? "
        "ORDER BY chapter_num",
        (novel_id,),
    )
    assert [r["chapter_num"] for r in rows] == [1, 2]
    assert all(r["status"] == "pending" for r in rows)


def test_upload_rejects_unsupported_extension(client: TestClient) -> None:
    """A .pdf upload is rejected with 400 by the filename gate before decode."""
    files = [_txt_upload("evil.pdf", "%PDF-1.4 garbage")]
    r = client.post("/api/translate/upload", data={"title": "X"}, files=files)
    assert r.status_code == 400, r.text
    assert _rows("SELECT id FROM novels") == []


def test_upload_blank_title_rejected(client: TestClient) -> None:
    """/upload uses Form(...) with no validators; normalize_title supplies the
    same blank-title rejection as /paste."""
    files = [_txt_upload("a.txt", "第一章\n内容" * 50)]
    r = client.post("/api/translate/upload", data={"title": "  "}, files=files)
    assert r.status_code == 400
    assert _rows("SELECT id FROM novels") == []


# --------------------------------------------------------------------------- #
# /append/{novel_id}/paste  +  /insert/{novel_id}
# --------------------------------------------------------------------------- #


def _create_three_chapter_novel(client: TestClient) -> int:
    body = "正文内容。" * 80
    text = f"第一章 甲\n{body}\n\n第二章 乙\n{body}\n\n第三章 丙\n{body}"
    r = client.post("/api/translate/paste", json={"title": "Base", "text": text})
    assert r.status_code == 200, r.text
    return r.json()["novel_id"]


def test_append_paste_lands_above_max(client: TestClient) -> None:
    """Appending printed chapters above the current max lands them at the
    printed numbers with no collision, leaving the existing rows intact."""
    novel_id = _create_three_chapter_novel(client)
    body = "正文内容。" * 80
    r = client.post(
        f"/api/translate/append/{novel_id}/paste",
        json={"text": f"第四章 丁\n{body}\n\n第五章 戊\n{body}"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["added_chapters"] == 2
    assert payload["first_new_chapter"] == 4
    assert payload["chapter_num_collision"] is False

    nums = [
        row["chapter_num"]
        for row in _rows(
            "SELECT chapter_num FROM chapters WHERE novel_id = ? ORDER BY chapter_num",
            (novel_id,),
        )
    ]
    assert nums == [1, 2, 3, 4, 5]


def test_append_paste_missing_novel_404(client: TestClient) -> None:
    """Appending to a novel that does not exist raises 404 (ensure_novel_exists)."""
    r = client.post(
        "/api/translate/append/99999/paste",
        json={"text": "第一章\n内容" * 50},
    )
    assert r.status_code == 404, r.text


def test_insert_chapter_renumbers_tail(client: TestClient) -> None:
    """/insert places a chapter immediately after after_chapter_num and shifts
    the tail down, so reading order is preserved and the count grows by one."""
    novel_id = _create_three_chapter_novel(client)
    body = "新的正文内容。" * 60
    r = client.post(
        f"/api/translate/insert/{novel_id}",
        json={"after_chapter_num": 1, "text": body, "title": "Inserted"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["added_chapters"] == 1
    assert payload["first_new_chapter"] == 2

    rows = _rows(
        "SELECT chapter_num, title_zh, status FROM chapters WHERE novel_id = ? "
        "ORDER BY chapter_num",
        (novel_id,),
    )
    # Tail renumbered: original 2,3 pushed to 3,4; inserted row is the new 2.
    assert [r["chapter_num"] for r in rows] == [1, 2, 3, 4]
    assert rows[1]["title_zh"] == "Inserted"
    assert rows[1]["status"] == "pending"


def test_insert_missing_novel_404(client: TestClient) -> None:
    """Insert into a nonexistent novel raises 404."""
    r = client.post(
        "/api/translate/insert/424242",
        json={"after_chapter_num": 0, "text": "内容" * 60},
    )
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# /scrape, stubbed at the service boundary, never touches the network.
# --------------------------------------------------------------------------- #


def test_scrape_recipe_url_spawns_background_job(
    client: TestClient, monkeypatch
) -> None:
    """A recipe-matched host backgrounds the crawl: the route creates a job row
    and fires scrape_jobs.spawn WITHOUT running any crawl inside the request.

    We stub dispatch to return a sentinel recipe, create_job to a fixed id, and
    spawn to record its args, so no fetch and no asyncio task ever runs."""
    import backend.services.scrape_jobs as scrape_jobs

    monkeypatch.setattr(
        "backend.services.scrapers.dispatch", lambda host: object()
    )

    async def _fake_create_job(url: str) -> int:
        return 777

    spawned: list[tuple] = []

    def _fake_spawn(job_id, url, cookies):
        spawned.append((job_id, url, cookies))

    monkeypatch.setattr(scrape_jobs, "create_job", _fake_create_job)
    monkeypatch.setattr(scrape_jobs, "spawn", _fake_spawn)

    r = client.post(
        "/api/translate/scrape",
        json={"url": "https://www.69shuba.com/book/12345"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["mode"] == "job"
    assert payload["job_id"] == 777
    assert payload["status"] == "pending"
    assert payload["recipe"] is True
    # The crawl was scheduled, not run: spawn got the job id + url, and no
    # novel was committed by the request.
    assert spawned == [(777, "https://www.69shuba.com/book/12345", None)]
    assert _rows("SELECT id FROM novels") == []


def test_scrape_recipe_url_rejects_append_mode(
    client: TestClient, monkeypatch
) -> None:
    """Recipe imports always create a fresh novel; supplying novel_id is a 400
    BEFORE any crawl is attempted (we assert spawn is never reached)."""
    import backend.services.scrape_jobs as scrape_jobs

    monkeypatch.setattr(
        "backend.services.scrapers.dispatch", lambda host: object()
    )

    def _boom_spawn(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("spawn must not run when append-mode is rejected")

    monkeypatch.setattr(scrape_jobs, "spawn", _boom_spawn)

    r = client.post(
        "/api/translate/scrape",
        json={"url": "https://www.69shuba.com/book/1", "novel_id": 5},
    )
    assert r.status_code == 400, r.text
    assert "Append-mode" in r.json()["detail"]


def test_scrape_generic_url_creates_novel(client: TestClient, monkeypatch) -> None:
    """A non-recipe host runs the fast generic path: scrape_url returns the
    article body (stubbed, no network) and the route creates a novel via the
    same pipeline as /paste, stamping source_type='url'."""
    monkeypatch.setattr(
        "backend.services.scrapers.dispatch", lambda host: None
    )

    class _Result:
        text = "第一章 抓取\n" + "正文内容。" * 80
        title = "Scraped Title"
        source_url = "https://blog.example.com/post"
        cover_bytes = None
        cover_ext = None

    async def _fake_scrape_url(url, cookies=None):
        return _Result()

    monkeypatch.setattr("backend.routes.translate.scrape_url", _fake_scrape_url)

    r = client.post(
        "/api/translate/scrape",
        json={"url": "https://blog.example.com/post"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["mode"] == "created"
    assert payload["scraped_url"] == "https://blog.example.com/post"
    novel_id = payload["novel_id"]
    nov = _rows("SELECT source_type, source_url FROM novels WHERE id = ?", (novel_id,))
    assert nov[0]["source_type"] == "url"
    assert nov[0]["source_url"] == "https://blog.example.com/post"


def test_scrape_generic_error_surfaces_400(client: TestClient, monkeypatch) -> None:
    """A ScrapeError from the generic path becomes a 400 carrying error_kind,
    and no novel is created."""
    from backend.services.scraper import ScrapeError

    monkeypatch.setattr(
        "backend.services.scrapers.dispatch", lambda host: None
    )

    async def _raise(url, cookies=None):
        err = ScrapeError("blocked by Cloudflare")
        err.error_kind = "cloudflare"
        raise err

    monkeypatch.setattr("backend.routes.translate.scrape_url", _raise)

    r = client.post(
        "/api/translate/scrape", json={"url": "https://blog.example.com/x"}
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["error_kind"] == "cloudflare"
    assert "Cloudflare" in detail["message"]
    assert _rows("SELECT id FROM novels") == []
