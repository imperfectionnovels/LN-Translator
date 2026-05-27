"""Initiative 7 round-trip smoke test against the frozen LN-Translator EXE.

Verifies the bundled ebooklib + python-docx + lxml actually work end-to-end
inside the packaged binary — not just in the dev environment.

Flow:
  1. Pick a fresh USER_DATA_ROOT, launch dist/LN-Translator/LN-Translator.exe.
  2. Find the bound port (default 8765, may walk to 8766+).
  3. Build a fixture EPUB in-process.
  4. POST it to /api/translate/upload.
  5. Insert a translated chapter directly into the EXE's SQLite so the
     export path has something to render (no provider configured in the
     smoke profile).
  6. GET /api/novels/{id}/download?format=epub.
  7. Re-parse the response with ebooklib in host Python and assert
     chapter content survived.
  8. Tear the EXE down, exit 0 with SMOKE PASS / exit 1 with SMOKE FAIL.

Run: `python scripts/smoke_initiative7.py`. Assumes the EXE bundle exists
at dist/LN-Translator/LN-Translator.exe (run scripts/smoke-exe.ps1 first
if it doesn't).
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXE_PATH = REPO_ROOT / "dist" / "LN-Translator" / "LN-Translator.exe"


def fail(
    msg: str,
    *,
    proc: subprocess.Popen | None = None,
    log_path: Path | None = None,
    startup_log_path: Path | None = None,
) -> int:
    print(f"SMOKE FAIL: {msg}", flush=True)
    if log_path and log_path.exists():
        print("--- exe stdout (last 40 lines) ---", flush=True)
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]:
            print(line, flush=True)
    # With console=False in the frozen build, stdout is empty; startup.log
    # is the actual diagnostic surface for boot-time failures.
    if startup_log_path and startup_log_path.exists():
        print("--- startup.log (last 40 lines) ---", flush=True)
        for line in startup_log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]:
            print(line, flush=True)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 1


def find_live_port(deadline: float, data_dir: Path | None = None) -> int | None:
    """Return the port THIS smoke's EXE bound to, polled until /api/health
    returns 200 or the deadline expires.

    Prefers the sentinel file `<data_dir>/port.txt` that app_entry writes
    once uvicorn is bound — guarantees we talk to our own EXE rather than
    a stale instance on 8765 (Bug #7). If the sentinel is absent (older
    EXE, or write failed), falls back to the legacy walk over 8765..8814.
    """
    import httpx

    def _probe(p: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", p), timeout=0.1):
                pass
        except OSError:
            return False
        try:
            r = httpx.get(f"http://127.0.0.1:{p}/api/health", timeout=2.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    while time.monotonic() < deadline:
        # 1) sentinel-driven path — definitive port for THIS EXE.
        if data_dir is not None:
            sentinel = data_dir / "port.txt"
            if sentinel.exists():
                try:
                    p = int(sentinel.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    p = None
                if p is not None and _probe(p):
                    return p
        # 2) fallback walk — same behavior as before the sentinel existed.
        for p in range(8765, 8815):
            if _probe(p):
                return p
        time.sleep(0.5)
    return None


def build_fixture_epub() -> bytes:
    """Two chapters + cover, padded over MIN_CHAPTER_CHARS so the parser
    doesn't merge them."""
    from ebooklib import epub

    pad = "The protagonist walked the long road of trials and tribulations. " * 8

    book = epub.EpubBook()
    book.set_identifier("urn:smoke:init7")
    book.set_title("Smoke Test Novel")
    book.set_language("en")
    book.add_author("Smoke Author")

    tiny_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
        b"\x00\x00\x00\x03\x00\x01\x88\x00\xbf\xf0\xd6\x06\x00\x00\x00\x00IEND"
        b"\xaeB`\x82"
    )
    book.set_cover("cover.png", tiny_png)

    items = []
    for i, (t, body) in enumerate([
        ("Chapter 1: The Opening", f"First chapter body. {pad}"),
        ("Chapter 2: The Closing", f"Second chapter body. {pad}"),
    ], start=1):
        item = epub.EpubHtml(title=t, file_name=f"chap_{i}.xhtml", lang="en")
        item.content = (
            f"<html xmlns='http://www.w3.org/1999/xhtml'>"
            f"<body><h1>{t}</h1><p>{body}</p></body></html>"
        )
        book.add_item(item)
        items.append(item)

    book.toc = tuple(items)
    book.spine = ["cover", "nav", *items]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    fd, tmp_name = tempfile.mkstemp(suffix=".epub", prefix="smoke-init7-")
    os.close(fd)
    try:
        epub.write_epub(tmp_name, book, {})
        return Path(tmp_name).read_bytes()
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def main() -> int:
    if not EXE_PATH.exists():
        return fail(
            f"EXE not built at {EXE_PATH}; run scripts/smoke-exe.ps1 first."
        )

    data_dir = Path(tempfile.mkdtemp(prefix="ln-i7-smoke-"))
    env = os.environ.copy()
    env["LN_TRANSLATOR_DATA"] = str(data_dir)
    # Headless mode: no pywebview window, no browser tab — the smoke only
    # drives the EXE over HTTP. Per-child env (we pass env=env to Popen),
    # so this never leaks into the parent process's environment.
    env["LN_TRANSLATOR_NO_WINDOW"] = "1"
    # Make sure we don't accidentally bootstrap a provider from env keys.
    for k in ("GEMINI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        env.pop(k, None)

    log_path = data_dir / "exe.log"
    log_err = data_dir / "exe.err.log"
    # With console=False in the frozen build, the EXE's stdout/stderr is
    # empty; the actual startup diagnostics live in this file. Computed
    # here so every fail() call can pass it.
    startup_log_path = data_dir / "logs" / "startup.log"
    print(f"==> launching EXE (data dir: {data_dir})", flush=True)
    with open(log_path, "wb") as out, open(log_err, "wb") as err:
        proc = subprocess.Popen(
            [str(EXE_PATH)],
            stdout=out,
            stderr=err,
            env=env,
            cwd=str(data_dir),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    try:
        port = find_live_port(time.monotonic() + 30.0, data_dir=data_dir)
        if not port:
            return fail("no /api/health response within 30s", proc=proc, log_path=log_path, startup_log_path=startup_log_path)
        print(f"==> /api/health reachable on port {port}", flush=True)

        import httpx

        base = f"http://127.0.0.1:{port}"

        # --- 1) POST a fixture EPUB --------------------------------------
        print("==> POST /api/translate/upload (EPUB fixture)", flush=True)
        epub_bytes = build_fixture_epub()
        r = httpx.post(
            f"{base}/api/translate/upload",
            data={"title": "Smoke EPUB Import"},
            files={"file": ("smoke.epub", epub_bytes, "application/epub+zip")},
            timeout=30.0,
        )
        if r.status_code != 200:
            return fail(
                f"upload returned {r.status_code}: {r.text[:400]}",
                proc=proc, log_path=log_path, startup_log_path=startup_log_path,
            )
        body = r.json()
        if body.get("source_type") != "epub":
            return fail(f"source_type expected 'epub', got {body!r}", proc=proc, log_path=log_path, startup_log_path=startup_log_path)
        if body.get("cover_extracted") is not True:
            return fail(
                f"cover_extracted expected True, got {body!r}",
                proc=proc, log_path=log_path, startup_log_path=startup_log_path,
            )
        novel_id = body["novel_id"]
        print(f"   novel_id={novel_id}, cover_extracted={body['cover_extracted']}", flush=True)

        # Confirm exactly two chapters parsed and the EPUB cover landed
        # on disk under USER_DATA_ROOT/covers/.
        db_path = data_dir / "novels.db"
        if not db_path.exists():
            return fail(
                f"DB file not found at {db_path}",
                proc=proc, log_path=log_path, startup_log_path=startup_log_path,
            )
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = list(
                conn.execute(
                    "SELECT chapter_num, title_zh FROM chapters "
                    "WHERE novel_id = ? ORDER BY chapter_num",
                    (novel_id,),
                )
            )
        if len(rows) != 2:
            return fail(
                f"expected 2 chapters parsed from EPUB, got {len(rows)}",
                proc=proc, log_path=log_path, startup_log_path=startup_log_path,
            )
        if "Chapter 1" not in (rows[0]["title_zh"] or ""):
            return fail(
                f"chapter 1 title_zh missing 'Chapter 1': {rows[0]['title_zh']!r}",
                proc=proc, log_path=log_path, startup_log_path=startup_log_path,
            )
        cover_dir = data_dir / "covers"
        cover_files = list(cover_dir.glob(f"{novel_id}.*"))
        if not cover_files:
            return fail(
                f"EPUB cover not written to {cover_dir}",
                proc=proc, log_path=log_path, startup_log_path=startup_log_path,
            )
        print(f"   {len(rows)} chapters parsed; cover={cover_files[0].name}", flush=True)

        # --- 2) Plant a translated chapter directly so EPUB export has
        #         something to render. No provider is configured in this
        #         smoke profile so we can't run a real translation.
        print("==> seeding translated chapter rows via SQL", flush=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE chapters SET title_en=?, translated_text=?, status='done' "
                "WHERE novel_id=? AND chapter_num=1",
                (
                    "Chapter 1: The Opening (EN)",
                    "First chapter, translated.\n\nA second paragraph in chapter one.",
                    novel_id,
                ),
            )
            conn.execute(
                "UPDATE chapters SET title_en=?, translated_text=?, status='done' "
                "WHERE novel_id=? AND chapter_num=2",
                (
                    "Chapter 2: The Closing (EN)",
                    "Second chapter, translated.",
                    novel_id,
                ),
            )
            conn.commit()

        # --- 3) GET ?format=epub and re-parse ----------------------------
        print("==> GET /api/novels/{id}/download?format=epub", flush=True)
        r = httpx.get(
            f"{base}/api/novels/{novel_id}/download",
            params={"format": "epub"},
            timeout=30.0,
        )
        if r.status_code != 200:
            return fail(
                f"download returned {r.status_code}: {r.text[:400]}",
                proc=proc, log_path=log_path, startup_log_path=startup_log_path,
            )
        if not r.headers.get("content-type", "").startswith("application/epub+zip"):
            return fail(
                f"unexpected content-type: {r.headers.get('content-type')!r}",
                proc=proc, log_path=log_path, startup_log_path=startup_log_path,
            )
        epub_out = r.content
        if len(epub_out) < 500:
            return fail(
                f"downloaded EPUB suspiciously small ({len(epub_out)} bytes)",
                proc=proc, log_path=log_path, startup_log_path=startup_log_path,
            )

        # Re-parse via host ebooklib
        from ebooklib import ITEM_DOCUMENT
        from ebooklib import epub as epub_mod

        fd, exported_path = tempfile.mkstemp(suffix=".epub", prefix="smoke-roundtrip-")
        os.close(fd)
        Path(exported_path).write_bytes(epub_out)
        try:
            parsed = epub_mod.read_epub(exported_path)
        finally:
            try:
                os.unlink(exported_path)
            except OSError:
                pass

        titles = [t[0] for t in parsed.get_metadata("DC", "title")]
        if not any("Smoke EPUB Import" in t for t in titles):
            return fail(
                f"exported EPUB title missing; got {titles!r}",
                proc=proc, log_path=log_path, startup_log_path=startup_log_path,
            )

        docs = list(parsed.get_items_of_type(ITEM_DOCUMENT))
        chapter_docs = [d for d in docs if d.get_name().startswith("chap_")]
        if len(chapter_docs) != 2:
            return fail(
                f"expected 2 exported chapters, got {len(chapter_docs)}: "
                f"{[d.get_name() for d in chapter_docs]!r}",
                proc=proc, log_path=log_path, startup_log_path=startup_log_path,
            )
        ch1_html = chapter_docs[0].get_content().decode("utf-8")
        if "Chapter 1: The Opening (EN)" not in ch1_html:
            return fail(
                "exported chapter 1 missing translated heading; got: " + ch1_html[:400],
                proc=proc, log_path=log_path, startup_log_path=startup_log_path,
            )
        if "First chapter, translated" not in ch1_html:
            return fail(
                "exported chapter 1 missing translated body; got: " + ch1_html[:400],
                proc=proc, log_path=log_path, startup_log_path=startup_log_path,
            )
        print(f"   exported EPUB: {len(epub_out)} bytes, 2 chapters, title round-tripped", flush=True)

        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        # Drop the data dir last so a failure's log_path is still readable
        # for diagnosis up until this finally block.
        import shutil
        try:
            shutil.rmtree(data_dir, ignore_errors=True)
        except OSError:
            pass


if __name__ == "__main__":
    rc = main()
    if rc == 0:
        print("SMOKE PASS")
    sys.exit(rc)
