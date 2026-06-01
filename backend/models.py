from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MAX_PASTE_CHARS = 25_000_000
MAX_TITLE_CHARS = 200

Category = Literal["character", "technique", "item", "place", "other", "idiom"]
Status = Literal["pending", "translating", "done", "error"]
SourceType = Literal["paste", "txt", "url", "epub", "docx", "html"]  # 'url' Phase 5; epub/docx/html added Initiative 7.
RefinementStatus = Literal["none", "pending", "in_progress", "done", "error"]


class PasteRequest(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    text: str = Field(min_length=1, max_length=MAX_PASTE_CHARS)
    # 2026-05-25: per-novel genre picked by the user at import time. None
    # leaves novels.genre NULL; the user can set it later on the novel
    # overview page. Validated against backend/genres.py::GENRES inside
    # the route handler (kept off the model so the import doesn't pull
    # in genres for callers that don't need it).
    genre: str | None = Field(default=None, max_length=64)


class AppendPasteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_PASTE_CHARS)


class ScrapeRequest(BaseModel):
    """Body for POST /api/translate/scrape (Phase 5).

    The scraper fetches `url`, runs trafilatura over it, and creates a
    novel from the extracted text. `title` is optional — when absent, the
    scraper uses the page's <title> or the hostname as a fallback. When
    `novel_id` is set, the scraped text appends to the existing novel
    instead of creating a new one (matches /append/{novel_id}/paste).

    `cookies` is an optional Cookie-header string the user pastes from
    their browser's devtools — the escape hatch for sites where the
    scraper's browser-shaped headers alone don't get past Cloudflare's
    challenge. Capped at 8 KB to stop a misclick from posting a megabyte
    of body.
    """
    url: str = Field(min_length=1, max_length=2000)
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)
    novel_id: int | None = None
    cookies: str | None = Field(default=None, max_length=8192)
    # 2026-05-25: per-novel genre at scrape time. When provided, takes
    # precedence over the recipe's default_genre. When None, falls back to
    # the recipe's default_genre (e.g. 69shuba → 'xianxia') or NULL for
    # the generic-trafilatura path.
    genre: str | None = Field(default=None, max_length=64)


_MAX_EDIT_PARAGRAPH_CHARS = 50_000


class EditParagraphRequest(BaseModel):
    """Body for POST /novels/{id}/chapters/{n}/edit-paragraph.

    paragraph_index is the 0-based index into chunks = body.split('\\n\\n').
    before_md is the current paragraph text at that index — the server
    checks this is still equal before applying, so a concurrent retranslate
    is detected (409) rather than silently smashed. after_text is what to
    write in its place.

    source picks which body the edit applies to:
      - 'draft'   → chapters.translated_text (the translator's output)
      - 'refined' → chapters.refined_text (the refiner's polish pass)

    The reader sets this based on which body is currently being displayed
    (refined_text when refinement_status='done', draft otherwise). Default
    is 'draft' so older clients (and any caller that doesn't know about the
    refiner) keep editing the translator's output."""

    paragraph_index: int = Field(ge=0)
    before_md: str = Field(min_length=1, max_length=_MAX_EDIT_PARAGRAPH_CHARS)
    after_text: str = Field(min_length=1, max_length=_MAX_EDIT_PARAGRAPH_CHARS)
    source: Literal["draft", "refined"] = "draft"


class Novel(BaseModel):
    id: int
    title: str
    source_type: SourceType
    source_url: str | None
    created_at: str
    # Per-novel style brief (voice / register / imagery summary) injected
    # into every chapter-translation prompt. NULL until the user sets it
    # via PATCH /api/novels/{id} (the reader's ✒ Style note dialog).
    style_note: str | None = None
    # Source language key. 'zh' for the current Chinese-only translator;
    # future parser/encoding expansion branches on this.
    source_language: str = "zh"
    # Genre key from backend.genres.GENRES. NULL falls back to DEFAULT_GENRE.
    genre: str | None = None
    # Free-text style brief that overrides the genre overlay when set.
    custom_style_brief: str | None = None
    # Per-novel provider selection. NULL on translator → use default. NULL on
    # refinement → refinement OFF.
    translator_provider_id: int | None = None
    refinement_provider_id: int | None = None
    # Initiative 2 metadata. All optional — older novels keep working with
    # NULLs. The cover_image_path is filled in by the upload route; clients
    # should fetch /api/novels/{id}/cover rather than constructing URLs.
    author: str | None = None
    original_title: str | None = None
    synopsis: str | None = None
    status: str | None = None
    cover_image_path: str | None = None
    # Provenance of the cover image: 'epub' | 'url' | 'upload' | None.
    # Library cards key the source pip ("scraped" / "epub" badge) off this.
    cover_source: str | None = None
    series_name: str | None = None
    series_index: int | None = None
    # 2026-05-25 F11: archive timestamp. NULL on active novels; set
    # when the user soft-deletes via DELETE /api/novels/{id}.
    deleted_at: str | None = None
    # 2026-05-28 durable reading position. NULL until the reader records a
    # position; reader boot prefers this over the localStorage breadcrumb so
    # reopening the app resumes on the last-read chapter. last_read_at drives
    # the library "Continue reading" sort. Written by
    # PUT /api/novels/{id}/reading-position.
    last_read_chapter_num: int | None = None
    last_read_at: str | None = None


class NovelWithProgress(Novel):
    total_chapters: int
    done_chapters: int
    translate_queue: int = 0
    # Distinct chapters with translate_queued=1. With the single-pass pipeline
    # this is identical to translate_queue, kept as a named field for the
    # frontend's queue badge.
    queue_chapters: int = 0
    translating_now: int = 0
    first_chapter_num: int | None = None
    # 2026-05-26 resumable imports: surface the lifecycle so the library
    # card can render an in-progress / paused badge. NULL means atomic-
    # create path or pre-feature row (treated as done).
    import_status: str | None = None
    # Skeleton chapter rows that haven't been filled yet (recipe scrapes
    # awaiting fetch). 0 for atomic-create novels and finished imports.
    import_pending_chapters: int = 0


class NovelUpdate(BaseModel):
    """Partial-update body for PATCH /novels/{id}.

    Optional fields. Pydantic v2 distinguishes "field omitted" from
    "field set to NULL" via `model_fields_set` — callers in routes/novels.py
    check that set to decide which columns to UPDATE.
    """

    title: str | None = Field(default=None, min_length=1, max_length=200)
    style_note: str | None = None
    source_language: str | None = Field(default=None, min_length=1, max_length=16)
    genre: str | None = None
    custom_style_brief: str | None = None
    translator_provider_id: int | None = None
    refinement_provider_id: int | None = None
    # Initiative 2 metadata. All optional in the partial-update body. Pydantic
    # v2's model_fields_set lets the route distinguish "omitted" from
    # "explicitly set to None" — None CLEARS the column.
    author: str | None = Field(default=None, max_length=200)
    original_title: str | None = Field(default=None, max_length=300)
    synopsis: str | None = Field(default=None, max_length=10_000)
    status: str | None = Field(default=None, max_length=32)
    series_name: str | None = Field(default=None, max_length=200)
    series_index: int | None = Field(default=None, ge=0)


class ReadingPositionUpdate(BaseModel):
    """Body for PUT /novels/{id}/reading-position.

    Records the chapter the reader is on so reopening the app resumes there.
    `chapter_num` is the novel-local chapter number (>= 1). We deliberately do
    NOT validate that the chapter exists: the write fires on every chapter open
    and an existence check would add a SELECT per open and race partial imports.
    The reader's boot guard already falls back to the first available chapter if
    the stored position points at a since-deleted chapter.
    """

    chapter_num: int = Field(ge=1)


class MassQueueRequest(BaseModel):
    """Body for POST /novels/{id}/queue.

    Bulk version of POST /novels/{id}/chapters/{n}/retranslate. Lets the user
    queue many chapters in a single click instead of clicking each one.

    Modes:
      - 'all_untranslated' — every chapter that isn't done and isn't already
        translating. Skips chapters already flagged translate_queued=1.
      - 'range' — chapter_num in [from_chapter, to_chapter]. Same skip rules.

    `include_errors`: when False, chapters with status='error' are left alone
    (the user explicitly wants to retry only via the Retry-all banner). When
    True (the default), errored chapters are reset to pending and re-queued
    through the same path as a per-chapter retranslate."""

    mode: Literal["all_untranslated", "range"] = "all_untranslated"
    from_chapter: int | None = Field(default=None, ge=1)
    to_chapter: int | None = Field(default=None, ge=1)
    include_errors: bool = True


class Chapter(BaseModel):
    id: int
    novel_id: int
    chapter_num: int
    title_zh: str | None
    title_en: str | None
    original_text: str
    translated_text: str | None
    status: Status
    error_msg: str | None
    translate_queued: bool = False
    glossary_merge_error: str | None = None
    translation_degraded: bool = False
    # Phase 4 refinement state. `refinement_status` is the state machine;
    # `refined_text` is non-NULL once status='done'. The reader picks which
    # text to display: refined_text when done, translated_text otherwise.
    refinement_status: RefinementStatus = "none"
    refined_text: str | None = None
    refinement_error: str | None = None
    refined_at: str | None = None
    # Phase B Design v2: which provider ran the refinement pass. Lets the
    # reader's bilingual pane label show a "refined by X" attribution chip.
    # NULL for pre-migration rows and chapters that never refined.
    refined_by_provider_id: int | None = None
    # 2026-05-26 free-tier mechanical NMT draft (Google Translate as of
    # 2026-05-28). ``free_draft_text`` holds the draft; the LLM PEMT pass
    # reads it as a fidelity reference (see
    # services/translators/base.py::build_prompt). The reader may render
    # this while ``translated_text`` is still NULL.
    free_draft_text: str | None = None
    free_draft_status: str = "none"
    free_draft_error: str | None = None
    free_draft_completed_at: str | None = None
    # Per-chapter translator provenance — mirrors refined_by_provider_id.
    # Drives the reader's banner copy: free-tier rough draft vs LLM polished
    # vs LLM PEMT-merged. NULL on pre-migration rows.
    translated_by_provider_id: int | None = None


class ChapterSummary(BaseModel):
    chapter_num: int
    title_zh: str | None
    title_en: str | None
    status: Status
    translate_queued: bool = False


class CandidateTerm(BaseModel):
    """One pre-flight glossary-saturation candidate: a CN run that recurs in
    the chapter but isn't in the glossary yet. Produced by
    `glossary_filters.detect_candidate_terms`."""

    term: str
    count: int


class OcrIssues(BaseModel):
    """OCR-corruption heuristic result for one chapter's source text.
    Produced by `parser.detect_ocr_issues`."""

    score: int
    issues: list[str]
    flagged: bool


class ChapterSaturation(BaseModel):
    """Response for GET /novels/{id}/chapters/{n}/saturation. Cheap pre-flight
    checks (glossary candidates + OCR heuristics) with no LLM call. The reader
    reads `candidates` to highlight not-yet-glossed terms in the preview."""

    candidates: list[CandidateTerm]
    glossary_size: int
    ocr_issues: OcrIssues


class ChapterSearchMatch(BaseModel):
    """One full-text-search hit inside a novel's chapters. `snippet` carries
    SQLite FTS5 <mark>...</mark> highlighting that the reader renders inline."""

    chapter_num: int
    title_en: str | None
    title_zh: str | None
    status: Status
    snippet: str


class ChapterSearchResults(BaseModel):
    """Response for GET /novels/{id}/search. Wraps the match list so the
    reader's TOC search can render result rows with jump-to-chapter links."""

    matches: list[ChapterSearchMatch]


class DeleteCounts(BaseModel):
    """Response for GET /novels/{id}/delete-counts. Mirrors the
    `services.soft_delete.DeleteCounts` dataclass field-for-field so the
    quantified-confirm dialog can show exactly what archiving / purging
    would affect before the user commits."""

    novel_id: int
    chapters: int
    glossary_entries: int
    bookmarks: int
    chapter_observations: int
    tm_segments: int
    fr_snapshots: int


class Observation(BaseModel):
    """One deterministic detect_* hit (or the implicit translation_degraded /
    glossary_merge_error kind) persisted from the queue worker. Reader's QA
    sidebar groups these per chapter and renders dismiss buttons.

    `paragraph_index` is None until individual observers are extended to
    expose match offsets; the sidebar hides the jump-to-paragraph affordance
    when it's None.
    """

    id: int
    chapter_id: int
    kind: str
    severity: Literal["info", "warn"]
    # F26 (2026-05-25): 'semantic' (meaning-loss signal: missing terms,
    # malformed compounds, predicate loss, translation_degraded,
    # glossary_merge_error, tm_inconsistency) vs 'stylistic' (prose
    # advisory: MT-texture, double possessive, mid-sentence breaks,
    # intensifier inflation, locked-idiom grammar). Drives the split
    # library badge "⚠ N semantic / ⓘ N stylistic".
    severity_tier: Literal["semantic", "stylistic"] = "stylistic"
    paragraph_index: int | None
    excerpt: str
    created_at: str
    dismissed_at: str | None


class ObservationsSummary(BaseModel):
    """Aggregate view: undismissed observation counts grouped by chapter for
    a novel. Used by the library badge and the reader's TOC issue dots —
    one fetch instead of N per-chapter calls."""

    total_undismissed: int
    by_chapter: dict[int, int]  # chapter_num → count


class Bookmark(BaseModel):
    """One reader bookmark on a chapter paragraph (Initiative 2).

    `chapter_num` is denormalized from the FK chapters row at read time so
    the panel can render a chapter heading without a second fetch.
    `paragraph_index` is the 0-based index into the displayed body's
    paragraph split — same shape EditParagraphRequest uses, so jump-to
    is just `bodyEn.children[paragraph_index].scrollIntoView()`."""

    id: int
    novel_id: int
    chapter_id: int
    chapter_num: int
    paragraph_index: int | None
    note: str | None
    created_at: str


class NewBookmark(BaseModel):
    """Body for POST /api/novels/{id}/chapters/{n}/bookmarks."""

    paragraph_index: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=2000)


GlossaryScope = Literal["novel", "global"]


class GlossaryEntry(BaseModel):
    id: int
    # Initiative 3: nullable so global glossary entries — which have no novel
    # — can flow through the same prompt-build pipeline as per-novel rows.
    # Per-novel rows still set this; global rows leave it None.
    novel_id: int | None = None
    term_zh: str
    term_en: str
    category: Category
    notes: str | None
    usage_note: str | None = None
    auto_detected: bool
    locked: bool
    # Initiative 3 scope marker. "novel" for per-novel rows, "global" for
    # cross-novel entries. The prompt-glossary block uses this to render
    # [novel-locked] / [novel-auto] / [global] tags so the translator can
    # see precedence directly. Default keeps the model backward-compatible
    # — existing per-novel callers don't need to set this.
    scope: GlossaryScope = "novel"
    # Design v2 Phase D: when the term was last edited. Drives the stale
    # watermark in the reader / glossary UI ("term changed since chapters
    # using it were last translated"). Nullable for callers that don't
    # surface the column (avoid bloating every glossary response).
    updated_at: str | None = None


class GlobalGlossaryEntry(BaseModel):
    """A glossary term that applies to EVERY novel's translations.

    Always treated as locked. Per-novel entries (auto or locked) with the
    same term_zh take precedence at prompt-build time, so a novel can
    override a global rendering when it needs to."""

    id: int
    term_zh: str
    term_en: str
    category: Category
    notes: str | None
    usage_note: str | None = None
    created_at: str
    updated_at: str


class NewGlobalGlossaryEntry(BaseModel):
    term_zh: str = Field(min_length=1, max_length=200)
    term_en: str = Field(min_length=1, max_length=200)
    category: Category = "character"
    notes: str | None = None
    usage_note: str | None = None


class GlobalGlossaryUpdate(BaseModel):
    term_en: str | None = Field(default=None, min_length=1, max_length=200)
    category: Category | None = None
    notes: str | None = None
    usage_note: str | None = None


class GlobalGlossaryUsage(BaseModel):
    """Per-novel chapter-count impact of one global glossary entry.

    Used by the scope-warning dialog: "editing this term affects N novels /
    M chapters." Counts are based on raw `INSTR(original_text, term_zh)`
    — the same primitive `find_chapters_using_term` uses, so the numbers
    match what the per-novel retranslate-affected workflow would touch."""

    novel_id: int
    novel_title: str
    chapter_count: int


class GlossaryUpdate(BaseModel):
    term_en: str | None = Field(default=None, min_length=1, max_length=200)
    category: Category | None = None
    notes: str | None = None
    usage_note: str | None = None
    locked: bool | None = None


class NewGlossaryEntry(BaseModel):
    term_zh: str = Field(min_length=1, max_length=200)
    term_en: str = Field(min_length=1, max_length=200)
    category: Category = "character"
    notes: str | None = None
    usage_note: str | None = None


class NewTerm(BaseModel):
    zh: str
    en: str
    category: Category = "other"


class TokenUsage(BaseModel):
    """Per-translation LLM token usage. Accumulated across all _complete /
    _complete_plain calls a single translate_chapter makes (including
    retries and the DeepSeek revision pass). Backends that don't expose
    token counts on the response (claude_cli) leave the counts at 0 —
    treat 0 as "unknown" rather than "free"."""

    input_tokens: int = 0
    output_tokens: int = 0
    # Tokens billed at the cached / discounted rate. For Gemini this is
    # `cached_content_token_count`; on backends without a cached-input
    # concept (DeepSeek, Claude CLI) this stays 0.
    cached_input_tokens: int = 0


class TranslationResult(BaseModel):
    title_en: str
    translated_text: str
    new_terms: list[NewTerm] = []
    degraded: bool = False
    # 2026-05-25 F22: optional diagnostics for the translation-attempts
    # log. prompt_snapshot is the full user prompt sent to the LLM
    # (excluding the system instruction, which is cached LRU and
    # huge); parse_error is the envelope-parse failure message when the
    # translator fell back to plain-text mode. Both NULL on the happy
    # path; populated by backends when available so the reader's
    # "Show prompt" / "View attempts" diagnostics can render them.
    prompt_snapshot: str | None = None
    parse_error: str | None = None
    # Token usage for the whole translate_chapter call. None when the
    # backend didn't emit any usage records (e.g. cache hit, claude_cli).
    usage: TokenUsage | None = None


# ----- Provider models -----

class Provider(BaseModel):
    """A user-configured AI provider that the translator can route to."""

    id: int
    name: str
    provider_type: str
    base_url: str | None = None
    model_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None
    is_default: bool = False
    # Stamped by /providers/{id}/test on success; NULL means "never tested".
    last_tested_at: str | None = None
    created_at: str
    updated_at: str


class ProviderCreate(BaseModel):
    """Body for POST /providers."""

    name: str = Field(min_length=1, max_length=100)
    provider_type: str = Field(min_length=1, max_length=50)
    model_id: str = Field(min_length=1, max_length=100)
    base_url: str | None = Field(default=None, max_length=500)
    params: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = Field(default=None, max_length=100)
    is_default: bool = False


class ProviderUpdate(BaseModel):
    """Body for PATCH /providers/{id}. is_default uses a separate endpoint
    (POST /providers/{id}/set-default) because it has cross-row semantics."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    provider_type: str | None = Field(default=None, min_length=1, max_length=50)
    model_id: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str | None = Field(default=None, max_length=500)
    params: dict[str, Any] | None = None
    secret_ref: str | None = Field(default=None, max_length=100)


class ProviderTestResult(BaseModel):
    ok: bool
    message: str


class ProviderStats(BaseModel):
    """Response for GET /providers/{id}/stats. 30-day rollup for the settings
    control-room card: throughput, failure rate, and a fixed-length sparkline
    bucket array aligned to the last N days."""

    provider_id: int
    window_days: int
    chapters_translated_30d: int
    chapters_translated_buckets: list[int]
    failure_rate_30d: float
    failure_count_30d: int
    attempts_30d: int
    last_tested_at: str | None = None


class ProviderRoutedNovel(BaseModel):
    """One novel routed through a provider. `role` is 'translator',
    'refinement', 'both', or 'unknown'."""

    id: int
    title: str
    role: str


class ProviderRoutedNovels(BaseModel):
    """Response for GET /providers/{id}/routed-novels. The settings card shows
    the first `limit` novels as a chip-row with a "+N more" overflow."""

    provider_id: int
    novels: list[ProviderRoutedNovel]
    total: int
    limit: int


class ProviderActivityEvent(BaseModel):
    """One recent translation attempt for the provider activity feed. `status`
    is the bucketed 'ok' / 'warn' / 'err'; `raw_status` is the underlying
    attempt status. `duration_ms` is None when start / finish timestamps are
    missing or unparseable."""

    when_iso: str | None
    status: str
    raw_status: str
    novel_title: str
    chapter_num: int
    duration_ms: int | None
    msg: str


class ProviderActivity(BaseModel):
    """Response for GET /providers/{id}/activity. Last N translation attempts
    on novels routed through this provider, newest first."""

    provider_id: int
    events: list[ProviderActivityEvent]


class NovelGenresResponse(BaseModel):
    """Per-novel genre list: primary (drives the prompt overlay) + every
    secondary tag in insertion order. `all_keys` is a convenience field
    so the UI can iterate over both without re-assembling."""
    primary: str | None
    secondary: list[str]
    all_keys: list[str]


class AddNovelGenreRequest(BaseModel):
    """Body for `POST /api/novels/{id}/genres`. `is_primary=True` swaps
    the chosen key into the primary slot (delegating to set_primary_genre
    internally); default `False` adds it as a secondary tag."""
    genre_key: str
    is_primary: bool = False
