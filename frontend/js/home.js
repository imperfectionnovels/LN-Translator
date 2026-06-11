const params = new URLSearchParams(location.search);
const appendNovelId = params.get("novel");
const appendMode = appendNovelId !== null && appendNovelId !== "";

// Notify any open reader tab for the same novel that new chapters landed,
// so it can refresh its TOC + chapter count without waiting for the 6s
// background poll. Safe to fail silently if BroadcastChannel is unsupported.
function broadcastNovelChange(novel_id) {
  try {
    const bc = new BroadcastChannel("novel-changes");
    bc.postMessage({ novel_id: Number(novel_id), type: "appended" });
    bc.close();
  } catch (_) { /* ignore */ }
}

// Mirror backend/routes/translate.py MAX_BULK_FILES so a user dragging more
// than the cap onto the bulk-upload zone gets an immediate, descriptive error
// instead of the network round-trip-then-400 path.
const MAX_BULK_FILES = 10000;

// H2: real WAI-ARIA tab pattern. The tab buttons carry role="tab" +
// aria-selected + aria-controls in the HTML; here we sync state on click,
// route arrow keys / Home / End between tabs, and hide non-active panels.
const tabs = document.getElementById("import-tabs");
const tabButtons = Array.from(tabs.querySelectorAll('button[role="tab"]'));
function activateTab(target) {
  tabButtons.forEach(b => {
    const isActive = b === target;
    b.classList.toggle("on", isActive);
    b.setAttribute("aria-selected", isActive ? "true" : "false");
    b.tabIndex = isActive ? 0 : -1;
  });
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.add("hidden"));
  document.getElementById(`panel-${target.dataset.tab}`).classList.remove("hidden");
}
tabButtons.forEach(t => t.addEventListener("click", () => activateTab(t)));
tabs.addEventListener("keydown", (e) => {
  const i = tabButtons.indexOf(document.activeElement);
  if (i < 0) return;
  let next = null;
  if (e.key === "ArrowRight" || e.key === "ArrowDown") next = tabButtons[(i + 1) % tabButtons.length];
  else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = tabButtons[(i - 1 + tabButtons.length) % tabButtons.length];
  else if (e.key === "Home") next = tabButtons[0];
  else if (e.key === "End") next = tabButtons[tabButtons.length - 1];
  if (!next) return;
  e.preventDefault();
  activateTab(next);
  next.focus();
});

const statusEl = document.getElementById("status");
function showStatus(msg, kind = "info") {
  statusEl.className = `status ${kind}`;
  statusEl.textContent = msg;
}

// Genre picker — populated on load from /api/genres. Each tab's panel
// has its own <select.import-genre> (genre-paste / genre-url / genre-upload
// / genre-bulk); the user MUST pick one before submitting (2026-05-26:
// no default; the import button is disabled until a real genre is
// selected). This forces a deliberate choice instead of accidentally
// landing novels under the wrong genre.
function _genreValueFor(tab) {
  const el = document.getElementById(`genre-${tab}`);
  const v = el?.value?.trim() || "";
  return v || null;
}

// Map of tab → submit-button id. Each tab's submit is enabled only when
// the corresponding genre select carries a non-empty value.
const _IMPORT_TABS = [
  { tab: "paste", btn: "submit-paste" },
  { tab: "url", btn: "submit-url" },
  { tab: "upload", btn: "submit-upload" },
  { tab: "bulk", btn: "submit-bulk" },
];

function _refreshImportButtonStates() {
  for (const { tab, btn } of _IMPORT_TABS) {
    const sel = document.getElementById(`genre-${tab}`);
    const buttonEl = document.getElementById(btn);
    if (!buttonEl) continue;
    // Append mode reuses the existing novel's genre, so initAppendMode() hides
    // the genre picker. The genre gate must NOT apply there: a hidden, empty
    // <select> would otherwise keep the submit button disabled forever, so
    // clicking "Append chapters" silently does nothing. Create mode still
    // requires a deliberate genre pick.
    const ok = appendMode || (sel ? (sel.value || "").trim() !== "" : false);
    // Don't override an already-disabled "in flight" lock. The submit
    // handlers call lockSubmit() during the import and we mustn't
    // re-enable mid-flight just because the user clicks the dropdown.
    if (!buttonEl.dataset.locked) {
      buttonEl.disabled = !ok;
      buttonEl.title = ok ? "" : "Pick a genre first";
    }
  }
}

let _genreRetryTimer = null;
async function populateGenreSelects(attempt = 0) {
  let payload;
  try {
    payload = await api.genres();
  } catch (e) {
    // Backend may still be booting inside the EXE; the race self-resolves,
    // so retry with backoff and offer a manual retry.
    _refreshImportButtonStates();
    statusEl.className = "status err";
    statusEl.innerHTML =
      `Couldn't load the genre list${attempt >= 5 ? `: ${escapeHtml(e.message)}` : " (the app may still be starting). Retrying."} ` +
      `<button type="button" class="btn-ghost" id="genres-retry-btn">Retry now</button>`;
    document.getElementById("genres-retry-btn")?.addEventListener("click", () => {
      if (_genreRetryTimer) { clearTimeout(_genreRetryTimer); _genreRetryTimer = null; }
      populateGenreSelects(0);
    });
    if (attempt < 5) {
      _genreRetryTimer = setTimeout(() => populateGenreSelects(attempt + 1),
        Math.min(8000, 1000 * 2 ** attempt));
    }
    return;
  }
  if (statusEl.textContent.startsWith("Couldn't load the genre list")) {
    statusEl.className = "status";
    statusEl.textContent = "";
  }
  const genres = (payload && payload.genres) || [];
  // The placeholder option carries no value - the user must explicitly
  // pick one of the 10 real options before the submit button enables.
  const opts =
    `<option value="" disabled selected>Pick a genre…</option>` +
    genres.map(g =>
      `<option value="${escapeHtml(g.key)}">${escapeHtml(g.name)}</option>`,
    ).join("");
  document.querySelectorAll(".import-genre").forEach(sel => {
    sel.innerHTML = opts;
    sel.addEventListener("change", _refreshImportButtonStates);
  });
  _refreshImportButtonStates();
}
populateGenreSelects();

/* ---- F05/F06/F08: import preview gate ----
 * Submit handlers call previewThenImport(previewArg, doImport).
 * previewArg is either a string (for paste) or a FormData (for file).
 * doImport is the function to run on user confirm. We render the
 * preview result inside the existing import-preview-dialog and only
 * run doImport when the user clicks "Looks right — import".
 *
 * Append mode (?novel=N) and the URL-tab path SKIP the preview gate
 * because (a) append-mode users have already committed to the novel,
 * and (b) URL scrape doesn't have a parse-then-commit shape — the
 * scraper writes immediately, and its own differentiated error UI
 * covers the worst failure modes.
 */
const importPreviewDlg = document.getElementById("import-preview-dialog");
const importPreviewSummary = document.getElementById("import-preview-summary");
const importPreviewFallback = document.getElementById("import-preview-fallback-warning");
const importPreviewHeadings = document.getElementById("import-preview-headings");
const importPreviewSnippet = document.getElementById("import-preview-snippet");
const importPreviewConfirm = document.getElementById("import-preview-confirm");

function previewThenImport(previewArg, doImport) {
  return new Promise(async (resolve) => {
    let preview;
    try {
      preview = await api.importPreview(previewArg);
    } catch (err) {
      // If the preview endpoint itself fails, skip the gate and run
      // the import — the actual upload will surface the same error.
      // Better to not block the user on a preview failure.
      doImport().then(resolve).catch(() => resolve(null));
      return;
    }

    const n = preview.detected_chapters || 0;
    const formatLabel =
      preview.format_path === "epub_spine" ? " (via EPUB spine)"
      : preview.format_path === "docx_headings" ? " (via DOCX heading styles)"
      : preview.format_path === "structured" ? " (structured)"
      : "";
    importPreviewSummary.innerHTML =
      `<div style="font-size:13px;">Detected <strong>${n}</strong> chapter${n === 1 ? "" : "s"}${formatLabel}.</div>`;

    if (n <= 1) {
      importPreviewFallback.hidden = false;
      importPreviewFallback.style.cssText =
        "background: rgba(200, 66, 58, 0.12); border: 1px solid var(--cinnabar, #c8423a); color: var(--cinnabar, #c8423a); padding: 8px 10px; border-radius: 4px; font-size: 13px; margin: 8px 0;";
      importPreviewFallback.textContent = n === 0
        ? "⚠ Couldn't find any chapter markers. Will import as one big chapter. Continue anyway?"
        : "⚠ Only 1 chapter detected. If you expected more, check that 第N章 / Chapter N markers are present; otherwise continue anyway.";
    } else {
      importPreviewFallback.hidden = true;
      importPreviewFallback.removeAttribute("style");
    }

    importPreviewHeadings.innerHTML = (preview.headings || [])
      .map(h => `<li>${escapeHtml(h)}</li>`)
      .join("") || `<li class="muted">(none detected)</li>`;
    importPreviewSnippet.textContent = preview.first_chapter_first_500 || "(empty)";

    const cleanup = () => {
      importPreviewConfirm.removeEventListener("click", onConfirm);
      const cancelBtn = importPreviewDlg.querySelector("[data-act='cancel']");
      if (cancelBtn) cancelBtn.removeEventListener("click", onCancel);
      importPreviewDlg.removeEventListener("cancel", onCancelEvt);
    };
    const onConfirm = async () => {
      cleanup();
      importPreviewDlg.close();
      try {
        const r = await doImport();
        resolve(r);
      } catch {
        resolve(null);
      }
    };
    const onCancel = () => { cleanup(); importPreviewDlg.close(); resolve(null); };
    const onCancelEvt = () => { cleanup(); resolve(null); };

    importPreviewConfirm.addEventListener("click", onConfirm);
    const cancelBtn = importPreviewDlg.querySelector("[data-act='cancel']");
    if (cancelBtn) cancelBtn.addEventListener("click", onCancel);
    importPreviewDlg.addEventListener("cancel", onCancelEvt);
    importPreviewDlg.showModal();
  });
}

// `escapeHtml` lives in frontend/js/utils.js (loaded ahead of this script).

async function initAppendMode() {
  document.querySelectorAll(".title-field").forEach(el => el.classList.add("hidden"));
  document.getElementById("submit-paste").textContent = "Append chapters";
  document.getElementById("submit-upload").textContent = "Upload & append";
  document.getElementById("submit-bulk").textContent = "Append chapters";
  document.getElementById("submit-url").textContent = "Fetch & append";

  const bannerRow = document.getElementById("append-banner-row");
  const banner = document.getElementById("append-banner");
  bannerRow.classList.remove("hidden");
  banner.textContent = "Loading novel…";
  try {
    const novel = await api.novel(appendNovelId);
    document.getElementById("page-title").textContent = `Append: ${novel.title}`;
    document.title = `Add chapters · ${novel.title}`;
    banner.textContent = `Appending to “${novel.title}” (currently ${novel.total_chapters} chapters). A numbered chapter lands at its own number, filling any gap; otherwise it lands at the end.`;
  } catch (e) {
    banner.className = "status err";
    banner.textContent = `Could not load novel: ${e.message}`;
  }
}
if (appendMode) initAppendMode();

/* Disable submit during async work — re-enable only on error
 * (success paths transition into a redirect, so leaving the button
 * disabled until then is correct). */
function lockSubmit(btn) {
  btn.disabled = true;
  btn.dataset.locked = "1";
}
function unlockSubmit(btn) {
  delete btn.dataset.locked;
  btn.disabled = false;
  // Re-evaluate genre-gating in case unlock happens before the user
  // changes the dropdown (e.g. after a failed import).
  if (typeof _refreshImportButtonStates === "function") {
    _refreshImportButtonStates();
  }
}

/* ---- Paste tab ---- */
const pasteText = document.getElementById("text-paste");
const pasteStats = document.getElementById("paste-stats");
function updatePasteStats() {
  const text = pasteText.value;
  if (!text.trim()) { pasteStats.textContent = ""; return; }
  // Mirror backend/services/parser.py CHAPTER_PATTERNS so the count the user
  // sees on the import page lines up with what the backend will actually
  // produce. The chapter-unit class is [章回节] only — volume / part dividers
  // (第N卷 / 第N篇 / 第N部 / 第N集) are stripped by parser.py's _VOLUME_RE and
  // do NOT count as chapters. Including them used to overcount any novel
  // with a volume divider near the start.
  const cjkChapterRe = /第\s*[\d零〇一二三四五六七八九十百千万两]+\s*[章回节]/g;
  const enChapterRe = /\b(?:Chapter|CH)\s*\d+/gi;
  const prologueRe = /(?:^|\n)\s*(?:楔子|序章|序言|前言|引子|番外)/g;
  const matches = [
    ...(text.match(cjkChapterRe) || []),
    ...(text.match(enChapterRe) || []),
    ...(text.match(prologueRe) || []),
  ];
  // Mirror backend/services/parser.py: ZERO markers triggers _chunk_fallback,
  // which splits at ~CHUNK_SIZE (4000 chars). ONE or more markers is enough
  // for marker-based splitting (the old threshold of >= 2 understated the
  // chapter count for a single-chapter paste). Single-marker fallback would
  // estimate from text length, which mismatches the backend behaviour.
  const CHUNK_SIZE = 4000;
  const chs = matches.length >= 1
    ? matches.length
    : Math.max(1, Math.ceil(text.length / CHUNK_SIZE));
  pasteStats.textContent = `${chs} chapter${chs === 1 ? "" : "s"} detected · ~${text.length.toLocaleString()} chars`;
}
pasteText.addEventListener("input", updatePasteStats);
updatePasteStats();

document.getElementById("submit-paste").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const text = pasteText.value.trim();
  if (!text) return showStatus("Text required.", "err");
  if (appendMode) {
    lockSubmit(btn);
    showStatus("Parsing and importing new chapters…", "info");
    try {
      const r = await api.appendPaste(appendNovelId, text);
      showStatus(`Imported ${r.added_chapters} chapter(s). Opening reader. Click Translate on a chapter to queue it.`, "ok");
      broadcastNovelChange(appendNovelId);
      renderRecent();
      const goto = r.first_new_chapter || 1;
      setTimeout(() => { location.href = `/reader?novel=${appendNovelId}&ch=${goto}`; }, 600);
    } catch (err) {
      showStatus(`Failed: ${err.message}`, "err");
      unlockSubmit(btn);
    }
    return;
  }
  const title = document.getElementById("title-paste").value.trim();
  if (!title) return showStatus("Title required.", "err");
  lockSubmit(btn);
  // F05 preview gate: show what the importer will commit BEFORE writing.
  // doImport runs only after the user confirms inside the dialog.
  showStatus("Detecting chapters…", "info");
  let r;
  try {
    r = await previewThenImport(
      text,
      async () => {
        showStatus("Parsing and importing chapters…", "info");
        return api.paste(title, text, _genreValueFor("paste"));
      },
    );
  } catch (err) {
    showStatus(`Failed: ${err.message}`, "err");
    unlockSubmit(btn);
    return;
  }
  if (!r) {
    // User cancelled at preview, OR preview-then-import surfaced no result.
    showStatus("", "info");
    unlockSubmit(btn);
    return;
  }
  showStatus("Imported. Opening reader. Click Translate on a chapter to queue it.", "ok");
  renderRecent();
  const ch = r.first_chapter || 1;
  setTimeout(() => { location.href = `/reader?novel=${r.novel_id}&ch=${ch}`; }, 600);
});

/* ---- URL tab (Phase 5) ---- */
document.getElementById("submit-url").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const url = document.getElementById("text-url").value.trim();
  if (!url) return showStatus("URL required.", "err");
  if (!/^https?:\/\//i.test(url)) {
    return showStatus("URL must start with http:// or https://.", "err");
  }
  // Optional cookies (sites where browser-shaped headers alone don't
  // get past Cloudflare). null when empty so the JSON body stays small.
  const cookies = (document.getElementById("cookies-url")?.value || "").trim() || null;
  if (appendMode) {
    lockSubmit(btn);
    showStatus("Fetching page and importing new chapters…", "info");
    try {
      const r = await api.scrape(url, null, appendNovelId, cookies);
      showStatus(`Imported ${r.added_chapters} chapter(s) from ${r.scraped_url}.`, "ok");
      broadcastNovelChange(appendNovelId);
      renderRecent();
      const goto = r.first_new_chapter || 1;
      setTimeout(() => { location.href = `/reader?novel=${appendNovelId}&ch=${goto}`; }, 600);
    } catch (err) {
      showStatus(`Failed: ${err.message}`, "err");
      unlockSubmit(btn);
    }
    return;
  }
  // Create-mode: title is optional. The backend uses the page's <title>
  // when blank.
  const title = document.getElementById("title-url").value.trim() || null;
  lockSubmit(btn);
  showStatus("Starting import…", "info");
  try {
    const r = await api.scrape(url, title, null, cookies, _genreValueFor("url"));
    if (r.background && r.job_id) {
      // Recipe URL — backgrounded. Hand off to the polling tracker so
      // the user can navigate away without aborting the job.
      _trackScrapeJob(r.job_id, btn);
      return;
    }
    // Generic URL — blocking finished inline.
    const successMsg = r.recipe
      ? `Imported “${r.scraped_title || "(untitled)"}” · ${r.added_chapters} chapters crawled. Opening reader.`
      : `Imported as “${r.scraped_title || "(untitled)"}”. Opening reader. Click Translate on a chapter to queue it.`;
    showStatus(successMsg, "ok");
    renderRecent();
    const ch = r.first_chapter || 1;
    setTimeout(() => { location.href = `/reader?novel=${r.novel_id}&ch=${ch}`; }, 600);
  } catch (err) {
    // F06: differentiated error rendering by error_kind. Each kind maps
    // to a specific recovery affordance rather than a generic "Failed:"
    // toast. Falls back to the generic message when error_kind is absent.
    _renderScrapeError(err);
    unlockSubmit(btn);
  }
});

/* Track a background scrape job — poll every 1.5s, render progress, and
 * navigate to the reader on completion. The poller survives navigation
 * away from /; the job itself runs in the FastAPI worker independently.
 * If the user comes back to / before the job finishes, the next page
 * load picks the job up via localStorage so progress stays visible. */
const _SCRAPE_JOB_LS_KEY = "ln.activeScrapeJob";

function _trackScrapeJob(jobId, btn) {
  // Persist so a reload / quick navigation away and back doesn't lose
  // the user's place in the import.
  try { localStorage.setItem(_SCRAPE_JOB_LS_KEY, String(jobId)); } catch (_) {}

  const statusEl = document.getElementById("status");
  let stopped = false;

  async function tick() {
    if (stopped) return;
    let job;
    try {
      job = await api.scrapeJob(jobId);
    } catch (err) {
      _renderProgress(statusEl, {
        status: "error",
        message: `Could not read job status: ${err && err.message ? err.message : err}`,
      });
      stopped = true;
      try { localStorage.removeItem(_SCRAPE_JOB_LS_KEY); } catch (_) {}
      if (btn) unlockSubmit(btn);
      return;
    }
    _renderProgress(statusEl, job);

    if (job.status === "done") {
      stopped = true;
      try { localStorage.removeItem(_SCRAPE_JOB_LS_KEY); } catch (_) {}
      renderRecent();
      setTimeout(() => {
        if (job.novel_id) location.href = `/reader?novel=${job.novel_id}&ch=1`;
      }, 800);
      return;
    }
    if (job.status === "error") {
      stopped = true;
      try { localStorage.removeItem(_SCRAPE_JOB_LS_KEY); } catch (_) {}
      if (btn) unlockSubmit(btn);
      return;
    }
    setTimeout(tick, 1500);
  }
  tick();
}

function _renderProgress(el, job) {
  if (!el) return;
  if (job.status === "error") {
    el.className = "status err";
    el.innerHTML = `<strong>Import failed.</strong> ${_escape(job.message || job.error_message || "Unknown error.")}`;
    return;
  }
  const titleBit = job.scraped_title
    ? `<strong>${_escape(job.scraped_title)}</strong>`
    : `<em>Fetching novel info…</em>`;
  let stepLabel = "Starting…";
  if (job.step === "fetching_overview") stepLabel = "Reading overview page";
  else if (job.step === "fetching_chapters") stepLabel = job.total
    ? `Chapter ${job.current} of ${job.total}`
    : "Discovering chapter list";
  else if (job.step === "writing") stepLabel = "Saving novel to library";
  const pct = job.total > 0 ? Math.floor((job.current / job.total) * 100) : 0;
  const bar = job.total > 0
    ? `<div class="scrape-bar" aria-hidden="true"><span style="width:${pct}%"></span></div>`
    : "";
  el.className = "status info";
  el.innerHTML = `
    <div class="scrape-progress">
      <div class="scrape-progress-head">${titleBit}</div>
      <div class="scrape-progress-step">${_escape(stepLabel)}${job.total > 0 ? ` · ${pct}%` : ""}</div>
      ${bar}
      <div class="scrape-progress-hint muted">You can navigate away. The import continues in the background.</div>
    </div>
  `;
}

function _escape(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// On home page load, resume tracking any active scrape job so a
// navigation-away-and-back doesn't lose progress.
(function _resumeScrapeJobIfActive() {
  let jobId = null;
  try { jobId = localStorage.getItem(_SCRAPE_JOB_LS_KEY); } catch (_) {}
  if (jobId) {
    const submitBtn = document.getElementById("submit-url");
    if (submitBtn) lockSubmit(submitBtn);
    _trackScrapeJob(Number(jobId), submitBtn);
  }
})();

/* F06: differentiated URL-scrape error UI. Read err.error_kind (set by
 * api._extractError) and render per-cause recovery affordances inline
 * instead of the generic showStatus("Failed: ...") toast. */
function _renderScrapeError(err) {
  const kind = err && err.error_kind ? err.error_kind : "unknown";
  const detail = err && err.message ? err.message : String(err);
  let html = "";
  switch (kind) {
    case "cf_blocked":
      html = (
        `<strong>Cloudflare (or similar bot protection) blocked the request.</strong> ` +
        `Wait a minute and try the import again. The rate limit usually lifts on its own. ` +
        `If it keeps failing, the site may not be importable.`
      );
      break;
    case "auth_required":
      html = (
        `<strong>This URL requires login (HTTP 401).</strong> ` +
        `The scraper can't follow the site's auth flow, so this URL can't be imported.`
      );
      break;
    case "timeout":
      html = (
        `<strong>The site didn't respond in time.</strong> ` +
        `This is often transient. Try again in a minute, or pick a different chapter source.`
      );
      break;
    case "ssrf":
      html = (
        `<strong>The URL resolves to an internal address.</strong> ` +
        `The scraper rejects internal IPs to prevent SSRF. Make sure the URL is a public site.`
      );
      break;
    case "no_content":
      html = (
        `<strong>No article content was found on the page.</strong> ` +
        `The URL may point at a table-of-contents page (not a chapter), or the site ` +
        `renders its content via JavaScript that the scraper can't see. Try a direct ` +
        `chapter URL instead.`
      );
      break;
    case "not_html":
      html = (
        `<strong>That URL isn't an HTML page.</strong> ` +
        `The response wasn't HTML. Possibly a binary download or an image. ` +
        `Provide the URL of the chapter page itself.`
      );
      break;
    case "network":
      html = (
        `<strong>Network error reaching the site.</strong> ${escapeHtml(detail)} ` +
        `Check the URL and confirm the site is reachable in your browser.`
      );
      break;
    case "http_error":
      html = (
        `<strong>The site returned an error.</strong> ${escapeHtml(detail)}`
      );
      break;
    default:
      html = `<strong>Failed:</strong> ${escapeHtml(detail)}`;
  }
  statusEl.className = "status err";
  statusEl.innerHTML = html;
}
const uploadInput = document.getElementById("file-upload");
const uploadName = document.getElementById("upload-name");
const dropUpload = document.getElementById("drop-upload");
document.getElementById("browse-upload").addEventListener("click", () => uploadInput.click());
uploadInput.addEventListener("change", () => {
  const f = uploadInput.files[0];
  uploadName.textContent = f ? `${f.name} · ${(f.size/1024).toFixed(1)} KB` : "";
});
["dragover", "dragenter"].forEach(ev => dropUpload.addEventListener(ev, e => {
  e.preventDefault(); dropUpload.classList.add("over");
}));
["dragleave", "drop"].forEach(ev => dropUpload.addEventListener(ev, () => dropUpload.classList.remove("over")));
dropUpload.addEventListener("drop", e => {
  e.preventDefault();
  const f = e.dataTransfer.files[0];
  if (!f) return;
  const dt = new DataTransfer(); dt.items.add(f); uploadInput.files = dt.files;
  uploadName.textContent = `${f.name} · ${(f.size/1024).toFixed(1)} KB`;
});

document.getElementById("submit-upload").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const file = uploadInput.files[0];
  if (!file) return showStatus("Choose a .txt file.", "err");
  if (appendMode) {
    lockSubmit(btn);
    showStatus(`Uploading and parsing (${file.name})…`, "info");
    try {
      const r = await api.appendUpload(appendNovelId, file);
      showStatus(`Imported ${r.added_chapters} chapter(s) (encoding: ${r.detected_encoding}). Opening reader. Click Translate on a chapter to queue it.`, "ok");
      broadcastNovelChange(appendNovelId);
      renderRecent();
      const goto = r.first_new_chapter || 1;
      setTimeout(() => { location.href = `/reader?novel=${appendNovelId}&ch=${goto}`; }, 800);
    } catch (err) {
      showStatus(`Failed: ${err.message}`, "err");
      unlockSubmit(btn);
    }
    return;
  }
  const title = document.getElementById("title-upload").value.trim();
  if (!title) return showStatus("Title required.", "err");
  lockSubmit(btn);
  showStatus(`Detecting chapters in ${file.name}…`, "info");
  // F07 preview gate via the same single-file path. Send the file once
  // for preview; on confirm send it again to the real upload route.
  const previewFd = new FormData();
  previewFd.append("file", file);
  let r;
  try {
    r = await previewThenImport(
      previewFd,
      async () => {
        showStatus(`Uploading and parsing (${file.name})…`, "info");
        return api.upload(title, file, _genreValueFor("upload"));
      },
    );
  } catch (err) {
    showStatus(`Failed: ${err.message}`, "err");
    unlockSubmit(btn);
    return;
  }
  if (!r) {
    showStatus("", "info");
    unlockSubmit(btn);
    return;
  }
  let msg = `Imported (${r.source_type || "txt"}). `;
  if (r.detected_encoding && r.source_type === "txt") {
    msg = `Detected encoding: ${r.detected_encoding}. Imported. `;
  }
  if (r.cover_extracted) {
    msg += "Cover image extracted. ";
  }
  msg += "Open the reader and click Translate on a chapter to queue it.";
  showStatus(msg, "ok");
  renderRecent();
  const ch = r.first_chapter || 1;
  setTimeout(() => { location.href = `/reader?novel=${r.novel_id}&ch=${ch}`; }, 800);
});

/* ---- Bulk tab + drag/drop ---- */
const bulkInput = document.getElementById("file-bulk");
const bulkList = document.getElementById("bulk-file-list");
const dropBulk = document.getElementById("drop-bulk");
const bulkSkipReport = document.getElementById("bulk-skip-report");

// Render the per-file skip report for a bulk import response. Returns true when
// any file was skipped (so the caller suppresses auto-navigation and lets the
// user read which files were dropped and why). Built with DOM nodes, not
// innerHTML, so a hostile filename can't inject markup. navHref/navLabel give
// the user a manual way forward since we no longer auto-navigate on skips.
function renderSkipReport(r, navHref, navLabel) {
  const total = (r.skipped_files || 0) + (r.skipped_nonchapter || 0);
  if (!bulkSkipReport) return total > 0;
  bulkSkipReport.textContent = "";
  if (total === 0) { bulkSkipReport.hidden = true; return false; }

  const head = document.createElement("p");
  head.className = "skip-report-head";
  head.textContent = `${total} file(s) skipped, not imported:`;
  bulkSkipReport.appendChild(head);

  const ul = document.createElement("ul");
  const details = Array.isArray(r.skipped_details) ? r.skipped_details : [];
  if (details.length) {
    for (const d of details) {
      const li = document.createElement("li");
      const name = document.createElement("strong");
      name.textContent = d.name || "(unnamed file)";
      li.appendChild(name);
      li.appendChild(document.createTextNode(`: ${d.reason || "skipped"}`));
      ul.appendChild(li);
    }
  } else {
    // Older backend without per-file detail: show the counts so the panel is
    // still informative rather than empty.
    const li = document.createElement("li");
    li.textContent =
      `${r.skipped_files || 0} empty, ${r.skipped_nonchapter || 0} non-chapter`;
    ul.appendChild(li);
  }
  bulkSkipReport.appendChild(ul);

  if (navHref) {
    const a = document.createElement("a");
    a.href = navHref;
    a.className = "skip-report-go";
    a.textContent = navLabel || "Continue";
    bulkSkipReport.appendChild(a);
  }
  bulkSkipReport.hidden = false;
  return true;
}
document.getElementById("browse-bulk").addEventListener("click", () => bulkInput.click());

function renderBulkList() {
  const files = Array.from(bulkInput.files || []);
  if (files.length === 0) { bulkList.innerHTML = ""; return; }
  bulkList.innerHTML = files.map((f, i) => {
    const kb = (f.size / 1024).toFixed(1);
    const t = f.name.replace(/\.txt$/i, "");
    return `<li>${i + 1}. ${escapeHtml(t)} <span class="muted">(${kb} KB)</span></li>`;
  }).join("");
}
bulkInput.addEventListener("change", renderBulkList);
["dragover", "dragenter"].forEach(ev => dropBulk.addEventListener(ev, e => {
  e.preventDefault(); dropBulk.classList.add("over");
}));
["dragleave", "drop"].forEach(ev => dropBulk.addEventListener(ev, () => dropBulk.classList.remove("over")));
dropBulk.addEventListener("drop", e => {
  e.preventDefault();
  const dt = new DataTransfer();
  for (const f of e.dataTransfer.files) dt.items.add(f);
  bulkInput.files = dt.files;
  renderBulkList();
});

document.getElementById("submit-bulk").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const files = Array.from(bulkInput.files || []);
  if (files.length === 0) return showStatus("Choose at least one .txt file.", "err");
  if (files.length > MAX_BULK_FILES) {
    return showStatus(
      `Too many files (${files.length.toLocaleString()}). The bulk upload is capped at ${MAX_BULK_FILES.toLocaleString()} files per request.`,
      "err",
    );
  }
  if (appendMode) {
    lockSubmit(btn);
    showStatus(`Uploading ${files.length} file(s)…`, "info");
    try {
      const r = await api.appendBulk(appendNovelId, files);
      const goto = r.first_new_chapter || 1;
      const href = `/reader?novel=${appendNovelId}&ch=${goto}`;
      const hadSkips = renderSkipReport(r, href, "Open reader");
      const total = (r.skipped_files || 0) + (r.skipped_nonchapter || 0);
      const skipMsg = total ? ` (${total} file(s) skipped, see below)` : "";
      const tail = hadSkips ? "" : " Opening reader. Click Translate on a chapter to queue it.";
      showStatus(`Imported ${r.added_chapters} raw chapter(s)${skipMsg}.${tail}`, "ok");
      broadcastNovelChange(appendNovelId);
      renderRecent();
      if (hadSkips) { unlockSubmit(btn); }
      else { setTimeout(() => { location.href = href; }, 800); }
    } catch (err) {
      showStatus(`Failed: ${err.message}`, "err");
      unlockSubmit(btn);
    }
    return;
  }
  const title = document.getElementById("title-bulk").value.trim();
  if (!title) return showStatus("Title required.", "err");
  lockSubmit(btn);
  showStatus(`Uploading ${files.length} file(s)…`, "info");
  try {
    const r = await api.bulkUpload(title, files, _genreValueFor("bulk"));
    const hadSkips = renderSkipReport(r, "/library", "Go to Library");
    const total = (r.skipped_files || 0) + (r.skipped_nonchapter || 0);
    const skipMsg = total ? ` (${total} file(s) skipped, see below)` : "";
    const tail = hadSkips ? "" : " Open the reader and click Translate on a chapter to queue it.";
    showStatus(`Imported ${r.added_chapters} raw chapter(s)${skipMsg}.${tail}`, "ok");
    renderRecent();
    if (hadSkips) { unlockSubmit(btn); }
    else { setTimeout(() => { location.href = `/library`; }, 800); }
  } catch (err) {
    showStatus(`Failed: ${err.message}`, "err");
    unlockSubmit(btn);
  }
});

/* ---- Recent novels (right-rail) ---- */
async function renderRecent() {
  const target = document.getElementById("recent-list");
  try {
    const novels = await api.novels();
    target.setAttribute("aria-busy", "false");
    if (!novels.length) {
      target.innerHTML = '<div class="muted" style="padding: 8px 0;">No novels yet.</div>';
      return;
    }
    target.innerHTML = novels.slice(0, 6).map(n => {
      const pct = n.total_chapters ? Math.round((n.done_chapters / n.total_chapters) * 100) : 0;
      const st = n.done_chapters >= n.total_chapters && n.total_chapters > 0 ? "done"
        : n.done_chapters > 0 ? `${pct}%` : "raw";
      const stClass = st === "done" ? "" : st === "raw" ? "warn" : "";
      // Prefer the durable DB position (survives a WebView2 storage wipe and is
      // present even on a session that never wrote the local breadcrumb); fall
      // back to the localStorage breadcrumb. `!= null` matches both null and
      // undefined — the same DB-first pattern library.js's readInfoFor uses.
      let resumeCh = 1;
      if (n.last_read_chapter_num != null) {
        resumeCh = n.last_read_chapter_num;
      } else {
        try {
          const raw = localStorage.getItem(`lastRead:${n.id}`);
          if (raw) {
            const parsed = JSON.parse(raw);
            if (parsed && Number.isFinite(parsed.ch)) resumeCh = parsed.ch;
          }
        } catch { /* ignore corrupted localStorage */ }
      }
      return `<div class="recent-row">
        <span class="id">${n.id}</span>
        <span class="ti"><a href="/reader?novel=${n.id}&ch=${resumeCh}">${escapeHtml(n.title)}</a></span>
        <span class="st ${stClass}">${st}</span>
      </div>`;
    }).join("");
  } catch (e) {
    target.innerHTML = `<div class="status err">${escapeHtml(e.message)}</div>`;
  }
}
renderRecent();
