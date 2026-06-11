/* Novel Overview page (/novel?id=N) — 2026-05-25.
 *
 * Surfaces metadata + genres (primary + secondary tags) + source language
 * + pipeline overrides for one novel. The translator pipeline still reads
 * novels.genre as the single primary; the chip UI here is a thin layer
 * over the /api/novels/{id}/genres endpoints (see commit 1).
 *
 * Depends on globals from sibling scripts (loaded before this one):
 *   - api.*       (api.js)
 *   - escapeHtml  (utils.js)
 *
 * Does NOT depend on any per-page dialog manager — keeps its own toast.
 */

const params = new URLSearchParams(location.search);
const novelId = Number(params.get("id"));

const els = {
  overview: document.getElementById("overview"),
  noIdMsg: document.getElementById("no-id-msg"),
  crumb: document.getElementById("crumb-title"),

  cover: document.getElementById("no-cover"),
  coverPlaceholder: document.getElementById("no-cover-placeholder"),
  title: document.getElementById("no-title"),
  author: document.getElementById("no-author"),
  originalTitle: document.getElementById("no-original-title"),
  progress: document.getElementById("no-progress"),

  readBtn: document.getElementById("no-read-btn"),
  glossaryBtn: document.getElementById("no-glossary-btn"),
  statsBtn: document.getElementById("no-stats-btn"),

  genreChips: document.getElementById("no-genre-chips"),
  addGenreSelect: document.getElementById("no-add-genre-select"),
  addGenreBtn: document.getElementById("no-add-genre-btn"),
  genreStatus: document.getElementById("no-genre-status"),

  sourceLangSelect: document.getElementById("no-source-lang-select"),
  sourceLangStatus: document.getElementById("no-source-lang-status"),

  translator: document.getElementById("no-translator"),
  refinement: document.getElementById("no-refinement"),
  customBrief: document.getElementById("no-custom-brief"),
  pipelineSave: document.getElementById("no-pipeline-save"),
  pipelineStatus: document.getElementById("no-pipeline-status"),

  titleIn: document.getElementById("no-title-in"),
  authorIn: document.getElementById("no-author-in"),
  originalTitleIn: document.getElementById("no-original-title-in"),
  statusIn: document.getElementById("no-status-in"),
  seriesNameIn: document.getElementById("no-series-name-in"),
  seriesIndexIn: document.getElementById("no-series-index-in"),
  synopsisIn: document.getElementById("no-synopsis-in"),
  metaSave: document.getElementById("no-meta-save"),
  metaStatus: document.getElementById("no-meta-status"),

  archiveBtn: document.getElementById("no-archive-btn"),
  archiveStatus: document.getElementById("no-archive-status"),
  archiveDlg: document.getElementById("archive-confirm"),
  archiveDlgBody: document.getElementById("archive-confirm-body"),

  statChapters: document.getElementById("no-stat-chapters"),
  statDone: document.getElementById("no-stat-done"),
  statQueued: document.getElementById("no-stat-queued"),
  statGlossary: document.getElementById("no-stat-glossary"),
  glossaryLink: document.getElementById("no-glossary-link"),
};

let _genreCatalog = []; // [{key,name,description}] from /api/genres

function flash(target, msg, kind = "ok") {
  if (!target) { showToast(msg, kind); return; }
  target.textContent = msg;
  target.dataset.kind = kind;
  setTimeout(() => { if (target.textContent === msg) target.textContent = ""; }, 4000);
}

// showToast is window.showToast from utils.js (audit 6.6).

if (!novelId) {
  els.noIdMsg.hidden = false;
} else {
  els.overview.hidden = false;
  loadAll();
}

/* ---- Loaders ---- */
async function loadAll() {
  try {
    const [novel, genresPayload, providers, novelGenres, glossary] = await Promise.all([
      api.novel(novelId),
      api.genres(),
      api.providers(),
      api.novelGenres(novelId),
      api.glossary(novelId).catch(() => []),
    ]);
    _genreCatalog = (genresPayload && genresPayload.genres) || [];
    renderHeader(novel);
    populateGenreCatalog();
    renderGenreChips(novelGenres);
    renderSourceLang(novel.source_language);
    populatePipeline(providers, novel);
    populateMetadata(novel);
    renderStats(novel, glossary);
    // Persist as last-opened novel so spine.js reader/glossary nav resolves here.
    try { localStorage.setItem("ink:lastNovel", String(novelId)); } catch (_) {}
  } catch (err) {
    showToast(`Failed to load novel: ${err.message}`, "err");
  }
}

function renderHeader(novel) {
  els.crumb.textContent = novel.title;
  document.title = `${novel.title} · LN Translator`;
  els.title.textContent = novel.title;
  els.author.textContent = novel.author ? `by ${novel.author}` : "Author unknown";
  els.originalTitle.textContent = novel.original_title ? `· ${novel.original_title}` : "";

  const done = novel.done_chapters || 0;
  const total = novel.total_chapters || 0;
  els.progress.textContent = total
    ? `${done} / ${total} chapters translated`
    : "No chapters yet. Append from the import page.";

  // Cover: cache-busted by cover_image_path so a re-upload shows up.
  if (novel.cover_image_path) {
    els.cover.style.backgroundImage =
      `url(/api/novels/${novelId}/cover?t=${encodeURIComponent(novel.cover_image_path)})`;
    if (els.coverPlaceholder) els.coverPlaceholder.style.display = "none";
  } else {
    els.cover.style.backgroundImage = "";
    if (els.coverPlaceholder) {
      els.coverPlaceholder.textContent = firstCJK(novel.title);
      els.coverPlaceholder.style.display = "";
    }
  }

  // CTAs.
  const firstCh = novel.first_chapter_num || 1;
  els.readBtn.href = `/reader?novel=${novelId}&ch=${firstCh}`;
  els.glossaryBtn.href = `/glossary?novel=${novelId}`;
  els.glossaryLink.href = `/glossary?novel=${novelId}`;
  els.statsBtn.href = `/stats?novel=${novelId}`;
}

function firstCJK(s) {
  const m = String(s || "").match(/[㐀-鿿]/);
  return m ? m[0] : (String(s || "").trim()[0] || "書");
}

/* ---- Genres ---- */
function populateGenreCatalog() {
  // The Add-genre select shows every registry key. The render step filters
  // out keys that are already on the novel.
  els.addGenreSelect.innerHTML =
    `<option value="">Pick a genre…</option>` +
    _genreCatalog.map(g =>
      `<option value="${escapeHtml(g.key)}">${escapeHtml(g.name)}</option>`,
    ).join("");
}

function _genreName(key) {
  const hit = _genreCatalog.find(g => g.key === key);
  return hit ? hit.name : key;
}

function renderGenreChips(ng) {
  const primary = ng.primary;
  const secondary = ng.secondary || [];

  const chips = [];
  if (primary) {
    chips.push(`
      <span class="no-chip is-primary" data-genre="${escapeHtml(primary)}">
        <span class="no-chip-mark">★ primary</span>
        ${escapeHtml(_genreName(primary))}
      </span>
    `);
  }
  for (const k of secondary) {
    chips.push(`
      <span class="no-chip" data-genre="${escapeHtml(k)}">
        ${escapeHtml(_genreName(k))}
        <button type="button" class="no-chip-action" data-act="make-primary" data-genre="${escapeHtml(k)}" title="Make primary">↑</button>
        <button type="button" class="no-chip-action" data-act="remove" data-genre="${escapeHtml(k)}" title="Remove">×</button>
      </span>
    `);
  }
  if (!chips.length) {
    chips.push(`<span class="muted">No genre yet. Pick one below.</span>`);
  }
  els.genreChips.innerHTML = chips.join("");

  // Update the Add-genre options so already-present keys are hidden.
  const inUse = new Set([primary, ...secondary].filter(Boolean));
  Array.from(els.addGenreSelect.options).forEach(opt => {
    if (!opt.value) return;
    opt.hidden = inUse.has(opt.value);
  });
}

els.genreChips.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  const key = btn.dataset.genre;
  try {
    let ng;
    if (act === "make-primary") {
      ng = await api.setPrimaryNovelGenre(novelId, key);
      flash(els.genreStatus, `Primary genre is now ${_genreName(key)}.`);
    } else if (act === "remove") {
      ng = await api.removeNovelGenre(novelId, key);
      flash(els.genreStatus, `Removed ${_genreName(key)}.`);
    } else {
      return;
    }
    renderGenreChips(ng);
  } catch (err) {
    flash(els.genreStatus, err.message, "err");
  }
});

els.addGenreBtn.addEventListener("click", async () => {
  const key = els.addGenreSelect.value;
  if (!key) {
    flash(els.genreStatus, "Pick a genre first.", "err");
    return;
  }
  try {
    const ng = await api.addNovelGenre(novelId, key, false);
    flash(els.genreStatus, `Added ${_genreName(key)}.`);
    renderGenreChips(ng);
    els.addGenreSelect.value = "";
  } catch (err) {
    flash(els.genreStatus, err.message, "err");
  }
});

/* ---- Source language ---- */
function renderSourceLang(lang) {
  els.sourceLangSelect.value = lang || "zh";
}

els.sourceLangSelect.addEventListener("change", async () => {
  const v = els.sourceLangSelect.value;
  try {
    await api.updateNovel(novelId, { source_language: v });
    flash(els.sourceLangStatus, `Updated to ${v}.`);
  } catch (err) {
    flash(els.sourceLangStatus, err.message, "err");
  }
});

/* ---- Pipeline ---- */
function populatePipeline(providers, novel) {
  const defaultName = (providers.find(p => p.is_default) || {}).name;
  els.translator.innerHTML =
    `<option value="">Default${defaultName ? ` (${escapeHtml(defaultName)})` : ""}</option>` +
    providers.map(p =>
      `<option value="${p.id}">${escapeHtml(p.name)}</option>`,
    ).join("");
  els.translator.value = novel.translator_provider_id != null
    ? String(novel.translator_provider_id) : "";

  els.refinement.innerHTML =
    `<option value="">Off</option>` +
    providers.map(p =>
      `<option value="${p.id}">${escapeHtml(p.name)}</option>`,
    ).join("");
  els.refinement.value = novel.refinement_provider_id != null
    ? String(novel.refinement_provider_id) : "";

  els.customBrief.value = novel.custom_style_brief || "";
}

els.pipelineSave.addEventListener("click", async () => {
  const payload = {
    translator_provider_id: els.translator.value
      ? Number(els.translator.value) : null,
    refinement_provider_id: els.refinement.value
      ? Number(els.refinement.value) : null,
    custom_style_brief: els.customBrief.value.trim() || null,
  };
  try {
    await api.updateNovel(novelId, payload);
    flash(els.pipelineStatus, "Saved.");
  } catch (err) {
    flash(els.pipelineStatus, err.message, "err");
  }
});

/* ---- Metadata ---- */
function populateMetadata(novel) {
  els.titleIn.value = novel.title || "";
  els.authorIn.value = novel.author || "";
  els.originalTitleIn.value = novel.original_title || "";
  els.statusIn.value = novel.status || "";
  els.seriesNameIn.value = novel.series_name || "";
  els.seriesIndexIn.value =
    novel.series_index != null ? String(novel.series_index) : "";
  els.synopsisIn.value = novel.synopsis || "";
}

els.metaSave.addEventListener("click", async () => {
  const titleRaw = els.titleIn.value.trim();
  if (!titleRaw) {
    flash(els.metaStatus, "Title cannot be blank.", "err");
    els.titleIn.focus();
    return;
  }
  const idxRaw = els.seriesIndexIn.value.trim();
  const payload = {
    title: titleRaw,
    author: els.authorIn.value.trim() || null,
    original_title: els.originalTitleIn.value.trim() || null,
    status: els.statusIn.value || null,
    series_name: els.seriesNameIn.value.trim() || null,
    series_index: idxRaw ? Number(idxRaw) : null,
    synopsis: els.synopsisIn.value.trim() || null,
  };
  try {
    const updated = await api.updateNovel(novelId, payload);
    flash(els.metaStatus, "Saved.");
    renderHeader({ ...updated, total_chapters: 0, done_chapters: 0 });
    // Re-load full novel to refresh chapter counts (PATCH response
    // doesn't include aggregates).
    api.novel(novelId).then(n => renderHeader(n)).catch(() => {});
  } catch (err) {
    flash(els.metaStatus, err.message, "err");
  }
});

/* ---- Archive ---- */
// Replaces the old library-card "Archive" button. Soft-deletes the novel
// (`DELETE /api/novels/{id}` — the same endpoint, just relocated to the
// per-novel page) and redirects back to the library where it will appear
// under the Archive tab. Uses a local <dialog> for the confirmation so
// the novel-overview page doesn't need to import the library's confirm
// helper.
els.archiveBtn.addEventListener("click", async () => {
  let counts = null;
  try { counts = await api.deleteCounts(novelId); }
  catch { counts = null; }
  const lines = [];
  if (counts) {
    if (counts.chapters) lines.push(`${counts.chapters} chapter${counts.chapters === 1 ? "" : "s"}`);
    if (counts.glossary_entries) lines.push(`${counts.glossary_entries} glossary ${counts.glossary_entries === 1 ? "entry" : "entries"}`);
    if (counts.bookmarks) lines.push(`${counts.bookmarks} bookmark${counts.bookmarks === 1 ? "" : "s"}`);
  }
  const breakdown = lines.length
    ? `<p>This will archive: <strong>${lines.join(" + ")}</strong>.</p>`
    : "";
  els.archiveDlgBody.innerHTML =
    `<p>Archive <strong>${escapeHtml(els.title.textContent || "this novel")}</strong>?</p>` +
    breakdown +
    `<p class="muted">The novel moves to the Archive tab for 30 days. Restorable from there. Permanent deletion requires a second Purge action.</p>`;
  const ok = await new Promise(resolve => {
    const okBtn = els.archiveDlg.querySelector('[data-act="ok"]');
    const cancelBtn = els.archiveDlg.querySelector('[data-act="cancel"]');
    const cleanup = () => {
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      els.archiveDlg.removeEventListener("cancel", onCancelEvt);
    };
    const onOk = () => { cleanup(); els.archiveDlg.close(); resolve(true); };
    const onCancel = () => { cleanup(); els.archiveDlg.close(); resolve(false); };
    const onCancelEvt = () => { cleanup(); resolve(false); };
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    els.archiveDlg.addEventListener("cancel", onCancelEvt);
    els.archiveDlg.showModal();
  });
  if (!ok) return;
  els.archiveBtn.disabled = true;
  try {
    await api.deleteNovel(novelId);
    flash(els.archiveStatus, "Archived. Redirecting…");
    setTimeout(() => { location.href = "/library"; }, 600);
  } catch (err) {
    els.archiveBtn.disabled = false;
    flash(els.archiveStatus, `Archive failed: ${err.message}`, "err");
  }
});

/* ---- Stats summary ---- */
function renderStats(novel, glossary) {
  els.statChapters.textContent = novel.total_chapters || 0;
  els.statDone.textContent = novel.done_chapters || 0;
  els.statQueued.textContent = novel.queue_chapters || novel.translate_queue || 0;
  els.statGlossary.textContent = Array.isArray(glossary) ? glossary.length : "…";
}
