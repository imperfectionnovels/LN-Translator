// Pull a human-useful error detail off a non-ok Response. Reads the body as
// text once, then tries JSON; if the body isn't JSON (HTML error page, plain
// text from a proxy / uvicorn) we still surface a short plain-text body so the
// caller sees more than the bare status. Long bodies (typically HTML error
// pages) fall back to statusText to avoid dumping a wall of markup into a
// toast.
async function _extractError(res) {
  let detail = res.statusText;
  // 2026-05-25 (F06): some routes (notably /api/translate/scrape) wrap
  // their detail in a structured object {message, error_kind} so the UI
  // can render per-cause recovery affordances. We unpack message into
  // the surfaced error text and stash error_kind on the Error object
  // so callers can inspect err.error_kind without re-parsing the body.
  let errorKind = null;
  try {
    const body = await res.text();
    try {
      const parsed = JSON.parse(body);
      if (parsed && parsed.detail) {
        const d = parsed.detail;
        if (d && typeof d === "object" && d.message) {
          detail = d.message;
          errorKind = d.error_kind || null;
        } else {
          detail = d;
        }
      }
    } catch (_) {
      const trimmed = body.trim();
      if (trimmed && trimmed.length < 200 && !trimmed.startsWith("<")) {
        detail = trimmed;
      }
    }
  } catch (_) {}
  const err = new Error(`${res.status}: ${detail}`);
  err.status = res.status;
  if (errorKind) err.error_kind = errorKind;
  return err;
}

async function apiFetch(path, opts = {}) {
  // Only set Content-Type when we're actually sending a body. Setting it on
  // bare GET requests is harmless against this server but pollutes the
  // network tab and triggers CORS preflights if the frontend is ever split
  // out from FastAPI's static mount.
  const headers = { ...(opts.headers || {}) };
  if (opts.body != null && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok) throw await _extractError(res);
  return res.json();
}

const api = {
  paste: (title, text, genre = null) => apiFetch("/api/translate/paste", {
    method: "POST",
    body: JSON.stringify({
      title, text,
      ...(genre ? { genre } : {}),
    }),
  }),
  // 2026-05-25 F05/F06/F08: preview gate. Accepts either { text } or a
  // FormData with a `file` field. Returns detected_chapters + headings
  // + first_chapter_first_500 + format_path. No DB writes.
  importPreview: (textOrFormData) => {
    if (textOrFormData instanceof FormData) {
      return fetch("/api/translate/preview", { method: "POST", body: textOrFormData })
        .then(async r => {
          if (!r.ok) throw await _extractError(r);
          return r.json();
        });
    }
    return apiFetch("/api/translate/preview", {
      method: "POST",
      body: JSON.stringify({ text: textOrFormData }),
    });
  },
  // Phase 5: scrape a URL and import the extracted text. `title` is
  // optional — when omitted, the backend uses the page's <title> or the
  // hostname. `novelId` is optional — when supplied, the scraped text
  // appends to that novel instead of creating a new one. `genre`, when
  // supplied, overrides the per-site recipe's default_genre.
  scrape: (url, title = null, novelId = null, cookies = null, genre = null) => apiFetch("/api/translate/scrape", {
    method: "POST",
    body: JSON.stringify({
      url,
      ...(title ? { title } : {}),
      ...(novelId ? { novel_id: novelId } : {}),
      ...(cookies ? { cookies } : {}),
      ...(genre ? { genre } : {}),
    }),
  }),
  // Recipe-backed URLs (69shuba, syosetu, …) return { job_id, background: true }
  // from /scrape instead of finishing inline. Poll this for progress.
  scrapeJob: (jobId) => apiFetch(`/api/translate/scrape/jobs/${jobId}`),
  upload: (title, file, genre = null) => {
    const fd = new FormData();
    fd.append("title", title);
    fd.append("file", file);
    if (genre) fd.append("genre", genre);
    return fetch("/api/translate/upload", { method: "POST", body: fd })
      .then(async r => {
        if (!r.ok) throw await _extractError(r);
        return r.json();
      });
  },
  appendPaste: (novelId, text) => apiFetch(`/api/translate/append/${novelId}/paste`, {
    method: "POST",
    body: JSON.stringify({ text }),
  }),
  // Insert pasted chapter(s) into the MIDDLE of a novel, after `afterNum`
  // (0 = before the first chapter), renumbering the tail. Unlike appendPaste,
  // which only lands at the end.
  insertChapter: (novelId, afterNum, text, title = null) => apiFetch(`/api/translate/insert/${novelId}`, {
    method: "POST",
    body: JSON.stringify({ after_chapter_num: afterNum, text, ...(title ? { title } : {}) }),
  }),
  appendUpload: (novelId, file) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch(`/api/translate/append/${novelId}/upload`, { method: "POST", body: fd })
      .then(async r => {
        if (!r.ok) throw await _extractError(r);
        return r.json();
      });
  },
  bulkUpload: (title, files, genre = null) => {
    const fd = new FormData();
    fd.append("title", title);
    for (const f of files) fd.append("files", f);
    if (genre) fd.append("genre", genre);
    return fetch("/api/translate/bulk", { method: "POST", body: fd })
      .then(async r => {
        if (!r.ok) throw await _extractError(r);
        return r.json();
      });
  },
  appendBulk: (novelId, files) => {
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    return fetch(`/api/translate/append/${novelId}/bulk`, { method: "POST", body: fd })
      .then(async r => {
        if (!r.ok) throw await _extractError(r);
        return r.json();
      });
  },
  novels: (opts) => {
    // F11 (2026-05-25): ?archived=1 returns soft-deleted novels for the
    // Archive tab. Existing callers still get the default (active) list.
    if (opts && opts.archived) return apiFetch("/api/novels?archived=1");
    return apiFetch("/api/novels");
  },
  // F11: soft-delete + restore + hard-purge + delete-counts preview.
  restoreNovel: (id) =>
    apiFetch(`/api/novels/${id}/restore`, { method: "POST" }),
  purgeNovel: (id) =>
    apiFetch(`/api/novels/${id}/purge`, { method: "DELETE" }),
  deleteCounts: (id) =>
    apiFetch(`/api/novels/${id}/delete-counts`),
  // F36 snapshot history + restore.
  frSnapshots: (novelId) =>
    apiFetch(`/api/novels/${novelId}/fr-snapshots`),
  restoreFrSnapshot: (snapshotId) =>
    apiFetch(`/api/fr-snapshots/${snapshotId}/restore`, { method: "POST" }),
  // F22 translation attempts diagnostics.
  chapterAttempts: (novelId, chapterNum) =>
    apiFetch(`/api/novels/${novelId}/chapters/${chapterNum}/attempts`),
  chapterLastPrompt: (novelId, chapterNum) =>
    apiFetch(`/api/novels/${novelId}/chapters/${chapterNum}/last-prompt`),
  // F26 bulk-dismiss observations.
  bulkDismissChapterObservations: (novelId, chapterNum) =>
    apiFetch(
      `/api/novels/${novelId}/chapters/${chapterNum}/observations/bulk-dismiss`,
      { method: "POST" },
    ),
  bulkDismissObservationsByKind: (novelId, kind) =>
    apiFetch(
      `/api/novels/${novelId}/observations/bulk-dismiss-by-kind/${encodeURIComponent(kind)}`,
      { method: "POST" },
    ),
  // F44 in-app diagnostics.
  diagnostics: () => apiFetch("/api/diagnostics"),
  novel: (id) => apiFetch(`/api/novels/${id}`),
  cacheStats: () => apiFetch(`/api/cache/stats`),
  chapterPreCheck: (id, n) => apiFetch(`/api/novels/${id}/chapters/${n}/pre-check`),
  renameNovel: (id, title) => apiFetch(`/api/novels/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  }),
  // Partial update for the novel record. Pass any subset of:
  //   title, style_note
  // The backend rejects a body with no fields set (400).
  updateNovel: (id, fields) => apiFetch(`/api/novels/${id}`, {
    method: "PATCH",
    body: JSON.stringify(fields),
  }),
  deleteNovel: (id) => apiFetch(`/api/novels/${id}`, { method: "DELETE" }),
  // Record the reader's last-read chapter so reopening the app resumes there.
  // Durable (DB-backed) replacement for the localStorage `lastRead:` breadcrumb.
  setReadingPosition: (id, chapterNum) => apiFetch(`/api/novels/${id}/reading-position`, {
    method: "PUT",
    body: JSON.stringify({ chapter_num: chapterNum }),
  }),
  chapters: (id) => apiFetch(`/api/novels/${id}/chapters`),
  chapter: (id, n) => apiFetch(`/api/novels/${id}/chapters/${n}`),
  retranslate: (id, n) => apiFetch(`/api/novels/${id}/chapters/${n}/retranslate`, { method: "POST" }),
  retryRefinement: (id, n) => apiFetch(`/api/novels/${id}/chapters/${n}/retry-refinement`, { method: "POST" }),
  refreshFreeDraft: (id, n) => apiFetch(`/api/novels/${id}/chapters/${n}/refresh-free-draft`, { method: "POST" }),
  cancelQueueChapter: (id, n) => apiFetch(`/api/novels/${id}/chapters/${n}/queue`, { method: "DELETE" }),
  translateNext: (novelId, chapterNum) => apiFetch(`/api/novels/${novelId}/chapters/${chapterNum}/translate-next`, { method: "POST" }),
  cancelQueueAll: (id) => apiFetch(`/api/novels/${id}/queue`, { method: "DELETE" }),
  // Mass-queue chapters for translation. `body` shape:
  //   { mode: "all_untranslated" | "range",
  //     from_chapter?: number, to_chapter?: number,
  //     include_errors?: boolean }
  // Pending chapters get queue_translations (no force_retranslate); errored
  // chapters take the reset path so the worker can re-claim them. Done /
  // in-flight / already-queued chapters are skipped and counted.
  massQueueChapters: (id, body) => apiFetch(`/api/novels/${id}/queue`, {
    method: "POST",
    body: JSON.stringify(body),
  }),
  globalQueue: () => apiFetch(`/api/novels/queue/all`),
  cancelGlobalQueue: () => apiFetch(`/api/novels/queue/all`, { method: "DELETE" }),
  glossary: (id) => apiFetch(`/api/novels/${id}/glossary`),
  createGlossary: (id, body) => apiFetch(`/api/novels/${id}/glossary`, {
    method: "POST",
    body: JSON.stringify(body),
  }),
  updateGlossary: (entryId, body) => apiFetch(`/api/glossary/${entryId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  }),
  deleteGlossary: (entryId) => apiFetch(`/api/glossary/${entryId}`, { method: "DELETE" }),
  bulkDeleteGlossary: (novelId, ids) => apiFetch(`/api/glossary/bulk-delete`, {
    method: "POST",
    body: JSON.stringify({ novel_id: novelId, ids }),
  }),
  bulkLockGlossary: (novelId, ids, locked) => apiFetch(`/api/glossary/bulk-lock`, {
    method: "POST",
    body: JSON.stringify({ novel_id: novelId, ids, locked }),
  }),
  glossaryHealth: (novelId) => apiFetch(`/api/novels/${novelId}/glossary/health`),
  chapterSaturation: (novelId, chapterNum) =>
    apiFetch(`/api/novels/${novelId}/chapters/${chapterNum}/saturation`),
  getChapterConsistency: (novelId, chapterNum) =>
    apiFetch(`/api/novels/${novelId}/chapters/${chapterNum}/consistency`),
  // Quality cockpit (read-only). qualityScorecard takes an optional "LO-HI"
  // chapter range; qualityConsistency is the novel-level TCR + worst-terms feed.
  qualityScorecard: (novelId, range = null) =>
    apiFetch(`/api/novels/${novelId}/quality${range ? `?chapters=${encodeURIComponent(range)}` : ""}`),
  qualityConsistency: (novelId) => apiFetch(`/api/novels/${novelId}/consistency`),
  chapterQuality: (novelId, chapterNum) =>
    apiFetch(`/api/novels/${novelId}/chapters/${chapterNum}/quality`),
  searchChapters: (novelId, q) =>
    apiFetch(`/api/novels/${novelId}/search?q=${encodeURIComponent(q)}`),
  editParagraph: (novelId, chapterNum, paragraphIndex, beforeMd, afterText, source = "draft") =>
    apiFetch(`/api/novels/${novelId}/chapters/${chapterNum}/edit-paragraph`, {
      method: "POST",
      body: JSON.stringify({
        paragraph_index: paragraphIndex,
        before_md: beforeMd,
        after_text: afterText,
        source,
      }),
    }),
  affectedChapters: (entryId) => apiFetch(`/api/glossary/${entryId}/affected-chapters`),
  retranslateAffected: (entryId) => apiFetch(`/api/glossary/${entryId}/retranslate-affected`, {
    method: "POST",
  }),
  bulkRetranslateAffected: (entryIds) => apiFetch(`/api/glossary/bulk-retranslate-affected`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ entry_ids: entryIds }),
  }),

  // ----- Providers (Phase 1b) -----
  providers: () => apiFetch("/api/providers"),
  provider: (id) => apiFetch(`/api/providers/${id}`),
  createProvider: (fields) => apiFetch("/api/providers", {
    method: "POST",
    body: JSON.stringify(fields),
  }),
  // PATCH body: subset of {name, provider_type, model_id, base_url, params, secret_ref}.
  // Explicit nulls clear the field (base_url, secret_ref); omitted keys leave alone.
  updateProvider: (id, fields) => apiFetch(`/api/providers/${id}`, {
    method: "PATCH",
    body: JSON.stringify(fields),
  }),
  deleteProvider: (id) => apiFetch(`/api/providers/${id}`, { method: "DELETE" }),
  setDefaultProvider: (id) => apiFetch(`/api/providers/${id}/set-default`, {
    method: "POST",
  }),
  testProvider: (id) => apiFetch(`/api/providers/${id}/test`, { method: "POST" }),
  setProviderSecret: (id, value) => apiFetch(`/api/providers/${id}/set-secret`, {
    method: "POST",
    body: JSON.stringify({ value }),
  }),
  deleteProviderSecret: (id) => apiFetch(`/api/providers/${id}/secret`, { method: "DELETE" }),
  providerStats: (id) => apiFetch(`/api/providers/${id}/stats`),
  providerRoutedNovels: (id, limit = 12) =>
    apiFetch(`/api/providers/${id}/routed-novels?limit=${limit}`),
  providerActivity: (id, limit = 6) =>
    apiFetch(`/api/providers/${id}/activity?limit=${limit}`),

  // ----- Diagnostics (About card on /settings) -----
  diagnostics: () => apiFetch("/api/diagnostics"),
  diagnosticsLogFolder: () => apiFetch("/api/diagnostics/log-folder"),

  // ----- Covers -----
  uploadCover: (novelId, file) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch(`/api/novels/${novelId}/cover`, { method: "POST", body: fd })
      .then(async r => {
        if (!r.ok) throw await _extractError(r);
        return r.json();
      });
  },
  deleteCover: (novelId) => apiFetch(`/api/novels/${novelId}/cover`, { method: "DELETE" }),

  // ----- Genres (static list served by the backend genre registry) -----
  genres: () => apiFetch("/api/genres"),

  // 2026-05-25: per-novel genre tags. primary lives on novels.genre
  // (still drives the prompt overlay); secondary tags live in the
  // novel_genres table. The set-primary endpoint swaps transactionally
  // and never observes a no-primary or two-primary intermediate state.
  novelGenres: (novelId) =>
    apiFetch(`/api/novels/${novelId}/genres`),
  addNovelGenre: (novelId, genreKey, isPrimary = false) =>
    apiFetch(`/api/novels/${novelId}/genres`, {
      method: "POST",
      body: JSON.stringify({ genre_key: genreKey, is_primary: isPrimary }),
    }),
  removeNovelGenre: (novelId, genreKey) =>
    apiFetch(`/api/novels/${novelId}/genres/${encodeURIComponent(genreKey)}`, {
      method: "DELETE",
    }),
  setPrimaryNovelGenre: (novelId, genreKey) =>
    apiFetch(`/api/novels/${novelId}/genres/${encodeURIComponent(genreKey)}/primary`, {
      method: "PUT",
    }),

  // ----- Observations (Initiative 1 QA dashboard) -----
  novelObservations: (novelId) => apiFetch(`/api/novels/${novelId}/observations`),
  chapterObservations: (novelId, chapterNum) =>
    apiFetch(`/api/novels/${novelId}/chapters/${chapterNum}/observations`),
  observationsLibrarySummary: () => apiFetch(`/api/observations/library-summary`),
  dismissObservation: (observationId) =>
    apiFetch(`/api/observations/${observationId}/dismiss`, { method: "POST" }),

  // ----- Bookmarks (Initiative 2) -----
  bookmarks: (novelId) => apiFetch(`/api/novels/${novelId}/bookmarks`),
  createBookmark: (novelId, chapterNum, body) =>
    apiFetch(`/api/novels/${novelId}/chapters/${chapterNum}/bookmarks`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteBookmark: (bookmarkId) =>
    apiFetch(`/api/bookmarks/${bookmarkId}`, { method: "DELETE" }),

  // ----- Global glossary (Initiative 3) -----
  globalGlossary: () => apiFetch("/api/glossary/global"),
  createGlobalGlossary: (body) => apiFetch("/api/glossary/global", {
    method: "POST",
    body: JSON.stringify(body),
  }),
  updateGlobalGlossary: (entryId, body) => apiFetch(`/api/glossary/global/${entryId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  }),
  deleteGlobalGlossary: (entryId) =>
    apiFetch(`/api/glossary/global/${entryId}`, { method: "DELETE" }),
  globalGlossaryUsage: (entryId) =>
    apiFetch(`/api/glossary/global/${entryId}/usage`),
  promoteToGlobal: (entryId) =>
    apiFetch(`/api/glossary/${entryId}/promote-to-global`, { method: "POST" }),

  // ----- Find / replace (Initiative 4) -----
  findPreview: (body) => apiFetch("/api/find", {
    method: "POST", body: JSON.stringify(body),
  }),
  findReplaceCommit: (token) => apiFetch("/api/replace", {
    method: "POST", body: JSON.stringify({ token }),
  }),
  glossaryApplyInPlace: (entryId, oldEn, newEn) =>
    apiFetch(`/api/glossary/${entryId}/apply-in-place`, {
      method: "POST",
      body: JSON.stringify({ old_en: oldEn, new_en: newEn }),
    }),
  globalGlossaryApplyInPlace: (entryId, oldEn, newEn) =>
    apiFetch(`/api/glossary/global/${entryId}/apply-in-place`, {
      method: "POST",
      body: JSON.stringify({ old_en: oldEn, new_en: newEn }),
    }),

  // ----- Translation memory (Initiative 5) -----
  tmConcordance: (novelId, q, side = "both") =>
    apiFetch(`/api/novels/${novelId}/tm/concordance?q=${encodeURIComponent(q)}&side=${side}`),
  tmInconsistencies: (novelId) =>
    apiFetch(`/api/novels/${novelId}/tm/inconsistencies`),
};
