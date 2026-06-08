"""HTTP-level + unit tests for routes/novels.py (mounted at /api/novels).

This is the owning test for `backend.routes.novels`: it imports the module at
top level and exercises both its pure helpers (`_safe_filename`, the row->kwargs
adapter, the streaming download body) and its HTTP surface against a real temp
DB. No translation/queue work runs, `mass_queue` is the only endpoint that
would spawn a worker and it is exercised only on the skip/404 paths where no
worker is spawned.

Coverage:
  * GET    /api/novels, list, archived filter, progress agg
  * GET    /api/novels/{id}, single + 404
  * PATCH  /api/novels/{id}, partial update, blank-title 400,
                                                empty-body 400, bad provider 400
  * PUT    /api/novels/{id}/reading-position, set + 404
  * DELETE /api/novels/{id} (+ /restore /purge /delete-counts), soft-delete flow
  * GET    /api/novels/{id}/download, txt / md formats + bad-format 400
  * helpers: _safe_filename, _row_to_novel_kwargs
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Direct import: this file is the owning test for the novels route module.
from backend.routes import novels

DB_PATH = Path(os.environ["DB_PATH"])


@pytest.fixture
def client(monkeypatch):
    """TestClient with the startup probe + queue drain stubbed so the lifespan
    never reaches for a real translator. Entering the context manager runs
    init_db() against the fresh temp DB."""
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


def _seed_novel(
    title: str = "Test Novel",
    *,
    source_type: str = "paste",
    genre: str | None = "xianxia",
) -> int:
    """Insert one novel. Returns novel_id. The explicit commit is required, sqlite3 deferred transactions roll back on close() without it."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO novels (title, source_type, genre) VALUES (?, ?, ?)",
            (title, source_type, genre),
        )
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return novel_id
    finally:
        conn.close()


def _seed_chapters(novel_id: int, specs: list[tuple]) -> None:
    """specs: list of (chapter_num, status, translated_text, translate_queued)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executemany(
            "INSERT INTO chapters "
            "(novel_id, chapter_num, original_text, status, translated_text, "
            " translate_queued) VALUES (?, ?, '原文', ?, ?, ?)",
            [
                (novel_id, num, status, translated, queued)
                for (num, status, translated, queued) in specs
            ],
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Pure-helper unit tests (no DB, no app).
# --------------------------------------------------------------------------

def test_safe_filename_sanitizes_unsafe_chars_and_keeps_ext():
    ascii_name, utf8_name = novels._safe_filename('a/b:c*?"novel', "txt")
    # Slashes, colons, asterisks, quotes, question marks are all unsafe.
    assert "/" not in ascii_name and ":" not in ascii_name
    assert ascii_name.endswith(".txt")
    assert utf8_name.endswith(".txt")
    # The ascii fallback collapses non [A-Za-z0-9._-] runs to underscores.
    assert ascii_name == "a_b_c_novel.txt"


def test_safe_filename_all_unsafe_falls_back_to_novel():
    # Every char is unsafe/strippable, so the ascii fallback collapses to
    # "novel"; the utf8 side retains the collapsed underscore placeholder.
    ascii_name, utf8_name = novels._safe_filename("   ///   ", "md")
    assert ascii_name == "novel.md"
    assert utf8_name == "_.md"
    # Truly empty input falls back to "novel" on both sides.
    ascii_empty, utf8_empty = novels._safe_filename("", "txt")
    assert ascii_empty == "novel.txt"
    assert utf8_empty == "novel.txt"


def test_safe_filename_avoids_windows_reserved_stem():
    # "CON" is a reserved device name on Windows; the helper prefixes "_".
    ascii_name, utf8_name = novels._safe_filename("CON", "epub")
    assert ascii_name == "_CON.epub"
    assert utf8_name == "_CON.epub"


def test_row_to_novel_kwargs_defaults_source_language_to_zh():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # NULL source_language must surface as 'zh', genre passes through.
        row = conn.execute(
            "SELECT NULL AS id, 'T' AS title, 'paste' AS source_type, "
            "NULL AS source_url, 'now' AS created_at, NULL AS style_note, "
            "NULL AS source_language, 'wuxia' AS genre, "
            "NULL AS custom_style_brief, NULL AS translator_provider_id, "
            "NULL AS refinement_provider_id"
        ).fetchone()
        kwargs = novels._row_to_novel_kwargs(row)
        assert kwargs["source_language"] == "zh"
        assert kwargs["genre"] == "wuxia"
        # Columns absent from this SELECT degrade to None via the keys() guard.
        assert kwargs["author"] is None
        assert kwargs["import_status"] is None
    finally:
        conn.close()


# --------------------------------------------------------------------------
# HTTP endpoint tests.
# --------------------------------------------------------------------------

def test_list_novels_reports_progress_aggregates(client):
    novel_id = _seed_novel("Progress Novel")
    # ch1 done, ch2 queued/pending, ch3 pending.
    _seed_chapters(
        novel_id,
        [(1, "done", "English body", 0), (2, "pending", None, 1), (3, "pending", None, 0)],
    )
    resp = client.get("/api/novels")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == novel_id
    assert row["total_chapters"] == 3
    assert row["done_chapters"] == 1
    assert row["translate_queue"] == 1
    assert row["first_chapter_num"] == 1


def test_list_novels_archived_filter_partitions(client):
    active_id = _seed_novel("Active")
    archived_id = _seed_novel("Archived")
    # Archive the second one directly.
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE novels SET deleted_at = datetime('now') WHERE id = ?",
        (archived_id,),
    )
    conn.commit()
    conn.close()

    default_ids = {r["id"] for r in client.get("/api/novels").json()}
    assert default_ids == {active_id}
    assert archived_id not in default_ids

    archived_ids = {r["id"] for r in client.get("/api/novels?archived=true").json()}
    assert archived_ids == {archived_id}
    assert active_id not in archived_ids


def test_get_novel_single_and_404(client):
    novel_id = _seed_novel("Solo")
    _seed_chapters(novel_id, [(1, "done", "Body", 0)])
    ok = client.get(f"/api/novels/{novel_id}")
    assert ok.status_code == 200
    body = ok.json()
    assert body["title"] == "Solo"
    assert body["total_chapters"] == 1
    assert body["done_chapters"] == 1

    missing = client.get("/api/novels/999999")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "novel not found"


def test_patch_novel_updates_fields_and_normalizes(client):
    novel_id = _seed_novel("Old Title", genre="xianxia")
    resp = client.patch(
        f"/api/novels/{novel_id}",
        json={
            "title": "  New Title  ",
            "genre": "wuxia",
            "source_language": "ZH",
            "custom_style_brief": "   ",  # blank -> NULL
            "author": "  Mo Xiang  ",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "New Title"          # stripped
    assert body["genre"] == "wuxia"
    assert body["source_language"] == "zh"       # lowercased
    assert body["custom_style_brief"] is None    # blank normalized to NULL
    assert body["author"] == "Mo Xiang"          # stripped


def test_patch_novel_blank_title_400(client):
    novel_id = _seed_novel("Keep Me")
    resp = client.patch(f"/api/novels/{novel_id}", json={"title": "   "})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "title cannot be blank"
    # The bad write must not have changed the stored title.
    still = client.get(f"/api/novels/{novel_id}").json()
    assert still["title"] == "Keep Me"


def test_patch_novel_empty_body_400(client):
    novel_id = _seed_novel("Untouched")
    resp = client.patch(f"/api/novels/{novel_id}", json={})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "no fields to update"


def test_patch_novel_unknown_provider_400(client):
    novel_id = _seed_novel("ProviderTest")
    resp = client.patch(
        f"/api/novels/{novel_id}",
        json={"translator_provider_id": 424242},
    )
    assert resp.status_code == 400
    assert "424242" in resp.json()["detail"]


def test_reading_position_set_and_404(client):
    novel_id = _seed_novel("Resumeable")
    ok = client.put(
        f"/api/novels/{novel_id}/reading-position",
        json={"chapter_num": 7},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["novel_id"] == novel_id
    assert body["last_read_chapter_num"] == 7
    # Confirm it persisted by reading the novel row back.
    conn = sqlite3.connect(DB_PATH)
    stored = conn.execute(
        "SELECT last_read_chapter_num FROM novels WHERE id = ?", (novel_id,)
    ).fetchone()[0]
    conn.close()
    assert stored == 7

    missing = client.put(
        "/api/novels/999999/reading-position", json={"chapter_num": 1}
    )
    assert missing.status_code == 404


def test_delete_counts_then_archive_restore_purge_flow(client):
    novel_id = _seed_novel("Lifecycle")
    _seed_chapters(novel_id, [(1, "done", "Body", 0), (2, "pending", None, 0)])

    # Preview counts before any destructive action.
    counts = client.get(f"/api/novels/{novel_id}/delete-counts")
    assert counts.status_code == 200
    assert counts.json()["chapters"] == 2
    assert counts.json()["novel_id"] == novel_id

    # Soft-delete (archive).
    archived = client.delete(f"/api/novels/{novel_id}")
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert archived.json()["chapters"] == 2
    # It now hides from the default list but shows in the archive view.
    assert novel_id not in {r["id"] for r in client.get("/api/novels").json()}
    assert novel_id in {r["id"] for r in client.get("/api/novels?archived=true").json()}

    # Purging an active novel is refused; archived can be purged.
    restored = client.post(f"/api/novels/{novel_id}/restore")
    assert restored.status_code == 200
    assert restored.json() == {"restored": novel_id}
    refused = client.delete(f"/api/novels/{novel_id}/purge")
    assert refused.status_code == 409

    client.delete(f"/api/novels/{novel_id}")  # re-archive
    purged = client.delete(f"/api/novels/{novel_id}/purge")
    assert purged.status_code == 200
    assert purged.json()["status"] == "purged"
    # Gone entirely now.
    assert client.get(f"/api/novels/{novel_id}").status_code == 404


def test_download_txt_and_md_contain_chapter_bodies(client):
    novel_id = _seed_novel("Downloadable")
    _seed_chapters(
        novel_id,
        [(1, "done", "First chapter body.", 0), (2, "pending", None, 0)],
    )

    txt = client.get(f"/api/novels/{novel_id}/download?format=txt")
    assert txt.status_code == 200
    assert "text/plain" in txt.headers["content-type"]
    assert "First chapter body." in txt.text
    # Untranslated chapter shows the placeholder, not blank.
    assert "not translated" in txt.text

    md = client.get(f"/api/novels/{novel_id}/download?format=md")
    assert md.status_code == 200
    assert "text/markdown" in md.headers["content-type"]
    # Markdown title heading + chapter body present.
    assert "# Downloadable" in md.text
    assert "First chapter body." in md.text


def test_download_skip_untranslated_omits_pending_placeholder(client):
    novel_id = _seed_novel("SkipNovel")
    _seed_chapters(
        novel_id,
        [(1, "done", "Done body.", 0), (2, "pending", None, 0)],
    )
    resp = client.get(
        f"/api/novels/{novel_id}/download?format=txt&skip_untranslated=true"
    )
    assert resp.status_code == 200
    assert "Done body." in resp.text
    # The pending chapter is dropped entirely, so no placeholder text.
    assert "not translated" not in resp.text


def test_download_bad_format_400_and_missing_novel_404(client):
    novel_id = _seed_novel("FmtNovel")
    bad = client.get(f"/api/novels/{novel_id}/download?format=pdf")
    assert bad.status_code == 400
    assert "unsupported format" in bad.json()["detail"]

    missing = client.get("/api/novels/999999/download?format=txt")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "novel not found"
