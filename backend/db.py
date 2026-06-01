from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite

from backend.config import DB_PATH, ensure_data_dirs

logger = logging.getLogger(__name__)

LAST_ORPHAN_RECOVERY: dict[str, int] = {
    "translating_reset": 0,
    # Stale queue flag on a row whose status is already terminal. Worker
    # normally clears `translate_queued` in its finally / outer-except path,
    # but a worker killed by SIGKILL or a host reboot between the result
    # commit and the flag clear leaves the inconsistency.
    "stale_translate_cleared": 0,
    # Rows migrated from humanized_text → translated_text on first boot after
    # the single-pass restructure. One-shot — zero on every boot after that.
    "humanized_migrated": 0,
}


async def _apply_conn_pragmas(conn: aiosqlite.Connection) -> None:
    """Per-connection PRAGMAs shared by init_db and open_conn.

    - foreign_keys is per-connection and OFF by default in SQLite; ON is
      required for the ON DELETE CASCADE relationships in this schema.
    - synchronous=NORMAL is crash-safe under WAL and skips per-commit fsync.
    - busy_timeout lets writers wait briefly on the SQLite write lock."""
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA synchronous = NORMAL")
    await conn.execute("PRAGMA busy_timeout = 5000")


SCHEMA = """
-- User-configurable AI provider definitions. Each row describes a provider
-- the user can pick as a per-novel translator or refinement agent. API keys
-- are NOT stored here; secret_ref names the OS keychain entry (preferred)
-- or env var (fallback) holding the actual key. Exactly one row should have
-- is_default=1 — that's the fallback when a novel does not specify a provider.
CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    provider_type TEXT NOT NULL,
    base_url TEXT,
    model_id TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    secret_ref TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    -- Stamped by routes/providers.py /test on success. Lets the settings
    -- control-room card render "tested 2m ago" without reprobing on every
    -- page load. NULL until the user runs their first test.
    last_tested_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_providers_default
    ON providers(is_default) WHERE is_default = 1;

CREATE TABLE IF NOT EXISTS novels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('paste', 'txt', 'url', 'epub', 'docx', 'html')),
    source_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- Voice/register/imagery brief injected into every chapter translation
    -- prompt. NULL until the user hand-edits it via PATCH /api/novels/{id}
    -- (the reader's ✒ Style note dialog).
    style_note TEXT,
    -- Source language of the raw chapter text. Defaults to 'zh' for the
    -- current Chinese-only translator. Future parser/encoding expansion will
    -- branch on this; the column exists from day one to avoid a migration.
    source_language TEXT NOT NULL DEFAULT 'zh',
    -- Genre key from backend/genres.py. NULL falls back to DEFAULT_GENRE.
    -- Drives which prompt overlay the translator loads — build_system_instruction
    -- in services/translators/base.py reads this per call.
    genre TEXT,
    -- Free-text style brief appended to the genre overlay when set.
    custom_style_brief TEXT,
    -- Per-novel provider selection. NULL on translator_provider_id falls back
    -- to the default provider (providers.is_default=1). NULL on
    -- refinement_provider_id means refinement is OFF for this novel.
    translator_provider_id INTEGER REFERENCES providers(id) ON DELETE SET NULL,
    refinement_provider_id INTEGER REFERENCES providers(id) ON DELETE SET NULL,
    -- Initiative 2 metadata. All optional — older novels keep working with
    -- NULLs. cover_image_path is the relative path under USER_DATA_ROOT
    -- written by the cover upload route; the serve route resolves it.
    author TEXT,
    original_title TEXT,
    synopsis TEXT,
    status TEXT,
    cover_image_path TEXT,
    -- Which ingestion path supplied the cover. One of: 'epub' (extracted
    -- from an imported .epub), 'url' (scraped from og:image / twitter:image
    -- on the source page), 'upload' (manual POST /covers), NULL (no cover
    -- or cover predates this column). Drives the source pip on library
    -- cards so the user can see at a glance whether the cover came along
    -- with the novel or they uploaded it.
    cover_source TEXT,
    series_name TEXT,
    series_index INTEGER,
    -- 2026-05-25 F11: soft-delete. NULL means active; non-NULL is
    -- archive timestamp. Default novel list filters WHERE deleted_at IS
    -- NULL; the /api/novels?archived=1 endpoint shows only archived
    -- novels (the Library Archive tab).
    deleted_at TEXT,
    -- 2026-05-25 F26 (Bundle 2): JSON array of detect_* observer kinds
    -- to skip for this novel. Lets the user mute false-positive observer
    -- categories per-novel rather than dismissing them one-by-one.
    disabled_observers TEXT,
    -- 2026-05-26: lifecycle for resumable imports.
    -- NULL = atomic-create path or pre-feature row (treated as done).
    -- 'in_progress' | 'paused' | 'done' | 'cancelled' otherwise.
    -- See _ADDITIVE_MIGRATIONS comment for full semantics.
    import_status TEXT,
    -- 2026-05-28 durable reading position (see _ADDITIVE_MIGRATIONS). NULL
    -- until the reader records a position; reopening the app resumes here.
    last_read_chapter_num INTEGER,
    last_read_at TEXT
);

CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    chapter_num INTEGER NOT NULL,
    title_zh TEXT,
    title_en TEXT,
    original_text TEXT NOT NULL,
    translated_text TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'translating', 'done', 'error')),
    error_msg TEXT,
    translate_queued INTEGER NOT NULL DEFAULT 0,
    -- Set by /retranslate so the worker bypasses the LLM cache. The worker
    -- clears it when it claims the row, so it survives a restart but is
    -- consumed exactly once.
    force_retranslate INTEGER NOT NULL DEFAULT 0,
    -- Set when a translation came from a degraded path (the translator's
    -- plain-text fallback after JSON parsing failed twice, or a DeepSeek
    -- draft whose revision pass did not complete). With single-pass +
    -- guardrails-as-observers, this is the ONLY remaining degraded signal —
    -- guardrail hits no longer flip it.
    translation_degraded INTEGER NOT NULL DEFAULT 0,
    -- Set by the queue worker when the post-translation glossary merge fails
    -- after the translation itself has committed.
    glossary_merge_error TEXT,
    -- Optional refinement pass state machine. 'none' when the novel has no
    -- refinement_provider_id set; 'pending' after translator commit when a
    -- refiner is configured; 'in_progress' while the refiner runs; 'done' or
    -- 'error' on completion. The queue worker flags 'pending' atomically with
    -- the draft commit; services/refiner.py drives the rest.
    refinement_status TEXT NOT NULL DEFAULT 'none'
        CHECK (refinement_status IN ('none', 'pending', 'in_progress', 'done', 'error')),
    refined_text TEXT,
    refinement_error TEXT,
    refined_at TEXT,
    -- Per-chapter LLM cost tracking (Section 6.1). Nullable so older rows
    -- and providers that don't expose usage stay well-defined as "unknown"
    -- — the queue worker only writes these when the backend emits usage.
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_input_tokens INTEGER,
    -- Retained-but-unused: per-chapter cost tracking was removed when
    -- per-model pricing input was dropped (2026-05-26 catalog redesign).
    -- Column stays for migration safety (drops need a risky rebuild) but is
    -- never read or written now. Token columns above are still live.
    cost_usd REAL,
    -- Initiative 6: timestamp of the successful translation commit. Lets
    -- the stats dashboard plot throughput-per-day. NULL on chapters that
    -- predate the migration; queue worker stamps it on every fresh commit.
    translated_at TEXT,
    -- 2026-05-26 resumable imports: per-chapter source URL captured at
    -- planning time (recipe scrapes only — bulk / EPUB leave this NULL).
    -- The runner's resume path uses this to re-fetch any chapter whose
    -- original_text is still NULL after a crash.
    import_source_url TEXT,
    -- Set when original_text was written by the runner's fill phase.
    -- NULL on a skeleton row = "still pending fetch / decode."
    import_fetched_at TEXT,
    -- 2026-05-26 free-tier mechanical NMT backend (Google Translate as of 2026-05-28).
    -- `free_draft_text` holds the mechanical draft; the LLM
    -- translation reads it as a REFERENCE TRANSLATION fidelity layer
    -- (see services/translators/base.py::build_prompt). free_draft_status
    -- gates the on-demand free-draft worker (services/free_draft_queue.py).
    free_draft_text TEXT,
    free_draft_status TEXT NOT NULL DEFAULT 'none'
        CHECK (free_draft_status IN ('none', 'pending', 'in_progress', 'done', 'error')),
    free_draft_error TEXT,
    free_draft_completed_at TEXT,
    -- Per-chapter translator provenance — mirrors refined_by_provider_id.
    -- Lets the reader pick banner copy by joining to providers.provider_type
    -- without re-deriving from novels.translator_provider_id (which can
    -- change after the chapter was translated).
    translated_by_provider_id INTEGER REFERENCES providers(id) ON DELETE SET NULL,
    -- 2026-05-28 prompt-assembly provenance. JSON blob written on every
    -- successful translate commit and extended on every refine commit:
    -- which prompt blocks shipped, which env flags were set, which
    -- translator + refiner produced the visible English. Lets A/B runs
    -- across PROMPT_INCLUDE_* flags stay recoverable per-output. See
    -- services/queue.py::_build_prompt_config_snapshot for the writer.
    prompt_config_snapshot TEXT NOT NULL DEFAULT '{}',
    UNIQUE (novel_id, chapter_num)
);

CREATE INDEX IF NOT EXISTS idx_chapters_novel ON chapters(novel_id, chapter_num);
CREATE INDEX IF NOT EXISTS idx_chapters_translate_queued
    ON chapters(novel_id, chapter_num) WHERE translate_queued = 1;
-- Note: idx_chapters_import_pending lives in _ADDITIVE_MIGRATIONS because
-- its WHERE clause references columns that only exist after the additive
-- ALTERs run (existing DBs created chapters without them). Putting it in
-- the SCHEMA executescript would error on first boot of an upgrade.

CREATE TABLE IF NOT EXISTS glossary_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    term_zh TEXT NOT NULL,
    term_en TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    notes TEXT,
    -- One-line gloss on how to USE this term (not just what it translates to).
    -- Injected inline into the prompt's glossary block when set. NULL on
    -- auto-extracted entries; the user fills it in for locked terms they
    -- want to anchor.
    usage_note TEXT,
    auto_detected INTEGER NOT NULL DEFAULT 1,
    locked INTEGER NOT NULL DEFAULT 0,
    -- Design v2 Phase D: when this entry's English text was last touched.
    -- The reader / glossary UI compares this against the corresponding
    -- chapter's translated_at to mark chapters "stale" when the term they
    -- used has since been edited. The matching ADDITIVE_MIGRATIONS entry
    -- covers existing databases; this CREATE handles fresh installs.
    -- services/glossary.py::update_entry MUST stamp datetime('now') on
    -- every PATCH — pinned by test_update_entry_stamps_updated_at.
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (novel_id, term_zh)
);

CREATE INDEX IF NOT EXISTS idx_glossary_novel ON glossary_entries(novel_id);

CREATE TABLE IF NOT EXISTS style_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
    before_text TEXT NOT NULL,
    after_text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_style_edits_novel ON style_edits(novel_id, id);

-- Per-novel paired source↔English examples. The loader and the
-- prompt-time sampler were removed in the post-pivot cleanup; this
-- table stays as a no-op storage shape for backward compat with any
-- older DB. Safe to drop entirely if no row count is ever non-zero.
CREATE TABLE IF NOT EXISTS canonical_paragraphs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    source_zh TEXT NOT NULL,
    target_en TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_canonical_paragraphs_novel
    ON canonical_paragraphs(novel_id, id);

-- Initiative 1 QA dashboard: persisted observations from the deterministic
-- detect_* observers plus implicit translation_degraded / glossary_merge_error
-- kinds. Owned by chapter and replaced as a set on every retranslation
-- (DELETE+INSERT inside the success-commit transaction).
CREATE TABLE IF NOT EXISTS chapter_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warn',
    paragraph_index INTEGER,
    excerpt TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    dismissed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_chapter_observations_chapter
    ON chapter_observations(chapter_id);

-- 2026-05-25 Bundle 1.B (F36): find/replace commit snapshots. Every
-- commit_preview writes the pre-substitution bodies into payload_json;
-- the restore endpoint replays them back onto the affected chapters.
-- One row per touched novel per commit (cross-novel commits produce N
-- rows). 30-day retention; 5MB cap per payload.
CREATE TABLE IF NOT EXISTS find_replace_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    commit_token TEXT NOT NULL,
    find_pattern TEXT NOT NULL,
    replace_pattern TEXT NOT NULL,
    target TEXT NOT NULL,
    scope TEXT NOT NULL,
    chapters_changed INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    committed_at TEXT NOT NULL DEFAULT (datetime('now')),
    restored_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_fr_snapshots_novel
    ON find_replace_snapshots(novel_id, committed_at DESC);

-- 2026-05-25 Bundle 2 (F22): translation attempts log. Records each
-- LLM invocation for diagnostic purposes — parse failures, prompt
-- snapshots for the "show prompt" panel, retry counts.
CREATE TABLE IF NOT EXISTS chapter_translation_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    provider_id INTEGER REFERENCES providers(id) ON DELETE SET NULL,
    model_id TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status TEXT NOT NULL,
    parse_error TEXT,
    prompt_snapshot TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chapter_attempts_chapter
    ON chapter_translation_attempts(chapter_id, started_at DESC);

-- 2026-05-26: background scrape job tracking. POST /scrape returns a
-- job_id immediately; the recipe runs in asyncio.create_task and writes
-- progress here. The frontend polls for status + chapter counters.
CREATE TABLE IF NOT EXISTS scrape_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    step TEXT,
    current INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    novel_id INTEGER REFERENCES novels(id) ON DELETE SET NULL,
    scraped_title TEXT,
    error_message TEXT,
    error_kind TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_scrape_jobs_status
    ON scrape_jobs(status, started_at DESC);

-- Initiative 2 reader bookmarks. Owned by chapter (FK CASCADE) so a chapter
-- delete cleans up; owned by novel too so a novel delete cascades through.
CREATE TABLE IF NOT EXISTS bookmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    paragraph_index INTEGER,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bookmarks_novel ON bookmarks(novel_id, id);
CREATE INDEX IF NOT EXISTS idx_bookmarks_chapter ON bookmarks(chapter_id);

-- Initiative 3 global glossary: cross-novel terms that apply to every
-- translation. Always treated as locked (authoritative); per-novel locked
-- + per-novel auto-detected entries take precedence when their term_zh
-- collides with a global entry. No novel_id — uniqueness is on term_zh
-- alone. No `locked` column — globals are inherently locked.
CREATE TABLE IF NOT EXISTS global_glossary_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_zh TEXT NOT NULL UNIQUE,
    term_en TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    notes TEXT,
    usage_note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_global_glossary_term ON global_glossary_entries(term_zh);

-- Design v2 Phase G — simple key/value app-level config store.
-- Reserved for app-wide state (first_run_complete, etc.). Per-novel
-- state belongs on the novels table.
CREATE TABLE IF NOT EXISTS config_kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Initiative 5 translation memory. Paragraph-aligned source ↔ target
-- segments populated by the queue worker on every successful chapter
-- commit. Concordance search and inconsistency detection read from here.
-- source_hash indexes the SHA256 prefix of source_text for fast lookup
-- of "have I translated this exact phrase before".
CREATE TABLE IF NOT EXISTS tm_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    paragraph_index INTEGER NOT NULL,
    source_text TEXT NOT NULL,
    target_text TEXT NOT NULL,
    -- 16-hex-char prefix of SHA256(source_text). Enough collision
    -- resistance for in-novel concordance + cheap to index.
    source_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tm_novel_hash
    ON tm_segments(novel_id, source_hash);
CREATE INDEX IF NOT EXISTS idx_tm_chapter
    ON tm_segments(chapter_id);
"""


# Idempotent additive migrations. Each ALTER raises OperationalError if the
# column already exists; we catch and move on so existing DBs upgrade in
# place while fresh DBs get the same columns via SCHEMA above. Keep this list
# APPEND-ONLY — never reorder, never drop, never repurpose an entry.
#
# Note: the legacy humanizer / review columns are still listed here so an
# upgrade from a pre-restructure DB picks them up (which lets
# _migrate_humanized_into_translated see the column and run, and lets
# _drop_dead_columns find the sentinel). _drop_dead_columns rebuilds the
# table without them afterwards. On a fresh DB built from the new SCHEMA the
# legacy ALTERs re-add the columns to the empty table; the subsequent
# rebuild drops them again. Wasteful at first boot, but the alternative is
# breaking the append-only rule.
_ADDITIVE_MIGRATIONS = (
    "ALTER TABLE chapters ADD COLUMN humanized_text TEXT",
    "ALTER TABLE chapters ADD COLUMN humanizer_report TEXT",
    "ALTER TABLE chapters ADD COLUMN humanizer_status TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE chapters ADD COLUMN humanizer_error TEXT",
    "ALTER TABLE chapters ADD COLUMN translate_queued INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chapters ADD COLUMN humanize_queued INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chapters ADD COLUMN glossary_merge_error TEXT",
    "ALTER TABLE novels ADD COLUMN humanizer_tone TEXT",
    "ALTER TABLE novels ADD COLUMN humanizer_honorific TEXT",
    "ALTER TABLE novels ADD COLUMN humanizer_intensity TEXT",
    "ALTER TABLE chapters ADD COLUMN force_retranslate INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chapters ADD COLUMN translation_degraded INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chapters ADD COLUMN review_status TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE chapters ADD COLUMN review_error TEXT",
    "ALTER TABLE chapters ADD COLUMN pre_review_text TEXT",
    # 2026-05-22 single-pass restructure additions:
    "ALTER TABLE novels ADD COLUMN style_note TEXT",
    "ALTER TABLE glossary_entries ADD COLUMN usage_note TEXT",
    "ALTER TABLE style_edits ADD COLUMN variant TEXT",
    """CREATE TABLE IF NOT EXISTS canonical_paragraphs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
        source_zh TEXT NOT NULL,
        target_en TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_canonical_paragraphs_novel ON canonical_paragraphs(novel_id, id)",
    # 2026-05-23 per-novel provider + genre + refinement state machine:
    "ALTER TABLE novels ADD COLUMN source_language TEXT NOT NULL DEFAULT 'zh'",
    "ALTER TABLE novels ADD COLUMN genre TEXT",
    "ALTER TABLE novels ADD COLUMN custom_style_brief TEXT",
    "ALTER TABLE novels ADD COLUMN translator_provider_id INTEGER REFERENCES providers(id) ON DELETE SET NULL",
    "ALTER TABLE novels ADD COLUMN refinement_provider_id INTEGER REFERENCES providers(id) ON DELETE SET NULL",
    "ALTER TABLE chapters ADD COLUMN refinement_status TEXT NOT NULL DEFAULT 'none'",
    "ALTER TABLE chapters ADD COLUMN refined_text TEXT",
    "ALTER TABLE chapters ADD COLUMN refinement_error TEXT",
    "ALTER TABLE chapters ADD COLUMN refined_at TEXT",
    # 2026-05-23 cost tracking (Section 6.1 + 6.8). USD-per-million-tokens
    # pricing on providers powers per-chapter cost computation on
    # successful translations. All cost columns are nullable so older rows
    # and providers without pricing data are well-defined as "unknown".
    "ALTER TABLE providers ADD COLUMN pricing_input_per_mtok REAL",
    "ALTER TABLE providers ADD COLUMN pricing_output_per_mtok REAL",
    "ALTER TABLE chapters ADD COLUMN input_tokens INTEGER",
    "ALTER TABLE chapters ADD COLUMN output_tokens INTEGER",
    "ALTER TABLE chapters ADD COLUMN cached_input_tokens INTEGER",
    "ALTER TABLE chapters ADD COLUMN cost_usd REAL",
    # 2026-05-24 QA dashboard (Initiative 1): persisted observations from the
    # deterministic detect_* observers + the two implicit issue kinds
    # (translation_degraded, glossary_merge_error). Rows are owned by their
    # chapter and replaced as a set on every retranslation — the queue worker
    # DELETE-then-INSERT is in the same transaction as the chapter UPDATE,
    # so there's no race window for a mixed-generation view.
    """CREATE TABLE IF NOT EXISTS chapter_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'warn',
        paragraph_index INTEGER,
        excerpt TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        dismissed_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chapter_observations_chapter "
    "ON chapter_observations(chapter_id)",
    # 2026-05-24 Initiative 2 (reader polish + library metadata): richer
    # per-novel metadata so the library covers view stops being a wall of
    # CJK seal placeholders and series groupings become a thing the UI can
    # render. All fields are nullable / default-NULL — existing rows stay
    # well-defined.
    "ALTER TABLE novels ADD COLUMN author TEXT",
    "ALTER TABLE novels ADD COLUMN original_title TEXT",
    "ALTER TABLE novels ADD COLUMN synopsis TEXT",
    # Free-form status: 'ongoing' | 'complete' | 'hiatus' | NULL. No CHECK
    # constraint — user can type custom labels via the info dialog if they
    # want, and a future status taxonomy stays migration-free.
    "ALTER TABLE novels ADD COLUMN status TEXT",
    # Absolute or USER_DATA_ROOT-relative path to an uploaded cover image.
    # Stored as a path rather than a blob so the SQLite file stays small and
    # backups stay diffable; serve route streams from disk.
    "ALTER TABLE novels ADD COLUMN cover_image_path TEXT",
    # Series grouping. NULL series_name = standalone. series_index orders
    # within a series; a sentinel large value keeps unsequenced rows after
    # the sequenced ones when sorted ASC.
    "ALTER TABLE novels ADD COLUMN series_name TEXT",
    "ALTER TABLE novels ADD COLUMN series_index INTEGER",
    # Per-novel reader bookmarks. Owned by the chapter (FK CASCADE) so a
    # chapter delete cleans up; owned by the novel too so a novel delete
    # cascades through chapters. paragraph_index is the 0-based offset
    # into the displayed body (translated_text or refined_text, whichever
    # the reader was showing) — the click handler re-derives the scroll
    # target from the rendered DOM.
    """CREATE TABLE IF NOT EXISTS bookmarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
        chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
        paragraph_index INTEGER,
        note TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_bookmarks_novel "
    "ON bookmarks(novel_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_bookmarks_chapter "
    "ON bookmarks(chapter_id)",
    # 2026-05-24 Initiative 3 global glossary.
    """CREATE TABLE IF NOT EXISTS global_glossary_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        term_zh TEXT NOT NULL UNIQUE,
        term_en TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'other',
        notes TEXT,
        usage_note TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_global_glossary_term "
    "ON global_glossary_entries(term_zh)",
    # 2026-05-24 Initiative 5 translation memory.
    """CREATE TABLE IF NOT EXISTS tm_segments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
        chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
        paragraph_index INTEGER NOT NULL,
        source_text TEXT NOT NULL,
        target_text TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tm_novel_hash "
    "ON tm_segments(novel_id, source_hash)",
    "CREATE INDEX IF NOT EXISTS idx_tm_chapter "
    "ON tm_segments(chapter_id)",
    # 2026-05-24 Initiative 6: chapter completion timestamp for the
    # stats dashboard's throughput-per-day chart.
    "ALTER TABLE chapters ADD COLUMN translated_at TEXT",
    # 2026-05-24 Design v2 Phase B: track which provider refined each
    # chapter so the reader's bilingual pane label can show a "refined by X"
    # attribution chip. Nullable — old rows + chapters that never refined
    # leave it NULL; the UI degrades to a generic "refined" chip if
    # refinement_status='done' but the provider id isn't recorded.
    "ALTER TABLE chapters ADD COLUMN refined_by_provider_id INTEGER REFERENCES providers(id) ON DELETE SET NULL",
    # 2026-05-24 Design v2 Phase D: stale-glossary watermark. The per-novel
    # glossary_entries table didn't have updated_at (global_glossary_entries
    # has one since launch). Default datetime('now') on existing rows is
    # fine — they're stale-relative to nothing pre-migration. The update
    # service in services/glossary.py::update_entry is responsible for
    # restamping this on every PATCH; without that the column would only
    # ever hold the insert time and the "stale" badge would never light up.
    # Test pinned in test_glossary.py::test_update_entry_stamps_updated_at.
    "ALTER TABLE glossary_entries ADD COLUMN updated_at TEXT NOT NULL DEFAULT (datetime('now'))",
    # 2026-05-24 Design v2 Phase G: simple key/value config store. Created
    # primarily for first_run_complete so the EXE entry can route to the
    # onboarding flow on actual first run (legacy path: check for any
    # provider rows; that misbehaves when a user deletes their only
    # provider and we'd then re-route them to onboarding even though they
    # already know the app). Reserve for app-level state only — per-novel
    # state belongs on the novels table.
    "CREATE TABLE IF NOT EXISTS config_kv (key TEXT PRIMARY KEY, value TEXT)",
    # 2026-05-24 wireframes redesign — track where each cover image came from
    # so the library card can render a small "scraped" / "epub" pip without
    # round-tripping to the source page. Nullable: pre-migration rows AND
    # novels with no cover read as NULL → no pip rendered.
    "ALTER TABLE novels ADD COLUMN cover_source TEXT",
    # 2026-05-25 multi-tag genres. novels.genre stays as the PRIMARY genre
    # (drives the prompt overlay via resolve_genre); this table holds any
    # SECONDARY tags the user has attached on the novel overview page.
    # Swap-primary is a transactional operation in services/genres_novel.py.
    # UNIQUE(novel_id, genre_key) prevents the same tag being added twice;
    # CASCADE on novel delete keeps the table clean.
    """CREATE TABLE IF NOT EXISTS novel_genres (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
        genre_key TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(novel_id, genre_key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_novel_genres_novel ON novel_genres(novel_id)",
    # 2026-05-25 Bundle 1.B — soft-delete novels. Default novel list filters
    # WHERE deleted_at IS NULL; restored from Archive via UPDATE clear; purge
    # is the existing DELETE which fires CASCADE on chapters / glossary / etc.
    "ALTER TABLE novels ADD COLUMN deleted_at TEXT",
    # 2026-05-25 Bundle 2 (observability) — per-novel observer mute. JSON
    # array of detect_* kind strings (e.g. '["mt_texture","double_possessive"]')
    # that the queue worker skips when running body_correctness_observations.
    "ALTER TABLE novels ADD COLUMN disabled_observers TEXT",
    # 2026-05-25 Bundle 3 (queue UX) — drag-to-reorder priority. NULL means
    # use FIFO by id (current behavior). Non-NULL takes priority lower-first.
    # Queue worker ORDER BY queue_position IS NULL, queue_position, id.
    "ALTER TABLE chapters ADD COLUMN queue_position INTEGER",
    # 2026-05-25 Bundle 1.B (F40) — bookmark drift detection. sha256 of the
    # paragraph text at bookmark create time. drift-status endpoint compares
    # current paragraph hash to this; mismatch surfaces in the bookmarks
    # panel as "⚠ may have drifted; click to verify."
    "ALTER TABLE bookmarks ADD COLUMN anchor_hash TEXT",
    # 2026-05-25 Bundle 1.B (F36) — snapshot-before-commit for find/replace
    # restore. Every commit_preview writes the pre-substitution body of each
    # changed chapter into payload_json. Restore endpoint replays the
    # snapshot back onto the chapters. 30-day retention; 5MB cap per row
    # (callers fall back to "snapshot unavailable" on commit when over cap).
    """CREATE TABLE IF NOT EXISTS find_replace_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
        commit_token TEXT NOT NULL,
        find_pattern TEXT NOT NULL,
        replace_pattern TEXT NOT NULL,
        target TEXT NOT NULL,
        scope TEXT NOT NULL,
        chapters_changed INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL,
        committed_at TEXT NOT NULL DEFAULT (datetime('now')),
        restored_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fr_snapshots_novel "
    "ON find_replace_snapshots(novel_id, committed_at DESC)",
    # 2026-05-25 Bundle 2 (F22) — translation attempts log. One row per
    # _translate_chapter_in_db invocation: started_at, finished_at, status
    # ('ok'/'parse_failed'/'fallback_plaintext'/'error'), parse_error
    # (which delimiter / what the LLM produced instead), prompt_snapshot
    # (full resolved prompt) for the "show prompt" diagnostic. Queue worker
    # writes inside the chapter commit transaction.
    """CREATE TABLE IF NOT EXISTS chapter_translation_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
        provider_id INTEGER REFERENCES providers(id) ON DELETE SET NULL,
        model_id TEXT,
        started_at TEXT NOT NULL DEFAULT (datetime('now')),
        finished_at TEXT,
        status TEXT NOT NULL,
        parse_error TEXT,
        prompt_snapshot TEXT,
        retry_count INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chapter_attempts_chapter "
    "ON chapter_translation_attempts(chapter_id, started_at DESC)",
    # 2026-05-26 — background scrape jobs. POST /api/translate/scrape used
    # to block the request for the entire crawl (25+ min for 1500-chapter
    # 69shuba novels); navigating away cancelled the asyncio task on some
    # paths and the user had no visibility into progress. The route now
    # creates a row here, spawns asyncio.create_task(run_scrape_job(...)),
    # and returns the job_id immediately. The task survives the request;
    # the frontend polls /api/translate/scrape/jobs/{id} for status + the
    # current/total chapter counters that the recipes write via the
    # progress callback.
    """CREATE TABLE IF NOT EXISTS scrape_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        step TEXT,
        current INTEGER NOT NULL DEFAULT 0,
        total INTEGER NOT NULL DEFAULT 0,
        novel_id INTEGER REFERENCES novels(id) ON DELETE SET NULL,
        scraped_title TEXT,
        error_message TEXT,
        error_kind TEXT,
        started_at TEXT NOT NULL DEFAULT (datetime('now')),
        finished_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_scrape_jobs_status "
    "ON scrape_jobs(status, started_at DESC)",
    # 2026-05-26 — settings control-room: providers.last_tested_at lets the
    # provider card show "tested 2m ago" without re-running the probe on
    # every page load. routes/providers.py /test stamps this on success.
    "ALTER TABLE providers ADD COLUMN last_tested_at TEXT",
    # 2026-05-26 — interruption-proof imports. The novel row + N skeleton
    # chapter rows are created up-front; the slow per-chapter work then
    # writes back into those rows incrementally and commits each one.
    # `novels.import_status` tracks the lifecycle:
    #   NULL          → legacy / atomic-create path (paste, small upload,
    #                   generic scrape). Treated as 'done' implicitly.
    #   'in_progress' → runner actively filling chapter rows.
    #   'paused'      → user cancelled OR boot recovery couldn't self-source
    #                   (bulk / EPUB whose upload bytes are gone). Partial
    #                   novel sits in the library; user can Resume or delete.
    #   'done'        → all skeleton chapters filled.
    #   'cancelled'   → reserved; not used by the current cancel path
    #                   (which lands in 'paused' so the user keeps work).
    "ALTER TABLE novels ADD COLUMN import_status TEXT",
    # 2026-05-26 — per-chapter source URL captured at planning time so the
    # runner can re-fetch any pending chapter after a restart without
    # re-running the catalog crawl. NULL for bulk / EPUB imports (the
    # source data lived in the request body and is gone post-crash).
    "ALTER TABLE chapters ADD COLUMN import_source_url TEXT",
    # 2026-05-26 — set when the runner commits `original_text` into a
    # skeleton row. NULL means "skeleton, awaiting fetch / decode" — the
    # drain query reads chapters where original_text IS NULL to pick up
    # the next batch on resume.
    "ALTER TABLE chapters ADD COLUMN import_fetched_at TEXT",
    # 2026-05-26 — partial index for the drain / resume query. The two
    # conditions narrow this to *recipe-scrape skeleton rows still
    # awaiting a fetch* — the only path that needs index-speed lookup.
    # Bulk / EPUB imports stream their decoded chapters as full INSERTs
    # (no skeleton) so they don't appear here. WHERE-indexed so the
    # index drops to empty once an import completes.
    "CREATE INDEX IF NOT EXISTS idx_chapters_import_pending "
    "ON chapters(novel_id) "
    "WHERE import_fetched_at IS NULL AND import_source_url IS NOT NULL",
    # 2026-05-26 — free-tier mechanical NMT backend + LLM post-editing of NMT.
    # `free_draft_text` holds the mechanical draft (Google Translate as of
    # 2026-05-28; OPUS-MT before that), separate from `translated_text` (the
    # LLM PEMT-merged final). The reader can show `free_draft_text` while the
    # LLM call is in flight, the LLM call uses it as a fidelity-reference
    # input (REFERENCE TRANSLATION section in the prompt — see
    # services/translators/base.py::build_prompt), and the draft survives
    # across LLM retranslations. For free-tier-only novels (no LLM provider
    # configured) the queue worker writes the MT output to BOTH `free_draft_text`
    # and `translated_text` so the reader path is uniform.
    "ALTER TABLE chapters ADD COLUMN free_draft_text TEXT",
    "ALTER TABLE chapters ADD COLUMN free_draft_status TEXT NOT NULL DEFAULT 'none'",
    "ALTER TABLE chapters ADD COLUMN free_draft_error TEXT",
    "ALTER TABLE chapters ADD COLUMN free_draft_completed_at TEXT",
    # Per-chapter translator provenance — mirrors the existing
    # `refined_by_provider_id` column. Lets the reader join to providers and
    # pick banner copy by provider_type (free-tier rough draft vs LLM polished
    # vs LLM PEMT-merged) without re-deriving from novels.translator_provider_id
    # (which can change after the chapter was translated).
    "ALTER TABLE chapters ADD COLUMN translated_by_provider_id INTEGER REFERENCES providers(id) ON DELETE SET NULL",
    # 2026-05-28 — prompt-assembly provenance. JSON blob stamped on every
    # successful translate commit (and extended on every successful refine
    # commit) recording the full pipeline config that produced this row:
    # PROMPT_TEMPLATE_VERSION, translator + refiner provider/model, genre,
    # which dynamic prompt blocks shipped (free_draft / previous_context /
    # style_note / style_edits), and the env-flag state at translation time.
    # Lets A/B runs across PROMPT_INCLUDE_* flags stay recoverable per-output
    # via SQL (`json_extract(prompt_config_snapshot, '$.flags...')`). See
    # services/queue.py::_build_prompt_config_snapshot for the writer.
    "ALTER TABLE chapters ADD COLUMN prompt_config_snapshot TEXT NOT NULL DEFAULT '{}'",
    # 2026-05-28 — durable per-novel reading position. Promotes the reader's
    # resume breadcrumb off fragile localStorage (WebView2 discards it whenever
    # app_entry's private_mode fallback fires, plus normal cache eviction) onto
    # the SQLite source of truth, so reopening the app lands on the chapter the
    # user left off rather than chapter 1. `last_read_at` lets the library
    # "Continue reading" strip reproduce its most-recently-read sort server-side
    # (it previously sorted on the localStorage `ts`). Both nullable: pre-feature
    # rows and never-opened novels read as NULL → reader falls back to the first
    # chapter. Written by PUT /api/novels/{id}/reading-position.
    "ALTER TABLE novels ADD COLUMN last_read_chapter_num INTEGER",
    "ALTER TABLE novels ADD COLUMN last_read_at TEXT",
)

# FTS5 virtual table mirroring searchable English text. Phase 4: the indexed
# body is COALESCE(refined_text, translated_text) so search hits whatever
# the reader actually sees. The trigger also fires on refined_text changes
# so a refinement landing mid-chapter updates the index immediately.
#
# `translated_text` is the FTS column name (kept as-is so existing search
# queries don't break); the VALUE inserted is the COALESCE expression.
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chapter_fts USING fts5(
    title_en, translated_text,
    content='chapters',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS chapter_fts_ai AFTER INSERT ON chapters BEGIN
    INSERT INTO chapter_fts(rowid, title_en, translated_text)
    VALUES (
        new.id,
        COALESCE(new.title_en, ''),
        COALESCE(new.refined_text, new.translated_text, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS chapter_fts_ad AFTER DELETE ON chapters BEGIN
    INSERT INTO chapter_fts(chapter_fts, rowid, title_en, translated_text)
    VALUES (
        'delete',
        old.id,
        COALESCE(old.title_en, ''),
        COALESCE(old.refined_text, old.translated_text, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS chapter_fts_au
    AFTER UPDATE OF title_en, translated_text, refined_text ON chapters
BEGIN
    INSERT INTO chapter_fts(chapter_fts, rowid, title_en, translated_text)
    VALUES (
        'delete',
        old.id,
        COALESCE(old.title_en, ''),
        COALESCE(old.refined_text, old.translated_text, '')
    );
    INSERT INTO chapter_fts(rowid, title_en, translated_text)
    VALUES (
        new.id,
        COALESCE(new.title_en, ''),
        COALESCE(new.refined_text, new.translated_text, '')
    );
END;
"""


async def _chapter_columns(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute("PRAGMA table_info(chapters)")
    rows = await cur.fetchall()
    return {r[1] for r in rows}


async def _novel_columns(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute("PRAGMA table_info(novels)")
    rows = await cur.fetchall()
    return {r[1] for r in rows}


async def _style_edits_columns(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute("PRAGMA table_info(style_edits)")
    rows = await cur.fetchall()
    return {r[1] for r in rows}


async def _migrate_humanized_into_translated(conn: aiosqlite.Connection) -> int:
    """One-shot: copy humanized_text → translated_text where present.

    The user chose (single-pass restructure plan, May 2026) to treat the
    humanizer's output as the canonical text on chapters that ran through it,
    so we promote that text before _drop_dead_columns removes the column.
    Idempotent: returns 0 if the column doesn't exist or no rows match."""
    cols = await _chapter_columns(conn)
    if "humanized_text" not in cols or "translated_text" not in cols:
        return 0
    cur = await conn.execute(
        "UPDATE chapters SET translated_text = humanized_text "
        "WHERE humanized_text IS NOT NULL AND length(humanized_text) > 0"
    )
    await conn.commit()
    return cur.rowcount or 0


async def _drop_dead_columns(conn: aiosqlite.Connection) -> None:
    """One-shot rebuild on pre-restructure DBs: drops humanizer + review
    columns from chapters, humanizer_* from novels, and the variant column
    from style_edits. Idempotent: sentinel is `humanized_text` in chapters.

    Phase 4 hardening: the append-only `_ADDITIVE_MIGRATIONS` list still
    re-adds `humanized_text` on every boot (the append-only rule forbids
    removing it). Without the early-out below, this function would rebuild
    chapters on every init_db call. That's wasteful on fresh DBs AND it
    re-drops chapter_fts, which under the new refined_text-aware FTS
    triggers leaves the index in a state that corrupts the next UPDATE
    ('database disk image is malformed'). When the legacy column exists
    but holds no data, just drop the column and skip the rebuild.
    """
    cols = await _chapter_columns(conn)
    if "humanized_text" not in cols:
        return
    cur = await conn.execute(
        "SELECT 1 FROM chapters WHERE humanized_text IS NOT NULL LIMIT 1"
    )
    if not await cur.fetchone():
        # No legacy rows ever wrote to humanized_text. Drop the column in
        # place — SQLite 3.35+ supports DROP COLUMN — and skip the full
        # table rebuild. This is the path every fresh DB hits.
        try:
            await conn.execute("ALTER TABLE chapters DROP COLUMN humanized_text")
            await conn.commit()
            return
        except aiosqlite.OperationalError as e:
            # SQLite < 3.35 lacks DROP COLUMN; fall through to the full
            # rebuild below. (Realistically every supported environment is
            # 3.35+, but the fallback keeps init_db crash-free either way.)
            logger.warning(
                "ALTER TABLE DROP COLUMN unsupported (%s); rebuilding chapters", e,
            )
    logger.info("rebuilding chapters / novels / style_edits to drop humanizer + review columns")
    await conn.execute("PRAGMA foreign_keys = OFF")
    try:
        await conn.execute("BEGIN")
        # ----- chapters rebuild -----
        # Cost columns (input_tokens / output_tokens / cached_input_tokens /
        # cost_usd) are part of the live schema; without them here, a rebuild
        # leaves the chapters table without the columns the live SQL expects,
        # and subsequent SELECTs fail with "no such column: cost_usd". Same
        # defense-in-depth as refinement_status below.
        await conn.execute(
            """
            CREATE TABLE chapters__new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
                chapter_num INTEGER NOT NULL,
                title_zh TEXT,
                title_en TEXT,
                original_text TEXT NOT NULL,
                translated_text TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'translating', 'done', 'error')),
                error_msg TEXT,
                translate_queued INTEGER NOT NULL DEFAULT 0,
                force_retranslate INTEGER NOT NULL DEFAULT 0,
                translation_degraded INTEGER NOT NULL DEFAULT 0,
                glossary_merge_error TEXT,
                refinement_status TEXT NOT NULL DEFAULT 'none'
                    CHECK (refinement_status IN ('none', 'pending', 'in_progress', 'done', 'error')),
                refined_text TEXT,
                refinement_error TEXT,
                refined_at TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cached_input_tokens INTEGER,
                cost_usd REAL,
                translated_at TEXT,
                import_source_url TEXT,
                import_fetched_at TEXT,
                UNIQUE (novel_id, chapter_num)
            )
            """
        )
        # The refinement_* / cost / translated_at columns were added by the
        # additive-migration batch that ran moments ago, but defense-in-depth:
        # re-snapshot the actual column set right now and carry forward
        # whichever subset exists. Same belt-and-suspenders pattern as the
        # novels rebuild's n_cols_now recheck below.
        cols_now = await _chapter_columns(conn)
        has_refinement = "refinement_status" in cols_now
        has_cost = "cost_usd" in cols_now
        has_translated_at = "translated_at" in cols_now
        # Cost + translated_at SELECT/INSERT fragments — empty when the
        # source rows don't have the columns yet (so the INSERT defaults
        # them to NULL).
        cost_select = (
            ", input_tokens, output_tokens, cached_input_tokens, cost_usd"
            if has_cost else ", NULL, NULL, NULL, NULL"
        )
        cost_cols = ", input_tokens, output_tokens, cached_input_tokens, cost_usd"
        ta_select = ", translated_at" if has_translated_at else ", NULL"
        ta_cols = ", translated_at"
        if has_refinement:
            await conn.execute(
                "INSERT INTO chapters__new "
                "(id, novel_id, chapter_num, title_zh, title_en, original_text, "
                "translated_text, status, error_msg, translate_queued, "
                "force_retranslate, translation_degraded, glossary_merge_error, "
                "refinement_status, refined_text, refinement_error, refined_at"
                + cost_cols + ta_cols + ") "
                "SELECT id, novel_id, chapter_num, title_zh, title_en, original_text, "
                "translated_text, status, error_msg, translate_queued, "
                "force_retranslate, translation_degraded, glossary_merge_error, "
                "COALESCE(refinement_status, 'none'), refined_text, refinement_error, refined_at"
                + cost_select + ta_select + " FROM chapters"
            )
        else:
            await conn.execute(
                "INSERT INTO chapters__new "
                "(id, novel_id, chapter_num, title_zh, title_en, original_text, "
                "translated_text, status, error_msg, translate_queued, "
                "force_retranslate, translation_degraded, glossary_merge_error"
                + cost_cols + ta_cols + ") "
                "SELECT id, novel_id, chapter_num, title_zh, title_en, original_text, "
                "translated_text, status, error_msg, translate_queued, "
                "force_retranslate, translation_degraded, glossary_merge_error"
                + cost_select + ta_select + " FROM chapters"
            )
        # Drop FTS triggers first — they reference the old columns and would
        # error during DROP TABLE chapters.
        await conn.execute("DROP TRIGGER IF EXISTS chapter_fts_ai")
        await conn.execute("DROP TRIGGER IF EXISTS chapter_fts_ad")
        await conn.execute("DROP TRIGGER IF EXISTS chapter_fts_au")
        await conn.execute("DROP TABLE IF EXISTS chapter_fts")
        # DROP TABLE chapter_fts is *supposed* to cascade to FTS5's shadow
        # tables, but in practice (aiosqlite inside an explicit BEGIN, observed
        # on this novel's DB) the shadow tables survive — and the subsequent
        # _FTS_SCHEMA CREATE IF NOT EXISTS then shares B-tree state with the
        # stale shadow rows. Writes to chapters then fire the FTS triggers and
        # raise "database disk image is malformed." Drop the shadow tables
        # explicitly to make the FTS rebuild deterministic.
        for shadow in (
            "chapter_fts_data",
            "chapter_fts_idx",
            "chapter_fts_docsize",
            "chapter_fts_config",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {shadow}")
        await conn.execute("DROP TABLE chapters")
        await conn.execute("ALTER TABLE chapters__new RENAME TO chapters")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chapters_novel ON chapters(novel_id, chapter_num)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chapters_translate_queued "
            "ON chapters(novel_id, chapter_num) WHERE translate_queued = 1"
        )

        # ----- novels rebuild (drop humanizer_*) -----
        n_cols = await _novel_columns(conn)
        if "humanizer_tone" in n_cols:
            await conn.execute(
                """
                CREATE TABLE novels__new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    source_type TEXT NOT NULL CHECK (source_type IN ('paste', 'txt', 'url', 'epub', 'docx', 'html')),
                    source_url TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    style_note TEXT,
                    source_language TEXT NOT NULL DEFAULT 'zh',
                    genre TEXT,
                    custom_style_brief TEXT,
                    translator_provider_id INTEGER REFERENCES providers(id) ON DELETE SET NULL,
                    refinement_provider_id INTEGER REFERENCES providers(id) ON DELETE SET NULL,
                    import_status TEXT,
                    last_read_chapter_num INTEGER,
                    last_read_at TEXT
                )
                """
            )
            # style_note / source_language / genre / custom_style_brief /
            # translator_provider_id / refinement_provider_id may or may not
            # exist on the old novels table at this point. The additive
            # migrations above add them, but if any haven't landed yet
            # (defense-in-depth) handle the subset. n_cols was snapshotted
            # before the ALTERs in init_db ran; re-check now.
            n_cols_now = await _novel_columns(conn)
            select_cols = ["id", "title", "source_type", "source_url", "created_at"]
            insert_cols = list(select_cols)
            # style_note from the 2026-05-22 single-pass restructure.
            if "style_note" in n_cols_now:
                select_cols.append("style_note")
                insert_cols.append("style_note")
            # 2026-05-23 per-novel provider + genre fields. Carry whichever
            # subset is currently present; the schema columns default to NULL
            # (or 'zh' for source_language) so omitted ones get sane values.
            for col in (
                "source_language",
                "genre",
                "custom_style_brief",
                "translator_provider_id",
                "refinement_provider_id",
                # 2026-05-26 resumable imports: carry through if the
                # additive migration already added it (it runs before
                # this rebuild). Humanizer-era DBs without the column
                # land here as NULL → treated as "atomic-create / done"
                # implicitly, same as legacy novels.
                "import_status",
                # 2026-05-28 durable reading position: carry through so a
                # one-time humanizer-era rebuild doesn't reset the reader's
                # resume point. Absent on humanizer-era DBs → NULL.
                "last_read_chapter_num",
                "last_read_at",
            ):
                if col in n_cols_now:
                    select_cols.append(col)
                    insert_cols.append(col)
            insert_sql = (
                f"INSERT INTO novels__new ({', '.join(insert_cols)}) "
                f"SELECT {', '.join(select_cols)} FROM novels"
            )
            await conn.execute(insert_sql)
            await conn.execute("DROP TABLE novels")
            await conn.execute("ALTER TABLE novels__new RENAME TO novels")

        # ----- style_edits rebuild (drop variant) -----
        s_cols = await _style_edits_columns(conn)
        if "variant" in s_cols:
            await conn.execute(
                """
                CREATE TABLE style_edits__new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
                    chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
                    before_text TEXT NOT NULL,
                    after_text TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            await conn.execute(
                "INSERT INTO style_edits__new (id, novel_id, chapter_id, before_text, after_text, created_at) "
                "SELECT id, novel_id, chapter_id, before_text, after_text, created_at FROM style_edits"
            )
            await conn.execute("DROP TABLE style_edits")
            await conn.execute("ALTER TABLE style_edits__new RENAME TO style_edits")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_style_edits_novel ON style_edits(novel_id, id)"
            )

        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.execute("PRAGMA foreign_keys = ON")


_NARROW_SOURCE_TYPE_CHECK = "CHECK (source_type IN ('paste', 'txt', 'url'))"
_WIDE_SOURCE_TYPE_CHECK = (
    "CHECK (source_type IN ('paste', 'txt', 'url', 'epub', 'docx', 'html'))"
)


async def _widen_source_type_check(conn: aiosqlite.Connection) -> None:
    """Rebuild novels so source_type accepts the Initiative-7 values
    ('epub', 'docx', 'html') alongside the legacy three ('paste', 'txt',
    'url'). No-op on fresh DBs (the SCHEMA already widened) and on DBs
    already rebuilt.

    Strategy: read the original DDL from sqlite_master.sql and surgically
    replace the narrow CHECK string with the wider one, then run the
    create-new + copy + drop-old + rename swap. This preserves EVERY
    other column-level constraint (DEFAULT expressions, CHECK clauses,
    UNIQUE, REFERENCES) verbatim instead of round-tripping through
    PRAGMA table_info — which strips parens from expression defaults
    (Bug #1) and drops CHECK constraints entirely (Bug #4).

    The novels table has FK references from chapters, glossary_entries,
    bookmarks, tm_segments, style_edits, observations — but every one is
    `ON DELETE CASCADE`, so `PRAGMA foreign_keys = OFF` during the
    rebuild keeps the rows intact while the table swaps."""
    cur = await conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'novels'"
    )
    row = await cur.fetchone()
    if row is None:
        return
    ddl = row[0] or ""
    # The legacy DDL had exactly these three values in the CHECK. If 'epub'
    # is already present, the rebuild has already happened OR a fresh DB
    # used the widened SCHEMA — either way, nothing to do.
    if "'epub'" in ddl or "epub" in ddl:
        return
    if _NARROW_SOURCE_TYPE_CHECK not in ddl:
        # Custom or already-modified DDL. Don't touch it.
        return
    logger.info("rebuilding novels to widen source_type CHECK for Initiative 7")

    # Surgery: keep everything else verbatim; only the source_type CHECK
    # changes. We also need to rename the table from `novels` to
    # `novels__new` so we can DROP the old one and RENAME this in.
    new_ddl = ddl.replace(_NARROW_SOURCE_TYPE_CHECK, _WIDE_SOURCE_TYPE_CHECK, 1)
    # CREATE TABLE [IF NOT EXISTS] novels ( -> CREATE TABLE novels__new (
    # The original DDL stored in sqlite_master.sql never includes
    # `IF NOT EXISTS` (SQLite strips it on storage), so a simple anchored
    # replace is safe. Use a regex with leading-whitespace tolerance for
    # paranoia — the live DDL we saw starts with `CREATE TABLE novels`.
    import re
    new_ddl = re.sub(
        r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?novels[\"']?\s*\(",
        "CREATE TABLE novels__new (",
        new_ddl,
        count=1,
        flags=re.IGNORECASE,
    )
    if "CREATE TABLE novels__new" not in new_ddl:
        # Defence-in-depth: if the regex didn't match (unexpected DDL
        # shape) bail out with a logged error rather than running the
        # original `CREATE TABLE novels` which would conflict with the
        # existing table.
        logger.error(
            "novels DDL did not match expected CREATE-TABLE prefix; aborting migration"
        )
        return

    # Column list for INSERT-from-old. PRAGMA gives us the names in the
    # right order without parsing the DDL ourselves.
    cur = await conn.execute("PRAGMA table_info(novels)")
    col_names = [r[1] for r in await cur.fetchall()]
    col_list = ", ".join(col_names)

    await conn.execute("PRAGMA foreign_keys = OFF")
    try:
        await conn.execute("BEGIN")
        await conn.execute(new_ddl)
        await conn.execute(
            f"INSERT INTO novels__new ({col_list}) SELECT {col_list} FROM novels"
        )
        await conn.execute("DROP TABLE novels")
        await conn.execute("ALTER TABLE novels__new RENAME TO novels")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.execute("PRAGMA foreign_keys = ON")


async def _drop_pricing_columns(conn: aiosqlite.Connection) -> None:
    """One-shot drop of pricing_input_per_mtok / pricing_output_per_mtok from
    providers. Idempotent: a no-op when the columns are already gone.

    Why: the per-token pricing UX (user-entered USD-per-1M-tokens fields on
    the Add Provider dialog) was removed when the catalog redesign expanded
    the provider list from 4 to 17 types — asking the user to look up
    per-token prices for every new vendor was friction without payoff. The
    columns and the `_estimated_remaining_cost` projection they fed are
    both dropped together.

    Follows the `_drop_dead_columns` pattern: try `ALTER TABLE DROP COLUMN`
    first (SQLite 3.35+ supports it), only fall back to a full table rebuild
    if the DROP COLUMN errors. The append-only `_ADDITIVE_MIGRATIONS` list
    still re-adds these columns to fresh DBs on every boot, so this drop
    runs once per startup; on a stable DB that already lacks the columns,
    `table_info` short-circuits the work.
    """
    cur = await conn.execute("PRAGMA table_info(providers)")
    cols = {r[1] for r in await cur.fetchall()}
    targets = {"pricing_input_per_mtok", "pricing_output_per_mtok"}
    to_drop = sorted(targets & cols)
    if not to_drop:
        return
    for col in to_drop:
        try:
            await conn.execute(f"ALTER TABLE providers DROP COLUMN {col}")
        except aiosqlite.OperationalError as e:
            # SQLite < 3.35 lacks DROP COLUMN. In practice every supported
            # environment is 3.35+ (Python 3.11+ ships SQLite 3.37+); if we
            # somehow land on an older runtime, log and bail — the columns
            # are nullable so leaving them in place is harmless.
            logger.warning(
                "ALTER TABLE providers DROP COLUMN %s failed (%s); leaving "
                "the column in place. Newer code ignores it.",
                col, e,
            )
            return
    await conn.commit()
    logger.info("dropped pricing columns from providers: %s", to_drop)


async def _drop_glossary_category_check(conn: aiosqlite.Connection) -> None:
    """Rebuild glossary_entries without the legacy CHECK on category. No-op
    on fresh DBs and on DBs already rebuilt."""
    cur = await conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'glossary_entries'"
    )
    row = await cur.fetchone()
    if row is None:
        return
    ddl = row[0] or ""
    if "CHECK (category IN" not in ddl:
        return
    logger.info("rebuilding glossary_entries to drop legacy category CHECK")
    await conn.execute("PRAGMA foreign_keys = OFF")
    try:
        await conn.execute("BEGIN")
        await conn.execute(
            """
            CREATE TABLE glossary_entries__new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
                term_zh TEXT NOT NULL,
                term_en TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'other',
                notes TEXT,
                usage_note TEXT,
                auto_detected INTEGER NOT NULL DEFAULT 1,
                locked INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (novel_id, term_zh)
            )
            """
        )
        # Both `usage_note` and `updated_at` may or may not exist yet — the
        # additive migrations run before this rebuild, so they normally do,
        # but a partially-applied or very old DB could lack them. Compose
        # the INSERT column list conditionally.
        cur = await conn.execute("PRAGMA table_info(glossary_entries)")
        g_cols = {r[1] for r in await cur.fetchall()}
        base_cols = "id, novel_id, term_zh, term_en, category, notes, auto_detected, locked"
        extra_cols = []
        if "usage_note" in g_cols:
            extra_cols.append("usage_note")
        if "updated_at" in g_cols:
            extra_cols.append("updated_at")
        col_list = base_cols + ("".join(", " + c for c in extra_cols))
        await conn.execute(
            f"INSERT INTO glossary_entries__new ({col_list}) "
            f"SELECT {col_list} FROM glossary_entries"
        )
        await conn.execute("DROP TABLE glossary_entries")
        await conn.execute("ALTER TABLE glossary_entries__new RENAME TO glossary_entries")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_glossary_novel ON glossary_entries(novel_id)"
        )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.execute("PRAGMA foreign_keys = ON")


async def init_db() -> None:
    # Create the data directories before opening the DB. Done here (and in the
    # app_entry launch path) instead of at config import so importing config is
    # side-effect-free.
    ensure_data_dirs()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA journal_mode = WAL")
        await _apply_conn_pragmas(conn)
        await conn.executescript(SCHEMA)
        for stmt in _ADDITIVE_MIGRATIONS:
            try:
                await conn.execute(stmt)
            except aiosqlite.OperationalError as e:
                msg = str(e).lower()
                if "duplicate column" in msg or "already exists" in msg:
                    continue
                # SQLite refuses ALTER TABLE ADD COLUMN with non-constant
                # DEFAULT — `(datetime('now'))` falls under that even
                # though it's allowed in CREATE TABLE. The matching column
                # already exists on every DB created from SCHEMA; the
                # ALTER form is only ever hit by DBs predating that
                # migration. Skip-and-continue keeps init_db forward-
                # progressing through the rest of the list (including
                # newer migrations added after the offender) rather than
                # crashing the whole startup. The latent missing column,
                # if there is one, surfaces as a read-time error in the
                # code that uses it — caught by tests on a fresh DB and
                # by an explicit error in production.
                if "cannot add a column with non-constant default" in msg:
                    logger.warning(
                        "skipping ALTER with non-constant DEFAULT (pre-existing "
                        "DB state): %s",
                        stmt,
                    )
                    continue
                logger.exception(
                    "additive migration failed (not a duplicate/already-exists error): %s",
                    stmt,
                )
                raise
        await conn.commit()

        # One-shot data migration before column drop, so humanized text (the
        # text the reader actually showed) becomes the new canonical
        # translated_text.
        humanized_migrated = await _migrate_humanized_into_translated(conn)

        # One-shot rebuild dropping humanizer + review columns from chapters,
        # humanizer_* from novels, variant from style_edits.
        await _drop_dead_columns(conn)

        # Legacy CHECK on glossary_entries.category — separate one-shot rebuild.
        await _drop_glossary_category_check(conn)

        # Catalog redesign 2026-05-26: drop pricing columns from providers.
        # Idempotent; runs once per boot in case the additive migrations
        # re-added them on a fresh DB.
        await _drop_pricing_columns(conn)

        # Initiative 7: widen novels.source_type CHECK to accept 'epub' / 'docx'
        # / 'html'. No-op on fresh DBs and on already-widened ones.
        await _widen_source_type_check(conn)

        # FTS5 virtual table + sync triggers. Idempotent.
        # Phase 4: when the existing chapter_fts_au trigger doesn't yet
        # reference `refined_text`, the table + shadow tables + triggers
        # all get dropped and recreated cleanly. CRITICAL: dropping only
        # the triggers leaves the shadow tables in a state that corrupts
        # the next UPDATE that fires the new trigger ('database disk
        # image is malformed'). Same FTS5 quirk documented inside
        # _drop_dead_columns above.
        try:
            cur = await conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='trigger' AND name='chapter_fts_au'"
            )
            row = await cur.fetchone()
            existing_au_ddl = (row[0] or "") if row else ""
            needs_rebuild = row is not None and "refined_text" not in existing_au_ddl
            if needs_rebuild:
                logger.info("rebuilding chapter_fts to install Phase 4 refined_text-aware triggers")
                await conn.execute("DROP TRIGGER IF EXISTS chapter_fts_ai")
                await conn.execute("DROP TRIGGER IF EXISTS chapter_fts_ad")
                await conn.execute("DROP TRIGGER IF EXISTS chapter_fts_au")
                await conn.execute("DROP TABLE IF EXISTS chapter_fts")
                # FTS5 shadow tables don't reliably cascade — drop explicitly.
                for shadow in (
                    "chapter_fts_data",
                    "chapter_fts_idx",
                    "chapter_fts_docsize",
                    "chapter_fts_config",
                ):
                    await conn.execute(f"DROP TABLE IF EXISTS {shadow}")
                await conn.commit()
            await conn.executescript(_FTS_SCHEMA)
            # Backfill the FTS table from chapters that pre-date the trigger.
            # Use the same COALESCE shape so the backfill matches the
            # trigger inserts.
            await conn.execute(
                "INSERT INTO chapter_fts(rowid, title_en, translated_text) "
                "SELECT c.id, COALESCE(c.title_en, ''), "
                "COALESCE(c.refined_text, c.translated_text, '') "
                "FROM chapters c "
                "LEFT JOIN chapter_fts f ON f.rowid = c.id "
                "WHERE f.rowid IS NULL"
            )
        except aiosqlite.OperationalError as e:
            logger.warning("FTS5 setup skipped: %s", e)

        # Orphan recovery: any chapter still in `translating` after a server
        # restart belonged to a background task that no longer exists. Reset
        # to `pending` so the user can re-queue.
        cur = await conn.execute(
            "UPDATE chapters SET status = 'pending', error_msg = NULL "
            "WHERE status = 'translating'"
        )
        translating_reset = cur.rowcount or 0

        # Stale translate_queued on terminal rows.
        cur = await conn.execute(
            "UPDATE chapters SET translate_queued = 0 "
            "WHERE translate_queued = 1 AND status IN ('done', 'error')"
        )
        stale_translate_cleared = cur.rowcount or 0
        await conn.commit()

    LAST_ORPHAN_RECOVERY["translating_reset"] = translating_reset
    LAST_ORPHAN_RECOVERY["stale_translate_cleared"] = stale_translate_cleared
    LAST_ORPHAN_RECOVERY["humanized_migrated"] = humanized_migrated
    if translating_reset or stale_translate_cleared or humanized_migrated:
        logger.info(
            "orphan recovery: %d translating→pending, %d stale translate_queued cleared, "
            "%d humanized_text rows migrated to translated_text",
            translating_reset, stale_translate_cleared, humanized_migrated,
        )


@asynccontextmanager
async def open_conn() -> AsyncIterator[aiosqlite.Connection]:
    """Open an aiosqlite connection with project conventions applied."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await _apply_conn_pragmas(conn)
        yield conn


async def get_conn() -> AsyncIterator[aiosqlite.Connection]:
    async with open_conn() as conn:
        yield conn
