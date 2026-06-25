# CLAUDE.md

Guidance for Claude (and other coding agents) when working on this project.

## Project overview

Local single-user app — runs as a Uvicorn web server or as a packaged Windows desktop EXE — that translates Chinese light novels into English. Users paste text, upload `.txt` / `.docx` / `.epub` / `.html` files (single or bulk), or paste a public URL; the backend parses chapters, translates them one at a time using a user-selected AI provider, auto-builds a per-novel glossary so terminology stays consistent, and serves a browser reader with bilingual side-by-side mode. Per-novel settings pick the translator provider, an optional refinement provider, and a genre (xianxia, wuxia, modern-romance, isekai, slice-of-life, mystery, litrpg, sci-fi, fantasy, yuri-bl). First-run launches a welcome wizard that walks the user through adding a provider before the rest of the UI unlocks.

**One LLM call per chapter from the selected provider.** No humanizer in the default flow; an opt-in per-novel refinement pass runs a second provider over the draft when configured. Each prompt carries the glossary, the previous-chapter tail, captured per-paragraph style edits, and the genre-aware system instruction composed from `backend/prompts/`. The translator owns every correctness axis AND the English prose itself.

## Tech stack

- **Backend**: Python 3.11+, FastAPI, Uvicorn, `aiosqlite`.
- **Default translator**: `claude_agent` backend — in-process Claude Agent SDK, Fable 5, `effort=high`. Burns the local Claude subscription window; serial (one chapter at a time).
- **Other supported translators** (configurable per-novel via the providers table; bootstrap-seeded from `TRANSLATOR_BACKEND`): `claude_cli` (subprocess, no thinking-config), `gemini` (Gemini API), `deepseek` (OpenAI-compatible single-pass translator at api.deepseek.com), `google_translate_free` (free tier — Google Translate via the `deep-translator` library, no API key; hits Google's public web endpoint). Plus 14 more provider types in the catalog (`codex_cli`, `gemini_cli`, `opencode`, `anthropic_api`, `openai`, `xai`, `mistral`, `openrouter`, `qwen`, `zhipu`, `moonshot`, `groq`, `openai_compatible`, `ollama`); the single source of truth is `backend/services/translator_catalog.py::_CATALOG`, which `services/providers.py::KNOWN_PROVIDER_TYPES` and `services/translators/factory.py::_DISPATCH` derive from (catalog-parity test pins both ends). See `docs/backends.md` for tuning knobs on the primary backends.
- **Encoding detection**: `chardet` for uploaded `.txt` files (often arrive as GBK / GB18030).
- **Frontend**: plain HTML + vanilla JS, no framework, no build step.
- **Storage**: SQLite at `<USER_DATA_ROOT>/novels.db` in WAL mode (`data/novels.db` in dev, `%APPDATA%\LN-Translator\novels.db` when frozen).
- **Desktop runtime**: `pywebview` + WebView2 for the native window; `keyring` for OS-credential-store secrets; PyInstaller `--onedir` bundle via `LN-Translator.spec`.

## Directory layout

```
.
├── CLAUDE.md, AGENTS.md, README.md, LICENSE, .env.example
├── pyproject.toml, .gitignore, LN-Translator.spec
├── docs/
│   ├── backends.md            # per-backend tuning knobs
│   ├── exe-build.md           # PyInstaller bundle + first-run wizard
│   ├── gotchas.md             # recurring CODE pitfalls + their fix
│   └── decisions.md           # decisions & lessons log: the WHY, settled calls, ruled-out alternatives, mistakes-and-corrections
├── backend/
│   ├── main.py                # FastAPI app, lifespan, _probe_backends
│   ├── app_entry.py           # frozen-mode entry orchestrator: main(), first-run routing, _run_uvicorn (re-exports the app_* helpers)
│   ├── app_ports.py, app_platform.py, app_ui.py, app_shutdown.py  # app_entry helpers: port probe + sentinel, win32 console/Popen shim, pywebview/browser UI, server-ref + _signal_shutdown bridge
│   ├── config.py              # env vars, USER_DATA_ROOT / PROJECT_ROOT resolution
│   ├── db.py                  # SCHEMA, _ADDITIVE_MIGRATIONS, init_db, _drop_dead_columns, drain_on_startup hooks
│   ├── models.py              # Pydantic models
│   ├── genres.py              # GENRES registry + resolve_genre()
│   ├── routes/                # 16 routers, mounted under /api
│   │   ├── translate.py           # /paste, /upload, /bulk + /append/* + /insert (mid-novel) + /scrape
│   │   ├── novels.py              # /novels list/get/patch/delete + downloads
│   │   ├── chapters.py            # /chapters, /retranslate, /edit-paragraph, /retry-refinement
│   │   ├── glossary.py            # per-novel CRUD + /affected-chapters + /retranslate-affected
│   │   ├── global_glossary.py     # cross-novel glossary
│   │   ├── providers.py           # CRUD + /test + /set-default + /set-secret + DELETE /secret
│   │   ├── genres.py              # GET /api/genres for the UI dropdown
│   │   ├── config_kv.py           # /config/{key} GET/PUT — first_run_complete + novel_defaults live here
│   │   ├── observations.py        # observer hits (read-only)
│   │   ├── imports.py             # resumable scrape/import job status feed
│   │   ├── stats.py, cache.py, bookmarks.py, find_replace.py, tm.py  # (cover endpoints fold into novels.py)
│   │   ├── quality.py             # cockpit: /novels/{id}/quality + /consistency + /chapters/{n}/quality (read-only); chapters.py adds /learn-edits (+/commit)
│   ├── services/
│   │   ├── parser.py              # chapter heading detection, reconcile_chapter_numbers
│   │   ├── uploads.py             # file decode (txt/docx/epub/html) + transactional novel/chapter insert
│   │   ├── scraper.py             # URL fetch with SSRF + size + timeout guards
│   │   ├── queue.py               # the translator worker (single asyncio.Lock, serial)
│   │   ├── refiner.py             # Phase-4 refinement worker (chains off queue under same lock)
│   │   ├── glossary.py, glossary_filters.py, glossary_casing.py   # admit / lock / cased normalization
│   │   ├── global_glossary.py, tm.py, find_replace.py             # cross-novel helpers
│   │   ├── text_fixups.py         # deterministic enforce_* transforms (em-dash, brackets, casing)
│   │   ├── text_observers.py      # detect_* observers (log-only, no retry)
│   │   ├── observations.py        # normalize observer outputs into NormalizedObservation rows
│   │   ├── llm_cache.py           # content-addressed on-disk cache (USER_DATA_ROOT/llm_cache)
│   │   ├── providers.py           # Provider dataclass, CRUD, set_default, resolve_secret, ensure_default_provider
│   │   ├── pre_check.py           # chapter_saturation glossary/OCR preflight
│   │   ├── prompt_inputs.py       # per-novel prompt-input fetchers (style edits/note, prev-chapter tail, provider resolve)
│   │   ├── translator_catalog.py  # _CATALOG: single source of truth for provider types / models / form defaults
│   │   ├── free_draft_queue.py    # mechanical NMT draft lane (own FREE_DRAFT_LOCK)
│   │   ├── import_runner.py, scrape_jobs.py        # resumable recipe/bulk import (skeleton + fill)
│   │   ├── translation_attempts.py, fr_snapshots.py, soft_delete.py, genres_novel.py, lang_detect.py
│   │   ├── quality_dashboard.py   # cockpit read service: wraps quality_report/consistency_eval cores with a run_in_threadpool offload + pull-based version-token cache
│   │   ├── learn_from_edits.py    # build_proposal/commit_proposal: route a chapter's captured style_edits -> glossary casing fixes + brief notes + ground-truth (stage-then-commit)
│   │   ├── epub_export.py, covers.py, stats.py
│   │   ├── scrapers/              # per-site recipe registry: base.py + cloudflare/piaotian/sixnineshu/syosetu/uukanshu/...
│   │   └── translators/
│   │       ├── base.py            # BaseTranslator, build_prompt, parse_delimited_response, build_system_instruction(genre, custom_brief)
│   │       ├── factory.py         # get_translator(provider) routes by provider_type; translator_factory() = legacy startup-probe shim
│   │       ├── claude_agent.py    # Claude Agent SDK (subscription auth)
│   │       ├── claude_cli.py, _claude_errors.py  # claude subprocess wrapper + Claude SDK error classification (claude_agent/claude_cli)
│   │       ├── gemini.py          # Google Gemini API
│   │       ├── deepseek.py        # OpenAI-compatible single-pass translator (delimited envelope)
│   │       ├── openai_compatible.py, _openai_errors.py  # shared OpenAI-SDK base + transient-retry helper (openai/xai/mistral/qwen/zhipu/moonshot/groq/openrouter/ollama subclasses)
│   │       ├── anthropic_api.py, google_translate_free.py  # Anthropic API + free Google-Translate NMT backends
│   │       └── _subprocess_utils.py, _cli_base.py  # shared subprocess plumbing (run_subprocess/resolve_binary) + SubprocessCliTranslator base for CLI backends (codex_cli/gemini_cli/opencode)
│   ├── prompts/                   # genre-aware prompt hierarchy (ships in the EXE bundle)
│   │   ├── base.md                # genre-agnostic literary translator core
│   │   ├── genres/<key>.md        # 10 overlays: xianxia, wuxia, modern-romance, isekai, slice-of-life, mystery, litrpg, sci-fi, fantasy, yuri-bl (+ generic as legacy fallback)
│   │   └── examples/<key>.md      # per-genre worked examples
│   ├── scripts/                   # maintenance + learn-from-edits tooling
│   │   ├── load_glossary_md.py        # load data/glossary.md preset
│   │   ├── ingest_edited_chapter.py   # learn-from-edits: diff a hand-edited chapter, route deltas
│   │   ├── diff_against_edit.py        # ground-truth diff: a translation vs a hand edit
│   │   ├── ab_style_edits.py           # A/B the captured style-edit prompt arm
│   │   ├── retranslate_chapter.py      # re-run one chapter through the live pipeline
│   │   ├── consistency_eval.py         # read-only cross-chapter consistency baseline (TCR/reuse) + McNemar/bootstrap helpers
│   │   ├── quality_metrics.py          # tracked port of the ww_metrics rule-category scorers (pure, importable)
│   │   └── quality_report.py           # multi-chapter quality scorecard: per-category matrix + observation harvest + group-by prompt_config_snapshot + A/B diff
│   └── tests/                     # 80+ pytest modules
├── frontend/
│   ├── index.html, library.html, reader.html, glossary.html, glossary-global.html
│   ├── settings.html, queue.html, stats.html, quality.html, find-replace.html, novel-overview.html, onboarding.html
│   ├── css/
│   │   ├── base.css           # shared (imports fonts.css first)
│   │   ├── fonts.css          # @font-face for the self-hosted faces (audit 6.1)
│   │   └── home.css, library.css, reader.css, glossary.css, queue.css, settings.css,
│   │       stats.css, quality.css, find-replace.css, novel-overview.css, onboarding.css
│   ├── fonts/                 # self-hosted subsetted woff2: fraunces/, spectral/, noto-serif-sc/ (+ OFL.txt each; regenerate via scripts/fetch_fonts.py)
│   └── js/
│       ├── api.js, theme.js, utils.js, spine.js, queue-panel.js, boot.js, command-palette.js  # shared
│       ├── reader-core.js, reader-toc.js, reader-glossary.js, reader-consistency.js, reader-chapter.js, reader-edit.js, reader-quality.js  # reader.js split into ordered modules (plain <script> tags, source-order = concat-identical; core first owns shared state, quality last = cockpit badge + learn-from-edits panel)
│       ├── home.js, library.js, glossary.js, glossary-global.js, novel-overview.js
│       ├── settings.js, queue.js, stats.js, quality.js, find-replace.js, onboarding.js
├── scripts/                   # dev/CI scripts (not packaged)
│   ├── lint.ps1, smoke-exe.ps1, smoke_initiative7.py   # lint + EXE/smoke harnesses
│   ├── fetch_fonts.py                                   # download + subset the self-hosted fonts
│   └── dash_hook.py, check_em_dashes.py                 # em-dash PostToolUse guard + lint
├── tools/                     # dev tooling (not packaged)
│   └── sqlite_ro_mcp.py       # read-only SQLite MCP server (wired in .mcp.json)
└── data/                      # dev USER_DATA_ROOT (gitignored)
    ├── novels.db              # SQLite, WAL mode
    ├── glossary.md            # optional preset for load_glossary_md
    ├── llm_cache/             # content-addressed translation cache
    ├── covers/                # uploaded novel cover images
    ├── logs/                  # frozen-build startup.log mirror
    └── runtime/               # per-genre system-prompt cache files
```

## Environment setup

Dev server:
1. `cp .env.example .env` — optional. The app boots without `.env` and uses the Settings page for provider keys; `.env` is the bootstrap-seed path.
2. `pip install -e .` (or `uv sync`).
3. `uvicorn backend.main:app --reload --port 8000`.
4. Open <http://localhost:8000>.

EXE-style local app (also useful in dev to exercise the first-run wizard):
- `python -m backend.app_entry` — picks a free port starting at 8765, opens a pywebview window, and routes to `/onboarding` on first run (no `first_run_complete` key in `config_kv`).
- `LN_TRANSLATOR_NO_WINDOW=1 python -m backend.app_entry` — explicit headless: server only, no window, no browser tab. Used by smoke tests.

`main.py::_probe_backends` round-trips the default provider on startup so misconfiguration fails the server boot rather than the first request.

## Conventions

- **Async-first**: all I/O (DB, LLM calls, subprocess) uses `async`/`await`. `aiosqlite` for the DB.
- **Pydantic** for request/response models in `backend/models.py`. DB rows stay as `aiosqlite.Row` / `dict`.
- **Routes are thin**: call services, return Pydantic models. The queue worker code lives in `services/queue.py`; routes just set the queue flag and spawn a worker.
- **DB connection per request** via the `get_conn` FastAPI dependency. Queue workers use `open_conn()` directly — never share connections.
- **Schema** lives in `db.py::SCHEMA` and runs on startup; additive changes append to `_ADDITIVE_MIGRATIONS` (append-only — never reorder or remove an entry). One-time non-additive rebuilds (`_drop_dead_columns`, `_drop_glossary_category_check`) sit alongside as separate idempotent functions.
- **Frontend**: plain HTML / JS, no framework, no bundler. One JS file per page; `api.js`, `theme.js`, `utils.js`, `spine.js`, `queue-panel.js` shared. Each page loads `base.css` plus its own page sheet.
- **Log decisions & lessons**: when you make a non-obvious choice, measure an alternative out, or catch and correct a mistake, add a dated bullet to `docs/decisions.md` as part of "done". Check it (and `docs/gotchas.md`) before re-opening a settled question. Mechanical code traps go in `docs/gotchas.md`; the judgment-level *why* goes in `docs/decisions.md`.

## Pipeline

A chapter moves through one state machine: `status: pending → translating → done | error`. The user explicitly clicks Translate (or Retranslate); nothing auto-translates on import. The queue flag `translate_queued` is durable, so the user's picks survive a server restart (`queue.drain_on_startup` re-spawns workers for any row still flagged).

**Queue ordering (audit 3.2, 2026-06-11)**: each translate task, once it holds the serial lock, claims the best-ranked queued pending chapter (`queue_priority DESC, novel_id, chapter_num`) rather than the one it was spawned for; one task per queued chapter is unchanged. `POST /api/novels/{id}/chapters/{n}/translate-next` bumps `queue_priority` to MAX+1 ("Translate next" on the Queue page). Every `translate_queued = 0` write also zeroes `queue_priority`. Do not reuse the stale dead `queue_position` column that older user DBs still carry.

**Free-tier free-draft lane**: `chapters.free_draft_status` tracks an independent `none → pending → in_progress → done | error` state machine for the mechanical NMT draft (`services/free_draft_queue.py`, owns its own `FREE_DRAFT_LOCK`). Triggered on-demand — opening a chapter via `GET /api/chapters/{n}` queues a Google-Translate draft (via `deep-translator`, no API key needed; requires internet). The draft text lands in `chapters.free_draft_text` and, when `PROMPT_INCLUDE_FREE_DRAFT` is enabled, is read by the LLM translator as a `REFERENCE TRANSLATION` block in the prompt (PEMT — see `backend/services/translators/base.py::build_prompt`). That flag now defaults `false` (the mechanical reference pulled the prose toward literal phrasing), so the generation lane still runs on chapter open but the block is not injected by default. The LLM lane and the free-draft lane have independent locks so they run in parallel for different chapters; the LLM call reads `free_draft_text` from the row at translate time, no inter-task coordination required.

One process-global `asyncio.Lock` makes the translator strictly serial — every backend's `max_parallel` is effectively 1 because parallel Claude calls burn the subscription window and parallel Gemini calls burn tokens. The lock is non-negotiable; don't replace it with a Semaphore(N).

The worker (`_translate_chapter_in_db` in `services/queue.py`):

1. Claims the row (`status='translating'` only if currently `'pending'`).
2. Gathers prompt inputs: glossary, previous-chapter tail (within `PREVIOUS_CONTEXT_MAX_GAP` chapters back), captured style edits, per-novel style note, plus the resolved Provider and the novel's `genre` + `custom_style_brief` for the genre-aware system instruction. Author update-count / vote-begging markers (（第四更！）, 求月票) are stripped from the prompt's title line and the body's heading line (`parser.strip_title_update_marker`; prompt-time only, stored source stays verbatim).
3. Single LLM call via `translate_chapter` → backend's `_complete(prompt)` → `parse_delimited_response`. On parse failure: one retry, then plain-text fallback (sets `translation_degraded=1`, the only remaining degraded signal).
4. Pure text fixups (`services/text_fixups.py`): `strip_leading_title_line`, `enforce_locked_term_casing`, `enforce_lowercase_locked_terms`, `enforce_stem_branch_casing`, `strip_chapter_end_marker`, `enforce_em_dash`, `enforce_spaced_hyphen_dash`, `enforce_brackets`, `enforce_balanced_emphasis`, `enforce_sentence_initial_capitalization`, `enforce_mid_sentence_comma_break`. Each rule's change count is captured and persisted as `chapters.fixup_audit` JSON (`{"rules": {name: count}, "total": N}`) on the success commit, so this deterministic post-LLM override layer is queryable per chapter (a fixup can no longer silently rewrite correct output without a record). `quality_report.py` surfaces fixup churn + glossary force-case collisions from it; behavior is unchanged (visibility, not suppression).
5. **Observations only** (`services/text_observers.py`; no retry, no degraded mark): `body_correctness_observations` runs `missing_translator_terms`, `detect_mt_texture`, `detect_double_possessive`, `detect_intensifier_inflation_on_glossary_term`, `detect_mid_sentence_paragraph_break`, `detect_glossary_predicate_loss`. Hits are logged at INFO. The single-pass thesis is that noticing has to happen inside the translator's thinking phase; a retry would be the same shallow pass twice.
6. `normalize_title_en` rewrites the model's title into the canonical `Chapter N: Title` form using the authoritative `chapter_num`; when `title_zh` carried an author update marker, a trailing parenthetical in the model's title is dropped (zh-gated backstop). The dash fixups keep interruption / suspension dashes (dash before punctuation, a closing quote, or a paragraph end) and rewrite only clause-splicing dashes.
7. Atomic success commit: one UPDATE writes `title_en`, `translated_text`, `status='done'`, clears `translate_queued` / `force_retranslate`. If the novel has `refinement_provider_id` set, the same UPDATE flags `refinement_status='pending'` and the worker chains into `_refine_chapter_in_db` under the same lock acquisition.
8. Glossary merge runs after the success commit — failures stamp `glossary_merge_error` so the reader can surface a banner without losing the translation.

### Prompt-assembly A/B knobs

The runtime user prompt stacks several dynamic blocks on top of the static `base.md` + genre overlay + examples. Each block is gated at the queue's fetch site so a single env flag can suppress it for one A/B arm without DB mutation. Two kinds of control:

- **Experiment instruments** (global env flags, flip via env for A/B; the style + refiner flags default `true`/parity, `PROMPT_INCLUDE_FREE_DRAFT` now defaults `false`):
  - `PROMPT_INCLUDE_FREE_DRAFT` — REFERENCE TRANSLATION (Google-Translate mechanical NMT draft, PEMT layer). Defaults `false` (flipped 2026-05-29: the mechanical reference anchored prose toward literal phrasing); set `true` to restore the block.
  - `PROMPT_INCLUDE_STYLE_NOTE` — STYLE NOTE block (per-novel voice anchor).
  - `PROMPT_INCLUDE_STYLE_EDITS` — USER STYLE PREFERENCES block (captured paragraph edits).
  - `PROMPT_INCLUDE_REFINER` — global refiner kill-switch (overrides per-novel `refinement_provider_id` when false).
- **Product settings** (defaults chosen for product reasons, **not** part of the A/B grid):
  - `PREVIOUS_CONTEXT_ENABLED` — previous-chapter tail is doing real continuity work (pronoun reference, honorific consistency) that the glossary does not carry. Stays default `true`; do not put it in the A/B sequence.
  - `novels.refinement_provider_id` — per-novel column; long-term opt-in surface for the refiner.
  - `novels.genre`, `novels.custom_style_brief` — per-novel.

**Graduation rule (binding).** Defaults flip only when all four hold:
1. Single-variable A/B: exactly one flag flipped between two arms, every other config identical.
2. Run on a chapter where you have a ground-truth rewrite to diff against.
3. The off-arm output measurably closes the gap to the rewrite — "looks better" without a side-by-side diff does not count.
4. The commit that flips the default cites the chapter pair (config-A output, config-B output, your rewrite) in its body.

No "feels right on priors" default flips. (Exception on record: the `PROMPT_INCLUDE_FREE_DRAFT=false` default was an explicit user-directed conviction flip on 2026-05-29, not a graduated A/B result.) Recommended A/B sequence for the remaining flags: `PROMPT_INCLUDE_REFINER=false` if the test novel was using a refiner, then the style flags as completeness arms. Run one flag at a time; bundling makes results uninterpretable.

**Provenance.** Every successful translate commit stamps `chapters.prompt_config_snapshot` (JSON) with the full pipeline config: template version, translator/refiner provider+model, genre, which blocks actually shipped (block included only when both the flag was true AND the data was non-empty — `*_included` keys), and which flags were set (`flags.*`). Refinement-success extends the same blob with `refiner_*` keys. Query A/B runs with `json_extract(prompt_config_snapshot, '$.flags.PROMPT_INCLUDE_FREE_DRAFT')`. Writer: `services/queue.py::_build_prompt_config_snapshot` + `_extend_snapshot_with_refiner`.

**Measuring it (the graduation gate, mechanized).** `python -m backend.scripts.quality_report --novel N [--chapters LO-HI]` produces a multi-chapter scorecard that does the side-by-side work the rule above describes: a per-rule-category compliance matrix, the `chapter_observations` harvested per observer kind (the queue writes these every translate and nothing else aggregates them), novel-level consistency (TCR/reuse), and **the same metrics grouped by `prompt_config_snapshot`** so two config arms in the back catalog compare without re-translating. `--diff A.json B.json` gives per-category deltas with a bootstrap CI on each arm. Prefer this over a one-chapter eyeball; single-chapter A/Bs are noisy. Scorer logic is the importable `quality_metrics.py` (tracked port of the old gitignored `data/ww_metrics.py`).

## Translator rules

The literary system instruction is **genre-aware**. It is composed per call by `build_system_instruction(genre, custom_brief)` in `services/translators/base.py`, layering:

1. `backend/prompts/base.md` — genre-agnostic universal rules (fidelity, prose elevation, glossary discipline, grammar, punctuation, formatting, final-pass self-edit).
2. `backend/prompts/genres/<genre>.md` — genre-specific overlay (xianxia covers cultivator titles + Heavenly Stems + scene modes; wuxia covers jianghu honorifics + martial-arts vocabulary; etc.).
3. `backend/prompts/examples/<genre>.md` — worked examples scoped to that genre.
4. Optional `custom_style_brief` (per-novel user override) appended after the overlay.

The composed string is LRU-cached on `(resolved_genre, sha256(custom_brief)[:16])`. To add a new genre, drop overlay + examples files under `backend/prompts/` and add the registry entry to `backend/genres.py`. **Edit the `.md` files, not Python constants.**

The composed instruction text is part of `llm_cache.translation_key` via `BaseTranslator.system_instruction` (set per-call by `translate_chapter`), so prompt edits invalidate cached translations automatically. The class-level `PROMPT_TEMPLATE_VERSION` token folds into `cache_identity` separately for cache-shape changes that don't show up in the prompt body.

`NULL novels.genre` resolves through `DEFAULT_GENRE` (env, defaults to `xianxia`); unknown genres fall back to `generic` inside the loader so a bad DB value cannot crash the translator. `resolve_genre` in `backend/genres.py` owns the resolution, and the same resolved genre drives both the system instruction and its worked examples.

All backends use the **delimited envelope** (TITLE_EN / =====BODY===== / =====TERMS=====) — JSON mode was removed because it forced the chapter body into one escaped string, which compressed the prose. The body rides as raw text; only the small TERMS block is JSON.

DeepSeek is a single-pass translator (`deepseek.py`): one delimited-envelope call per chapter on the provider's model, with a parse-retry and a plain-text fallback. A second polish pass, when wanted, is the per-novel refiner (`refinement_provider_id`), not a DeepSeek-internal stage.

## Glossary rules

- Auto-extracted entries are inserted via `ON CONFLICT(novel_id, term_zh) DO UPDATE … WHERE locked = 0` — atomic upsert, no SELECT-then-INSERT race.
- Auto-merge is gated by `filter_glossary_candidates`: a term is admitted if it appears in a `【...】` system-interface span **or** recurs ≥ 2× in the chapter body. One-offs stay out; recurring narrative vocabulary lands in.
- User edits (PATCH with any field changed) implicitly lock the entry. Locked rows are never overwritten by auto-detection.
- Glossary is per-novel — two novels can render the same term differently.
- Casing policy: named cultivation concepts are Title-Cased (techniques, divine abilities, formations, ranks-as-titles, Heavenly Stems and Earthly Branches). Only `idiom` category is lowercase.

## Desktop EXE + welcome wizard

The frozen build is driven by `backend/app_entry.py` and packaged via `LN-Translator.spec`. Key points:

- **`backend/config.py`** detects `sys.frozen` and routes `USER_DATA_ROOT` to `%APPDATA%\LN-Translator\` on Windows (XDG / `~/Library/Application Support` on Linux / macOS). `PROJECT_ROOT` points at `sys._MEIPASS` so bundled read-only resources (prompts, frontend assets) load from the extracted bundle. The `LN_TRANSLATOR_DATA` env var overrides both defaults (used by tests and headless smokes).
- **Three UI modes** in `app_entry.py`: native pywebview window (default), `LN_TRANSLATOR_NO_WINDOW=1` headless (server only, no browser tab — for smokes), and a degraded `webbrowser.open()` fallback when pywebview / WebView2 import fails.
- **Shutdown funnel**: SIGINT, SIGTERM, Win32 console-close, and pywebview window-close all funnel into a single shared `_shutdown_event` that initiates clean uvicorn shutdown.
- **First-run wizard**: `config_kv.first_run_complete` (managed by `routes/config_kv.py`) gates first-run routing. Absent / `'0'` → `app_entry` lands the user on `/onboarding`, where `frontend/onboarding.js` walks them through creating a Provider and storing its API key in the OS keychain via `POST /api/providers` + `POST /api/providers/{id}/secret`. The wizard stamps `first_run_complete='1'` on completion; subsequent boots land on `/`.
- **Novel-creation defaults**: `config_kv.novel_defaults` is a JSON blob (`{translator_provider_id, refinement_provider_id, genre, source_language}`) set from the Settings page. `services/uploads.py::_resolve_novel_defaults` reads it inside the INSERT-novel path, so brand-new novels pick up the user's defaults instead of NULL. Existing novels are never backfilled — the per-novel dialog on the library card remains the explicit override surface. Fields outside the whitelist are ignored, so a typo in the blob can't poison an unrelated column. Reserve `config_kv` for app-level state only; per-novel state belongs on the novels table.
- **Secrets**: `keyring` writes to Windows Credential Manager / macOS Keychain / Secret Service. If keyring import fails (rare), `resolve_secret(provider)` falls back to reading the env var named in `provider.secret_ref` — that's the dev path.
- **Startup log**: `console=False` in the spec hides the console, so any startup-time diagnostic is mirrored to `USER_DATA_ROOT/logs/startup.log` (1 MB rotated). Build instructions live in `docs/exe-build.md`.

## Do not

- **No JS framework** (React/Vue/Svelte) and **no build step**.
- **No drop caps** — `::first-letter` styling is off-limits.
- **No `WEB_CONCURRENCY > 1` / `uvicorn --workers > 1`** — the translator lock is process-global. Multiple workers each get their own lock and burn the subscription window in parallel.
- **Don't reorder or remove entries in `_ADDITIVE_MIGRATIONS`** — append-only.
- **Don't invent, but recompose freely.** Facts, event order, named entities, and glossary terms (with their predicates) stay strictly literal, and no event, object, person, place, number, or image the source lacks may be added. The image inside a frozen lexicalized idiom is etymology, not content: it renders as its plain everyday sense unless the source plays on the image (user-approved 2026-06-09). Within that spine the translator recomposes by default (merge/split sentences, reorder within a paragraph, unspool a long sentence into plain linear ones) and may render an implication the source carries explicit or match its full intensity, but never manufacture force the source does not exert. The two-tier rule lives in `backend/prompts/base.md`; amplification is not addition. The binding prose bar (user directive 2026-06-09) is the professional wuxiaworld register: linear-transparent sentences, everyday lexicon, semicolons rare, contractions normal, full titles pronominalized between weight-bearing uses; prompt rules that conflict with that practice get replaced or removed, not softened.
- **Don't hardcode model versions** in routing logic. Provider rows carry their own `model_id`; treat provider type + model as data, not as enum values.
- **Don't commit `.env` or `data/novels.db`**.
- **Don't add auth** — this is a local single-user app (packaged EXE).

## Testing

- `pytest backend/tests`. Currently 1484 tests.
- `conftest.py` overrides `DB_PATH` to a temp file before any backend import.
- Translator stubs at the function level (see `test_bulk_upload.py::_fake_translate`). Stubs are fine for routing / state-machine tests; for translation behavior use a real backend against a fixture chapter.

## When extending

**Adding a translator backend**:
1. Subclass `BaseTranslator` in `services/translators/`. For OpenAI-compatible vendors, subclass `OpenAICompatibleTranslator` instead — most subclasses end up <20 lines (just set `name` + `DEFAULT_BASE_URL`). For CLI subprocess wrappers, see `_subprocess_utils.py` for the shared `run_subprocess` / `resolve_binary` / `build_argv` helpers.
2. Implement `_complete(prompt: str) -> str` and `_complete_plain(prompt: str) -> str`. Set `name`, `model_id`. (Inherited from `OpenAICompatibleTranslator` for OpenAI-compatible vendors.)
3. Add a `TypeEntry(...)` to `services/translator_catalog.py::_CATALOG` — this is the single source of truth for `provider_type` values, curated model lists, auth shape, and form defaults. `KNOWN_PROVIDER_TYPES` derives from it automatically.
4. Add a `{type: (module_path, class_name)}` row to `services/translators/factory.py::_DISPATCH`. The catalog-parity test (`test_translator_catalog.py`) enforces that catalog and dispatch keys agree.
5. Add a probe path to `main.py::_probe_one` so misconfiguration fails fast. For CLI types, use `probe_binary(...)`; for API-key types the generic API-key branch already covers the secret-resolution check.
6. (Optional) extend `backend/services/providers.py::ensure_default_provider` if the new type should seed automatically from a legacy `TRANSLATOR_BACKEND` env value.

**Adding a download format**:
- Extend `routes/novels.py::download_novel`. Stream the response; don't buffer the whole novel.
