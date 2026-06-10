# Gotchas

Things the project keeps tripping on. Read once when something feels off; the
fix below probably matches.

## `Path.write_text` / `read_text` newline translation on Windows

Text-mode I/O silently rewrites line endings. Writing a string that already
contains `\r\n` through `write_text` translates the inner `\n` again and puts
`\r\r\n` on disk; reading it back with `read_text` (universal newlines)
collapses `\r\r\n` to `\n\n`, so every line break doubles and any
`split("\r\n")` finds nothing. This corrupted the 2026-06-10 twkan rescrape
(whole chapter bodies landed in `title_zh`, paragraph gaps became quadruple
newlines). When a file must round-trip exact bytes: write with `newline=""`,
read via `read_bytes().decode()`, and treat `\r` as stripping noise.

## Encoding detection for uploaded `.txt`

Don't assume UTF-8. Many CN raws arrive as **GBK or GB18030**. Detect via
`chardet` first; strip a leading BOM after decode. The bulk-upload code does
this in `backend/services/uploads.py::_decode_with_fallback` (moved out of
`routes/translate.py` during the B6 split).

## Chapter heading detection

Authors and scrapers use wildly different markers. `parser.py` tries, in order:

- `第\s*[\d零〇一二三四五六七八九十百千万两]+\s*[章回节]` (Arabic or Chinese numerals — chapter-level units only)
- `Chapter N` / `CH N` (English)
- `楔子` / `序章` / `序言` / `前言` / `引子` / `番外` (prologue / epilogue markers)

Volume-level dividers (`第N卷` / `第N篇` / `第N部` / `第N集`) are stripped — they
are NOT chapters and must not consume a chapter number. `_VOLUME_RE` in
`parser.py` is the place to extend.

Falls back to ~4000-char chunks split at paragraph boundaries (sequential
numbering) when no markers are found. Too-short slices are merged into the
previous chapter rather than dropped.

## Bulk upload — Starlette's 1000-file cap

Starlette's default `MultiPartParser` caps at `max_files=1000`. The `/bulk`
endpoints parse the form manually with `max_files=MAX_BULK_FILES` (10000) to
lift that. Files are read and decoded **outside** the SQLite write transaction
so a many-thousand-file batch doesn't hold the write lock for the duration of
file I/O.

## Concurrent appends — duplicate `chapter_num` violations

`_append_with_offset` uses `BEGIN IMMEDIATE` to take the write lock before
reading `MAX(chapter_num)`. Without this, two simultaneous appends on the same
novel can read the same offset and produce duplicate `chapter_num` values that
violate `UNIQUE(novel_id, chapter_num)`.

## Concurrent translates / retranslates

Every state-changing UPDATE includes a `WHERE status = '<expected>'` guard. A
concurrent `/retranslate` could otherwise be clobbered by an in-flight worker
writing `'done'` over the user's freshly-set `'pending'`. The
`reset_chapters_for_retranslate` helper guards with `status != 'translating'`
so it can't race a worker's claim.

## Orphan recovery on startup

`init_db` resets `status='translating'` → `'pending'` on startup. Worker tasks
that died with the server otherwise leave rows stuck. Queue flags
(`translate_queued`) are NOT cleared — they survive restart, and
`queue.drain_on_startup()` re-spawns workers for them so the user's picks
aren't lost.

## Malformed model output

Both backends sometimes wrap output in ```` ```json ```` fences or prepend
prose. `base.py::_strip_code_fence` tolerates this; `_unwrap_outer_fence`
handles the case where the model fences only the trailing TERMS block.
`parse_delimited_response` raises on a missing `=====BODY=====` delimiter so
the retry-then-fallback path engages instead of committing an empty chapter.

## SQLite + async

Use `aiosqlite` and `await conn.execute(...)`. Don't mix sync `sqlite3` in the
same request path.

## CORS

Not needed. The frontend is served by FastAPI's `StaticFiles`. Don't add CORS
middleware unless splitting the frontend out.

## Claude CLI on Windows

- The npm-installed shim is `claude.CMD`. Use `shutil.which` to resolve it —
  Python's `CreateProcess` doesn't apply PATHEXT.
- Wrap `.cmd` / `.bat` invocations through `cmd /c` — Python 3.13 tightened
  subprocess and direct `.cmd` execution now raises `OSError`.
- Both helpers live in `translators/_subprocess_utils.py` (`resolve_binary`,
  `build_argv`), shared across the CLI-backed translators (claude_cli,
  claude_agent, codex_cli, gemini_cli, opencode).

## Claude CLI subprocess model

Uvicorn defaults to the Selector event loop on Windows, which raises
`NotImplementedError` for `asyncio.create_subprocess_exec`. Use
`subprocess.Popen` + `asyncio.to_thread(proc.communicate)`. On
`CancelledError` / timeout, call `proc.kill()` (and `taskkill /F /T /PID` on
Windows to walk the cmd.exe → claude.CMD → node.exe process tree — a plain
`kill()` orphans the grandchild `node`).

## Claude CLI rate-limit / auth classification

Substrings like `"usage limit"`, `"5-hour limit"`, `"try again later"` →
`TransientTranslatorError` (user clicks Translate again to retry).
`"/login"`, `"not authenticated"` → `ClaudeCliError` (permanent until the
user re-logs in).

## FTS5 corruption after a column rebuild

`DROP TABLE chapter_fts` is supposed to cascade to FTS5's shadow tables
(`chapter_fts_data` / `_idx` / `_docsize` / `_config`) but in practice — with
aiosqlite inside an explicit BEGIN — the shadow tables can survive. The
subsequent `CREATE IF NOT EXISTS` then shares B-tree state with the stale
shadow rows, and any write to `chapters` that fires the FTS update trigger
raises `"database disk image is malformed."` `_drop_dead_columns` in `db.py`
explicitly drops all four shadow tables inside the rebuild to make this
deterministic.

## CRLF in DB-stored Chinese text

Chapters uploaded from Windows have `\r\n\r\n` paragraph separators (not
`\n\n`). When you split `chapters.original_text` on paragraph boundaries
for any reason (paragraph-level retranslate, etc.), normalize line
endings first: `text.replace("\r\n", "\n").split("\n\n")`.

## Translator cache hit-rate diagnosis

If you suspect the LLM cache isn't hitting when it should, look at the
INFO logs — every translation now logs one of three lines:

```
... translator cache HIT  (key abc123abc123…)
... translator cache MISS (key abc123abc123…)
... translator cache SKIP (force_retranslate, key abc123abc123…)
```

Same prefix shape for `refiner cache …`. Hit rate = `HIT / (HIT + MISS)`.
`SKIP` is the user pressing Retranslate, which deliberately bypasses the
cache read but still writes the fresh result — those aren't misses.

Two retranslates of the same chapter produce **different keys** when:

- Glossary changed between calls (an auto-detected term from a later
  chapter now appears in this chapter's `chapter_block`).
- A nearer earlier chapter has reached `status='done'` since the first
  call, so `_fetch_previous_chapter_tail` returns a different tail.
- A new style edit landed (`_fetch_style_edits` is ORDER BY id DESC, so
  the prompt's "preferred rewrites" block shifts).
- The user edited `style_note`, the genre, or the custom style brief.
- A `backend/prompts/**.md` file was edited (the system instruction text
  changes, which is part of the cache key).
- The provider's `model_id` or backend type changed.

All of those are correct cache invalidations, not bugs. If you see a
MISS that doesn't match any of those, that's worth digging into.

## No frontend test plumbing (deferred 2026-05-23)

`frontend/js/` has no test runner wired up. The 2026-05-23 audit
(`~/.claude/plans/plan-post-roadmap-working-cozy-snail.md`, Block 3.2)
considered adding Vitest with three small tests (library.js cost
math, settings.js `dataset.secretRef` selector, reader.js
`_confirmPreCheck`) and decided to defer until the next time someone
is actively editing JS. Reasoning: the project is solo, the JS
surface is small (one file per page, no framework), and the previous
shipped-JS bug (`settings.js:209` reading the wrong `<code>` for
`secret_ref`) was found and fixed inside an hour. Investing ~2.5
hours of plumbing upfront pays back only if JS changes become
frequent.

**Trigger to revisit:** the next time a JS-only bug ships, OR the
next time you make a non-trivial change to library / settings /
reader JS without rewriting the surrounding file. At that point the
gotcha-recovery cost will exceed the plumbing cost and Vitest setup
becomes the right call. Don't relitigate the decision in the
meantime.

## Stale element IDs after an HTML refactor

When an `.html` page is restructured (breadcrumb redesign, layout cleanup) its
companion `js/*.js` keeps referencing the old element IDs. Two failure modes:

- An UNGUARDED top-level `document.getElementById("x").prop = ...` where `x` was
  renamed throws `TypeError: Cannot read/set ... of null` at module load, which
  aborts the whole script: every handler below it never binds and the page looks
  totally dead. This is what silently broke the entire glossary page (`reader-link`
  renamed to `crumb-novel`).
- A GUARDED lookup (`if (el)` / `?.`) for a renamed/removed ID does not crash but
  the feature silently no-ops: the reader's TOC glossary link and source-text
  downloads (`glossary-link` -> `toc-glossary-link`, `download-*-raw` ->
  `download-*-source`) and the home cookies input (`#cookies-url` dropped from
  the URL panel) all went dead this way.

Detector (run from repo root) lists IDs each page's JS looks up that are absent
from the HTML that loads it (filter out IDs the JS creates dynamically):

```bash
for js in home library reader glossary glossary-global settings queue stats find-replace onboarding; do
  htmls=$(grep -rl "js/$js.js" frontend/*.html)
  grep -oE 'getElementById\("[a-zA-Z0-9_-]+"\)' "frontend/js/$js.js" \
    | sed -E 's/getElementById\("//;s/"\)//' | sort -u \
    | while read id; do
        for h in $htmls; do grep -q "id=\"$id\"" "$h" && continue 2; done
        echo "$js.js -> missing #$id"
      done
done
```

## Editing a CSS file without bumping its `?v=` serves stale styles

Every page links its sheets with a cache-bust query, e.g.
`<link rel="stylesheet" href="/static/css/library.css?v=2">`. The browser keys
its cache on the full URL including `?v=N`. If you edit `library.css` but leave
the link at the same `?v=`, the dev server (and any already-open client) keeps
serving the **previously cached** bytes: the change is on disk but invisible in
the running app. This reads exactly like "my fix did nothing", and it is the
usual reason a CSS edit appears not to take.

The fix: when you change a sheet's content, bump the `?v=` integer on **every**
`<link>` that references it (the same sheet is often linked from several pages,
sometimes at inconsistent versions, so grep for all of them and set them to one
new value). Skip the bump only when the change is provably render-neutral (e.g.
deleting a rule that another loaded sheet already provides identically).

To verify a responsive/CSS change, drive a headless browser with a **fresh,
cache-empty context per run** (Playwright `browser.new_context()` with an
explicit `viewport`). A cache-empty context reads the current file from disk
regardless of `?v=`, so it measures the actual edit, not a cached copy. The
Playwright **MCP** `browser_resize` was unreliable for this in practice; a short
standalone script with `viewport={width,height}` was the dependable path. The
overflow probe that found every break this round:

```
() => {
  const vw = window.innerWidth, sw = document.scrollingElement.scrollWidth;
  return { vw, scrollW: sw, overflow: sw > vw };
}
```

`scrollW > innerWidth` means the page scrolls sideways (the bug); they should be
equal.

## Equal-specificity `@media` overrides must come later in source order

A responsive override like `@media (max-width: 720px) { main { padding: ... } }`
has the **same specificity** as a base `main { padding: ... }` rule, and a media
query does not raise specificity. The cascade then breaks the tie by source
order: if the base rule appears *after* the media-query rule in the file, the
desktop value wins even on a phone and the override silently does nothing. Put
narrow-viewport overrides at the **end** of the sheet (or raise their
specificity deliberately), not next to the breakpoint they conceptually belong
with. This bit the first attempt at the phone-gutter fix in `base.css`.
