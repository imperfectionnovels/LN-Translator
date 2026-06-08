"""Translate entry points: paste, upload (.txt), and bulk multi-file upload.

Imports leave chapters in `status='pending'` with no work scheduled.
Translation only runs on chapters the user explicitly queues via the
per-chapter endpoints in `routes/chapters.py`. The process-global lock in
`services/queue.py` keeps execution strictly serial.

The byte-level decoding and the transactional novel / chapter inserts live
in `backend/services/uploads.py`. This file is the HTTP layer only —
multipart form parsing, .txt filename gating, the author-note bulk skip, and
thin handler bodies that call into the upload service.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from starlette.datastructures import UploadFile as StarletteUploadFile

from backend.db import get_conn
from backend.genres import normalize_and_validate_genre
from backend.models import (
    MAX_TITLE_CHARS,
    AppendPasteRequest,
    InsertChapterRequest,
    PasteRequest,
    ScrapeRequest,
)
from backend.services.covers import write_cover_for_novel

# Volume divider pattern (第N卷 …) lives in parser.py and is consumed in
# parse_chapters' opening step. The bulk path needs the same stripping so a
# file starting with `第一卷\n第296章 ...` doesn't lose the printed-number
# heading to the volume line. Imported here to avoid duplicating the regex.
from backend.services.parser import (
    _VOLUME_RE,  # noqa: PLC2701
    CHAPTER_PATTERNS,
    ParsedChapter,
    extract_heading_number,
    has_author_note_markers,
    is_non_chapter_block,
    parse_chapters,
    reconcile_chapter_numbers,
    split_leading_heading,
)
from backend.services.scraper import ScrapeError, scrape_url
from backend.services.uploads import (
    MAX_BULK_FILES,
    MAX_BULK_TOTAL_BYTES,
    DecodedDoc,
    append_parsed_chapters,
    append_with_offset,
    create_novel_and_chapters,
    decode_docx,
    decode_epub,
    decode_html,
    ensure_novel_exists,
    ext_from_filename,
    insert_parsed_chapters,
    read_bulk_file,
    read_text_file,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def normalize_title(raw: str | None) -> str:
    """Single source of truth for novel-title validation. Strips, rejects
    whitespace-only / empty, truncates to MAX_TITLE_CHARS. /paste already
    enforces these via Pydantic on PasteRequest; /upload and /bulk used
    Form(...) with no validation, which let through whitespace-only or
    very long titles. This helper closes the asymmetry — every import path
    now applies the same DB-safe contract.

    Silent truncation (rather than 413) on length: a paste from a long
    source can include trailing noise, and the user's intent is clearly
    "use the first 200 chars as the title."
    """
    title = (raw or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    return title[:MAX_TITLE_CHARS]


_ALLOWED_UPLOAD_EXTS = frozenset({"txt", "epub", "docx", "html", "htm"})


def _validate_genre(genre: str | None) -> str | None:
    """Normalize + validate a user-supplied genre key for the import routes.

    Thin wrapper over the single-source `genres.normalize_and_validate_genre`
    so paste/upload/bulk/scrape and the novel-PATCH path can't drift: empty /
    None -> None (column stays NULL), unknown key -> 400."""
    return normalize_and_validate_genre(genre)


def _validate_upload_filename(filename: str | None) -> str:
    """Return the format key for a multiformat upload (txt / epub / docx /
    html), or raise 400 when the extension is unsupported. Empty filename
    is permitted (some multipart clients omit it) — defaults to 'txt' so
    the legacy paste-as-file behavior holds."""
    if not filename:
        return "txt"
    ext = ext_from_filename(filename)
    if ext == "htm":
        ext = "html"
    if ext not in _ALLOWED_UPLOAD_EXTS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unsupported file type '{filename}'; expected .txt, .epub, "
                ".docx, or .html"
            ),
        )
    return ext


def _assert_txt_filename(filename: str | None) -> None:
    """TXT-only filename guard used by the bulk endpoints. Bulk semantics
    are "each file = one chapter," which only meaningfully applies to plain
    text; EPUB and DOCX carry their own internal chapter structure so they
    route through the single-file /upload endpoint, not /bulk.

    Empty filename is permitted (some multipart clients omit it for inline
    parts)."""
    if filename and not filename.lower().endswith(".txt"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"only .txt files are supported in bulk upload (got "
                f"'{filename}'); use /upload for .epub or .docx."
            ),
        )


async def _decode_upload(file: UploadFile) -> tuple[DecodedDoc, str]:
    """Dispatch a single-file upload to the format-appropriate decoder.

    Returns (DecodedDoc, source_type) where source_type is one of the
    SourceType literals ('txt' | 'epub' | 'docx' | 'html'). The caller
    persists source_type with the novel and inspects DecodedDoc.cover_bytes
    to decide whether to also call write_cover_for_novel.

    .txt uploads flow through read_text_file for byte-level encoding
    detection (the existing CJK-density scoring) so the multi-format
    dispatcher doesn't regress raw-text imports.
    """
    ext = _validate_upload_filename(file.filename)
    if ext == "txt":
        text, encoding = await read_text_file(file)
        # read_text_file enforces MAX_UPLOAD_BYTES and 400s on empty, but
        # doesn't expose the raw byte count — DecodedDoc.raw_size is None-
        # safe for TXT callers because they don't use it.
        return DecodedDoc(text=text, encoding=encoding, raw_size=0), "txt"
    if ext == "epub":
        return await decode_epub(file), "epub"
    if ext == "docx":
        return await decode_docx(file), "docx"
    if ext == "html":
        return await decode_html(file), "html"
    # _validate_upload_filename already filtered; defensive:
    raise HTTPException(status_code=400, detail=f"unsupported file type '{file.filename}'")


@router.post("/preview")
async def translate_preview(
    request: Request,
) -> dict:
    """Pre-flight chapter-detection preview for the import gate (F05/F06/F08).

    Accepts a multipart upload (paste text via `text` field, or a single
    file via `file` field) or a JSON body with `text`. Returns:

      {
        detected_chapters: int,
        headings: list[str],          # first 5 detected heading lines
        first_chapter_first_500: str, # the first chapter body, truncated
        format_path: str,             # 'text' | 'epub_spine' | 'docx_headings'
      }

    No DB writes; safe to call repeatedly. Mirrors the parse path used by
    the actual import so the user sees what the importer will commit
    before they spend on it.
    """
    ctype = (request.headers.get("content-type") or "").lower()
    text: str | None = None
    pre_parsed: list[ParsedChapter] | None = None
    format_path = "text"
    if "multipart/form-data" in ctype:
        form = await request.form()
        if "file" in form:
            file = form["file"]
            if isinstance(file, StarletteUploadFile):
                decoded, source_type = await _decode_upload(file)
                if decoded.pre_parsed_chapters:
                    pre_parsed = decoded.pre_parsed_chapters
                    format_path = (
                        "epub_spine" if source_type == "epub"
                        else "docx_headings" if source_type == "docx"
                        else "structured"
                    )
                else:
                    text = decoded.text
        if text is None and pre_parsed is None and "text" in form:
            text_val = form["text"]
            text = str(text_val) if not isinstance(text_val, str) else text_val
    else:
        try:
            body = await request.json()
            text = (body or {}).get("text")
        except Exception:
            text = None

    if pre_parsed is None and not text:
        raise HTTPException(
            status_code=400,
            detail="preview requires either a `text` field or an uploaded `file`",
        )

    if pre_parsed is None:
        try:
            pre_parsed = parse_chapters(text or "")
        except HTTPException:
            pre_parsed = []

    headings = [
        (ch.title_zh or f"(chapter {ch.chapter_num})")[:120]
        for ch in pre_parsed[:5]
    ]
    first_body = ""
    if pre_parsed:
        first_body = (pre_parsed[0].original_text or "")[:500]
    return {
        "detected_chapters": len(pre_parsed),
        "headings": headings,
        "first_chapter_first_500": first_body,
        "format_path": format_path,
    }


@router.post("/paste")
async def translate_paste(
    payload: PasteRequest,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    title = normalize_title(payload.title)
    genre = _validate_genre(payload.genre)
    novel_id, first_chapter = await create_novel_and_chapters(
        conn, title=title, text=payload.text, source_type="paste", genre=genre,
    )
    return {"novel_id": novel_id, "first_chapter": first_chapter}


@router.post("/upload")
async def translate_upload(
    file: UploadFile = File(...),
    title: str = Form(...),
    genre: str | None = Form(None),
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    title = normalize_title(title)
    genre_norm = _validate_genre(genre)
    decoded, source_type = await _decode_upload(file)
    # 2026-05-25 (F07): EPUB spine / DOCX heading paths surface a
    # structured chapter list. Use it directly via atomic_create_novel
    # rather than running parse_chapters over the flattened text blob.
    # Source-language detection runs on the first chapter's body.
    if decoded.pre_parsed_chapters:
        from backend.services.import_runner import insert_chapters_incrementally  # noqa: PLC0415
        from backend.services.lang_detect import detect_source_language  # noqa: PLC0415
        chapters = decoded.pre_parsed_chapters
        detected_lang = detect_source_language(chapters[0].original_text)
        # Structured uploads (EPUB / DOCX / HTML) can carry hundreds of
        # chapters — large enough that a single atomic transaction adds
        # real interruption surface. Use the resumable-import shape: novel
        # row with import_status='in_progress' + per-batch commits, flipped
        # to 'done' on the last batch. Drain-on-startup flips to 'paused'
        # if a crash occurred mid-INSERT (no source to re-fetch).
        novel_id = await insert_chapters_incrementally(
            title=title, decoded_chapters=chapters, source_type=source_type,
            source_url=None, genre=genre_norm, source_language=detected_lang,
        )
        first_chapter = chapters[0].chapter_num
    else:
        novel_id, first_chapter = await create_novel_and_chapters(
            conn, title=title, text=decoded.text, source_type=source_type,
            genre=genre_norm,
        )
    cover_written = False
    if decoded.cover_bytes:
        # EPUB shipped an embedded cover. Reuse the same writer the HTTP
        # cover-upload route uses so EPUB covers and uploaded covers land
        # identically on disk + in novels.cover_image_path.
        # source_type drives the cover_source label: only EPUB ships embedded
        # covers in this upload path today, so 'epub' is the only value we
        # produce here; the literal source_type happens to match.
        result = await write_cover_for_novel(
            conn, novel_id, decoded.cover_bytes, ext_hint=decoded.cover_ext,
            source=source_type,
        )
        if result is not None:
            cover_written = True
        await conn.commit()
    return {
        "novel_id": novel_id,
        "first_chapter": first_chapter,
        "detected_encoding": decoded.encoding,
        "source_type": source_type,
        "cover_extracted": cover_written,
    }


@router.post("/scrape")
async def translate_scrape(
    payload: ScrapeRequest,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Fetch a URL, extract main article text via trafilatura, then route
    the text through the same pipeline as /paste (when novel_id is omitted)
    or /append/{novel_id}/paste (when supplied).

    Every response carries a stable `"mode"` discriminator so a client can
    switch on one field instead of re-deriving recipe-vs-generic and
    create-vs-append from its own request:
      * `"job"`      — recipe URL, background import job (poll /scrape/jobs).
      * `"appended"` — generic URL appended to an existing novel.
      * `"created"`  — generic URL created a fresh novel.

    Two flow shapes:

    1. **Recipe URLs** (69shuba, syosetu, uukanshu, piaotian). The crawl
       can take 25+ minutes for a 1500-chapter novel. The route creates
       a `scrape_jobs` row, fires `asyncio.create_task(run_job(...))`,
       and returns `{job_id, status: 'pending'}` immediately. The
       frontend polls `GET /scrape/jobs/{id}` for progress and
       navigates to the reader on `status='done'`. Closing the request
       does NOT cancel the task — the recipe owns its own DB
       connection and runs to completion in the FastAPI worker.

    2. **Generic URLs** (everything else — single blog posts, etc.).
       Fast path; the request blocks for the few seconds it takes
       trafilatura to extract the article body, then returns the novel.
       No job row created.

    Security guards live in `backend/services/scraper.py` — SSRF
    rejection, 10 MB response cap, 15 s timeout, http(s)-only schemes.
    Any failure surface in the recipe path is captured on the job row's
    `error_message` + `error_kind`; failures in the generic path raise
    HTTPException(400) directly.
    """
    # Decide recipe-vs-generic before touching scrape_url so we know
    # whether to background. dispatch is cheap (one urlparse + a few
    # dict lookups).
    from urllib.parse import urlparse  # noqa: PLC0415

    from backend.services.scrapers import dispatch  # noqa: PLC0415
    parsed = urlparse(payload.url) if payload.url else None
    recipe = dispatch(parsed.hostname) if parsed is not None else None

    if recipe is not None:
        # A per-site recipe matched. Recipe imports always create a fresh
        # novel via the resumable import_runner (background job) — they do
        # not support append-mode. Reject append BEFORE doing any fetch so
        # we never run a full crawl just to 400 afterward.
        if payload.novel_id is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Append-mode URL import is not supported for sites with "
                    "a per-site recipe (e.g. 69shuba). The recipe creates a "
                    "fresh novel from the URL; to add chapters to an "
                    "existing novel from this site, delete the duplicate "
                    "afterwards or use a different import method."
                ),
            )
        from backend.services import scrape_jobs  # noqa: PLC0415
        job_id = await scrape_jobs.create_job(payload.url)
        # Fire-and-forget. The task survives the request returning. The
        # job runs through import_runner.start_from_recipe (resumable
        # skeleton + per-chapter fill), not the route.
        scrape_jobs.spawn(job_id, payload.url, payload.cookies)
        return {
            "mode": "job",
            "job_id": job_id,
            "status": "pending",
            "recipe": True,
            "background": True,
        }

    # Generic (non-recipe) URL: trafilatura article extraction. No conn is
    # passed — recipe dispatch is handled entirely above, so scrape_url
    # always returns a ScrapeResult here.
    try:
        result = await scrape_url(payload.url, cookies=payload.cookies)
    except ScrapeError as e:
        # 2026-05-25 (F06): differentiated error UI. Surface error_kind
        # in the JSON so the frontend can render per-cause recovery
        # affordances (CF block → cookies tutorial; auth → "log in";
        # timeout → retry with longer cap; etc.).
        raise HTTPException(
            status_code=400,
            detail={
                "message": str(e),
                "error_kind": getattr(e, "error_kind", "unknown"),
            },
        ) from e

    if payload.novel_id is not None:
        # Append flow. The novel must already exist.
        await ensure_novel_exists(conn, payload.novel_id)
        added, first, collision = await append_parsed_chapters(
            conn, payload.novel_id, result.text,
        )
        return {
            "mode": "appended",
            "novel_id": payload.novel_id,
            "added_chapters": added,
            "first_new_chapter": first,
            "first_chapter": first,
            "chapter_num_collision": collision,
            "scraped_url": result.source_url,
        }

    # Create flow. Use the user-supplied title when present, otherwise
    # the scraper's extracted title (or hostname fallback). Persist the
    # source_url alongside source_type='url' so the library/detail model
    # can show users where the chapters came from.
    title_source = payload.title if payload.title else result.title
    title = normalize_title(title_source)
    genre = _validate_genre(payload.genre)
    novel_id, first_chapter = await create_novel_and_chapters(
        conn, title=title, text=result.text, source_type="url",
        source_url=result.source_url, genre=genre,
    )
    # Optional cover from og:image / twitter:image. Failure here is silent
    # by design — the import already succeeded; a bad scraped image must not
    # 500 the response. write_cover_for_novel returns None on any image
    # validation problem and we just swallow that.
    cover_written = False
    if result.cover_bytes:
        try:
            written = await write_cover_for_novel(
                conn, novel_id, result.cover_bytes,
                ext_hint=result.cover_ext, source="url",
            )
            if written is not None:
                cover_written = True
                await conn.commit()
        except Exception:
            logger.exception(
                "cover write failed for scraped novel %d (continuing)", novel_id,
            )
    return {
        "mode": "created",
        "novel_id": novel_id,
        "first_chapter": first_chapter,
        "scraped_url": result.source_url,
        "scraped_title": result.title,
        "cover_extracted": cover_written,
    }


@router.get("/scrape/jobs/{job_id}")
async def get_scrape_job(job_id: int) -> dict:
    """Poll endpoint for background scrape jobs created by POST /scrape
    against a recipe URL. Returns the latest progress + status. The
    frontend polls every 1.5s.

    Response shape:
      status: 'pending' | 'running' | 'done' | 'error'
      step: 'fetching_overview' | 'fetching_chapters' | 'writing' | null
      current / total: chapter counters (both 0 before the chapter list
        is known)
      novel_id: populated on status='done'
      scraped_title: populated as soon as the recipe extracts it
      error_message + error_kind: populated on status='error'
    """
    from backend.services import scrape_jobs  # noqa: PLC0415
    job = await scrape_jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="scrape job not found")
    return job


@router.post("/append/{novel_id}/paste")
async def append_paste(
    novel_id: int,
    payload: AppendPasteRequest,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    await ensure_novel_exists(conn, novel_id)
    added, first, collision = await append_parsed_chapters(conn, novel_id, payload.text)
    return {
        "novel_id": novel_id,
        "added_chapters": added,
        "first_new_chapter": first,
        "first_chapter": first,  # alias for cross-endpoint consistency
        "chapter_num_collision": collision,
    }


@router.post("/append/{novel_id}/upload")
async def append_upload(
    novel_id: int,
    file: UploadFile = File(...),
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    await ensure_novel_exists(conn, novel_id)
    decoded, source_type = await _decode_upload(file)
    added, first, collision = await append_parsed_chapters(conn, novel_id, decoded.text)
    return {
        "novel_id": novel_id,
        "added_chapters": added,
        "first_new_chapter": first,
        "first_chapter": first,
        "detected_encoding": decoded.encoding,
        "source_type": source_type,
        "chapter_num_collision": collision,
    }


@router.post("/insert/{novel_id}")
async def insert_chapter(
    novel_id: int,
    payload: InsertChapterRequest,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Insert pasted chapter(s) into the MIDDLE of a novel, immediately after
    `after_chapter_num`, renumbering the tail. Fills a chapter missed during
    import. Unlike /append/* (which only lands at the end), this places the
    chapter at a chosen position. The inserted chapter is 'pending'; nothing
    auto-translates."""
    await ensure_novel_exists(conn, novel_id)
    added, first = await insert_parsed_chapters(
        conn, novel_id, payload.after_chapter_num, payload.text, payload.title,
    )
    return {
        "novel_id": novel_id,
        "added_chapters": added,
        "first_new_chapter": first,
        "first_chapter": first,  # alias for cross-endpoint consistency
    }


@router.post("/bulk")
async def translate_bulk(
    request: Request,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Create a new novel from N raw files. Each file = one chapter, in upload order.
    Filename (sans extension) is the chapter title. Does NOT auto-queue translation."""
    title, files, genre_raw = await _parse_bulk_form(request)
    title = normalize_title(title)
    genre = _validate_genre(genre_raw)
    parsed, skipped, skipped_nonchapter, encodings, skipped_details = (
        await _files_to_chapters(files, start_num=1)
    )
    if not parsed:
        raise HTTPException(status_code=400, detail="all files were empty")
    # Bulk path bypasses create_novel_and_chapters (no big-text parse),
    # so detect source_language here from the first file's chapter text
    # before INSERTing the novel row.
    from backend.services.import_runner import insert_chapters_incrementally  # noqa: PLC0415
    from backend.services.lang_detect import detect_source_language  # noqa: PLC0415
    detected_lang = detect_source_language(parsed[0].original_text)
    # Resumable-import shape: novel row created with import_status=
    # 'in_progress'; chapters INSERTed in commit-per-batch chunks; flipped
    # to 'done' on the last batch. A crash mid-INSERT leaves the novel
    # with the chapters that did commit + status='in_progress'; drain_on_
    # startup flips it to 'paused' since bulk uploads have no re-fetchable
    # source.
    novel_id = await insert_chapters_incrementally(
        title=title, decoded_chapters=parsed, source_type="txt",
        source_url=None, genre=genre, source_language=detected_lang,
    )
    first_chapter = min(ch.chapter_num for ch in parsed)
    return {
        "novel_id": novel_id,
        "first_chapter": first_chapter,
        "added_chapters": len(parsed),
        "skipped_files": skipped,
        "skipped_nonchapter": skipped_nonchapter,
        "skipped_details": skipped_details,
        "detected_encodings": encodings,
        "translation_queued": False,
    }


@router.post("/append/{novel_id}/bulk")
async def append_bulk(
    novel_id: int,
    request: Request,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Append N raw files as chapters to an existing novel. Same rules as /bulk."""
    _, files, _ = await _parse_bulk_form(request, require_title=False)
    await ensure_novel_exists(conn, novel_id)
    # Decode files OUTSIDE the write transaction so a many-thousand-file batch
    # doesn't hold the SQLite write lock for the duration of file I/O.
    parsed, skipped, skipped_nonchapter, encodings, skipped_details = (
        await _files_to_chapters(files, start_num=1)
    )
    if not parsed:
        raise HTTPException(status_code=400, detail="all files were empty")
    first_new, collision = await append_with_offset(conn, novel_id, parsed)
    return {
        "novel_id": novel_id,
        "added_chapters": len(parsed),
        "first_new_chapter": first_new,
        "first_chapter": first_new,
        "skipped_files": skipped,
        "skipped_nonchapter": skipped_nonchapter,
        "skipped_details": skipped_details,
        "detected_encodings": encodings,
        "translation_queued": False,
        "chapter_num_collision": collision,
    }


async def _parse_bulk_form(
    request: Request, require_title: bool = True
) -> tuple[str, list[UploadFile], str | None]:
    """Manual multipart parse for the bulk endpoints. Starlette's default
    MultiPartParser caps at max_files=1000, which a long xianxia trips;
    request.form() with max_files=MAX_BULK_FILES lifts that. The match-by-class
    filter drops stray non-file form parts so a malformed client can't sneak
    in plain strings under the 'files' name.

    Returns (title, files, genre). `genre` is an optional form field — the
    bulk import picker sends it; legacy clients omit it (None)."""
    form = await request.form(max_files=MAX_BULK_FILES, max_fields=MAX_BULK_FILES)
    files = [f for f in form.getlist("files") if isinstance(f, StarletteUploadFile)]
    if not files:
        raise HTTPException(status_code=400, detail="no files provided")
    # request.form() above enforces max_files=MAX_BULK_FILES already; a post-form
    # length recheck is redundant.
    title_val = form.get("title")
    genre_val = form.get("genre")
    genre_out = genre_val if isinstance(genre_val, str) else None
    if require_title:
        if not isinstance(title_val, str) or not title_val.strip():
            raise HTTPException(status_code=400, detail="title required")
        return title_val, files, genre_out
    return (title_val if isinstance(title_val, str) else ""), files, genre_out


def _is_numberless_prologue_heading(heading: str) -> bool:
    """True when `heading` matches the numberless prologue pattern in
    CHAPTER_PATTERNS (楔子 / 序章 / 序言 / 前言 / 引子 / 番外). Used by the
    bulk-import author-note skip: a file titled `番外+活动预告！` carries a
    matched heading (so is_non_chapter_block doesn't run on it under
    split_leading_heading), but is still an announcement post, not a real
    extra. Numbered 第N章 headings are never treated this way — those are
    real chapters even if the body mentions 求月票."""
    # CHAPTER_PATTERNS[3] is the prologue pattern; matching here keeps the
    # source of truth in parser.py.
    return bool(CHAPTER_PATTERNS[3].match(heading))


async def _files_to_chapters(
    files: list[UploadFile], start_num: int
) -> tuple[list[ParsedChapter], int, int, list[str], list[dict[str, str]]]:
    """Decode each file and produce ParsedChapter objects in input order. Empty
    files are skipped. Returns (parsed_chapters, skipped_count,
    skipped_nonchapter_count, encodings, skipped_details) where `encodings` is
    the distinct sorted set of encodings detected across the batch — surfaces
    "chardet flagged this batch as half utf-8 half gb18030" in the response so
    the user can spot mixed-source imports. `skipped_details` names each dropped
    file with a reason ({"name", "reason"}) so a bulk import that silently lost
    a file is no longer a bare count: the user can see which file and why.

    A heading-less file that reads as an author's update post (求月票, 请假 …)
    is dropped from the chapter list — counting it as a chapter would take a
    number and shift every chapter after it. The same drop applies to a file
    whose first line matches the numberless prologue pattern AND whose body
    matches author-note vocabulary (`番外+活动预告！`), which the heading-
    matched branch used to let through silently.

    Volume divider lines (第N卷 / 第N篇 / …) are stripped from each file before
    heading extraction, matching parse_chapters. Without this, a file starting
    with `第一卷 风起云涌\\n第296章 ...` was treated as heading-less and lost
    its printed chapter number.

    The aggregate decoded-byte size across the batch is capped at
    MAX_BULK_TOTAL_BYTES so a misclick on a directory of 10000 × 25 MB files
    can't OOM the process. The check fires inside the per-file loop so the
    failing file is named in the 413 response."""
    parsed: list[ParsedChapter] = []
    skipped = 0
    skipped_nonchapter = 0
    skipped_details: list[dict[str, str]] = []
    num = start_num
    encodings: set[str] = set()
    total_bytes = 0
    for f in files:
        _assert_txt_filename(f.filename)
        fname = f.filename or "(unnamed file)"
        result = await read_bulk_file(f)
        if result is None:
            skipped += 1
            skipped_details.append(
                {"name": fname, "reason": "empty or could not be decoded"}
            )
            continue
        text, encoding, raw_size = result
        total_bytes += raw_size
        if total_bytes > MAX_BULK_TOTAL_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"bulk upload exceeded total size cap "
                    f"({MAX_BULK_TOTAL_BYTES} bytes); failing file "
                    f"'{f.filename}'"
                ),
            )
        encodings.add(encoding)
        # Strip volume / part divider lines (第N卷 …) before heading extraction
        # — same as parse_chapters does as its first step. Otherwise a file
        # starting with `第一卷\\n第296章 ...` reads as heading-less and loses
        # the printed number.
        text = _VOLUME_RE.sub("", text).lstrip("\n")
        # One file = one chapter. The real chapter title is usually the first
        # line (第N章 …) — extract it and drop it from the body. Only if the
        # file has no heading do we consider the filename: a meaningful stem
        # ("prologue") is kept, a numeric one ("0002") is discarded as a
        # non-title rather than fed to the translator as a source title.
        heading, body = split_leading_heading(text)
        if heading is not None:
            # Author-note skip even when a heading was matched: a numberless
            # prologue token (番外 etc.) combined with announcement vocabulary
            # in the body means an update post, not a real extra. Numbered
            # 第N章 headings are never skipped — those are real chapters even
            # if the body mentions 求月票 etc.
            if (
                _is_numberless_prologue_heading(heading)
                and has_author_note_markers(body)
            ):
                skipped_nonchapter += 1
                skipped_details.append(
                    {"name": fname, "reason": "author note (announcement post)"}
                )
                logger.info(
                    "bulk: skipping numberless-heading author-note file '%s' "
                    "(heading=%r)",
                    f.filename, heading,
                )
                continue
            title_zh: str | None = heading
            printed = extract_heading_number(heading)
        else:
            body = text
            # A heading-less file that is an author update post, not a story
            # chapter — exclude it so it doesn't consume a chapter number.
            if is_non_chapter_block(body):
                skipped_nonchapter += 1
                skipped_details.append(
                    {"name": fname, "reason": "non-chapter block (author note)"}
                )
                logger.info("skipped non-chapter file: %s", f.filename)
                continue
            stem = Path(f.filename).stem if f.filename else ""
            title_zh = None if (not stem or stem.isdigit()) else stem
            # A purely numeric filename ("0298.txt") IS the chapter number even
            # though it isn't kept as a title — feed it to reconciliation.
            printed = int(stem) if stem.isdigit() else extract_heading_number(stem)
        parsed.append(
            ParsedChapter(
                chapter_num=num,  # placeholder; reconciled below
                title_zh=title_zh,
                original_text=body,
                printed_num=printed,
            )
        )
        num += 1
    # One file = one chapter, so a heading number (or numeric filename) is the
    # authoritative chapter number — anchor to it just like parse_chapters does.
    reconcile_chapter_numbers(parsed)
    return parsed, skipped, skipped_nonchapter, sorted(encodings), skipped_details
