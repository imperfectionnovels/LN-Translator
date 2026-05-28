const params = new URLSearchParams(location.search);
const novelId = parseInt(params.get("novel"), 10);
let currentCh = parseInt(params.get("ch") || "1", 10);

// --- DOM HANDLES ---
// All `const X = document.getElementById(...)` lookups live in THIS block.
// Module-top-level wiring later in the file (addEventListener calls,
// apply*() invocations) assumes these are initialized at script-parse
// time. Declaring a DOM-handle const mid-file = TDZ ReferenceError at
// boot (and `?.` doesn't save you — TDZ fires on the binding lookup,
// before the optional-chain short-circuit runs). This convention is
// enforced by backend/tests/test_reader_js_boot_safety.py.
// R6: #novel-title was a hidden <h1> that never rendered. The actual
// title lives in the TOC head, the masthead crumbs, and document.title.
const stage = document.getElementById("reader-stage");
const statusEl = document.getElementById("chapter-status");
const tocNovelName = document.getElementById("toc-novel-name");
const tocNovelMeta = document.getElementById("toc-novel-meta");
const tocList = document.getElementById("toc-list");
const tocSearch = document.getElementById("toc-search-input");
const tocFootStats = document.getElementById("toc-foot-stats");
const tocToggle = document.getElementById("toc-toggle");
const mobileTocToggle = document.getElementById("mobile-toc-toggle");
const tocBackdrop = document.getElementById("toc-backdrop");
const chIdEl = document.getElementById("ch-id");
const chTitleEl = document.getElementById("ch-title");
const chTitleZhEl = document.getElementById("ch-title-zh");
const chH1En = document.getElementById("ch-h1-en");
const chH1ZhSub = document.getElementById("ch-h1-zh-sub");
const chH1Zh = document.getElementById("ch-h1-zh");

// Strip "Chapter N:" / "Ch. N -" (English) and "第N章 ·" / "第N章 " (Chinese)
// prefixes from chapter titles. The masthead shows the chapter index in
// its mono dateline (#masthead-index), so repeating it in the title would
// be the "Chapter 1: Chapter 1 of N" stacked-redundancy the audit caught.
// Backend's normalize_title_en still stores the canonical "Chapter N: ..."
// form; this strip is render-only.
function stripChapterPrefix(t, isZh) {
  if (!t) return t;
  if (isZh) return t.replace(/^第\s*\d+\s*章\s*[·:：\-—\s]*/u, "").trim();
  return t.replace(/^(?:chapter|ch\.?)\s*\d+\s*[:：\-—.·]?\s*/i, "").trim();
}
const bodyEn = document.getElementById("body-en");
const bodyZh = document.getElementById("body-zh");
const paneZh = document.getElementById("pane-zh");
const paneEnLabel = document.getElementById("pane-en-label");
const prevBtn = document.getElementById("prev-ch");
const nextBtn = document.getElementById("next-ch");
const toggleDual = document.getElementById("toggle-dual");
const toggleSource = document.getElementById("toggle-source");
const retranslateBtn = document.getElementById("retranslate");

// Event delegation for banner buttons that get re-created on every chapter
// load (the status banner's innerHTML rewrite + the glossary-merge-error
// card insertion). Attaching a fresh addEventListener inside those render
// paths leaks one closure per navigation. Delegate from stable ancestors
// instead, so the listener is registered exactly once.
statusEl.addEventListener("click", (e) => {
  if (e.target.closest(".status-dismiss")) {
    statusEl.hidden = true;
    return;
  }
  if (e.target.closest("#stale-retranslate")) {
    retranslateBtn?.click();
  }
});
// Glossary-merge-error card is inserted as a sibling of bodyEn (see
// applyGlossaryMergeBanner). Delegating from bodyEn.parentElement covers
// every successive re-render without leaking.
bodyEn.parentElement?.addEventListener("click", (e) => {
  if (e.target.closest("#glossary-merge-retry")) {
    retranslateBtn?.click();
  }
});
const copyChapterBtn = document.getElementById("copy-chapter");
const rereadBtn = document.getElementById("reread");
const nextCard = document.getElementById("next-card");
const nextTitle = document.getElementById("next-title");
const nextStatus = document.getElementById("next-status");
const nextGo = document.getElementById("next-go");
const endStat = document.getElementById("end-stat");
const endBlock = document.getElementById("end-block");
const readPct = document.getElementById("read-pct");
const readEta = document.getElementById("read-eta");
const glossaryLink = document.getElementById("glossary-link");

glossaryLink.href = `/glossary?novel=${novelId}`;
// Mobile-only cross-page nav inside the TOC drawer head. Mirrors the spine's
// glossary link with the current novel context preserved. Null-guarded so
// older cached HTML without the new toc-cross-nav row doesn't crash boot.
const tocGlossaryLink = document.getElementById("toc-glossary-link");
if (tocGlossaryLink) tocGlossaryLink.href = `/glossary?novel=${novelId}`;
document.getElementById("download-txt").href = `/api/novels/${novelId}/download?format=txt`;
document.getElementById("download-md").href = `/api/novels/${novelId}/download?format=md`;
// Initiative 7 EPUB export. Null-guard for cached old HTML.
const downloadEpub = document.getElementById("download-epub");
if (downloadEpub) downloadEpub.href = `/api/novels/${novelId}/download?format=epub`;
// Legacy "polished/raw" download links may not exist after reader.html cleanup;
// null-guard the assignment so a cached HTML doesn't crash boot.
const dlRawTxt = document.getElementById("download-txt-raw");
if (dlRawTxt) dlRawTxt.remove();
const dlRawMd = document.getElementById("download-md-raw");
if (dlRawMd) dlRawMd.remove();
// Optional: only present after the reader.html append-chapters edit lands.
// Null-guarded so a cached-old-HTML + fresh-JS combo doesn't crash boot.
const appendChaptersLink = document.getElementById("append-chapters");
if (appendChaptersLink) appendChaptersLink.href = `/?novel=${novelId}`;

// Two-way view mode: "english" (Classic, default) and "bilingual" (ZH + EN
// side-by-side). Persisted to localStorage scoped per-novel. Storage values
// intentionally keep "english" / "bilingual" — renaming would invalidate
// every user's stored preference. UI label maps "english" → "Classic".
// (Focus mode was removed 2026-05-26; the standalone "Focus mode" chrome
// toggle in the type-settings dialog still works independently.)
const VIEW_MODE_KEY = `viewMode_${novelId}`;
const VALID_MODES = ["english", "bilingual"];
let viewMode = localStorage.getItem(VIEW_MODE_KEY)
            || localStorage.getItem("viewMode")
            || "english";
if (!VALID_MODES.includes(viewMode)) viewMode = "english";
let dualMode = viewMode === "bilingual";

// 2026-05-27: per-reader-session pick between the polished translation
// (translated_text / refined_text) and the mechanical NMT free draft
// (free_draft_text) when a chapter has both. Per-novel scope mirrors
// VIEW_MODE_KEY so "I'm comparing on novel A" doesn't leak into novel B.
// Legacy-fallback: scoped key, then unscoped key, then "polished". The
// toggle is visibility-gated by applyTranslationSource so the stored
// preference is preserved even on chapters where only one body exists.
const TRANSLATION_SOURCE_KEY = `translationSource_${novelId}`;
const VALID_SOURCES = ["polished", "free_draft"];
let translationSource = localStorage.getItem(TRANSLATION_SOURCE_KEY)
                     || localStorage.getItem("translationSource")
                     || "polished";
if (!VALID_SOURCES.includes(translationSource)) translationSource = "polished";
// Hoisted to the top of the script: applyDual() (called below) accesses
// lastChapter on the seal-glyph branch. The original declaration sat
// hundreds of lines down, putting it in the temporal dead zone at
// module-load time and throwing ReferenceError before the boot IIFE
// could even start. Keep this near the top so any later top-level
// statement can read it safely.
let lastChapter = null;
applyDual();

let chaptersCache = [];
let glossaryCache = [];
let novelMeta = null;
// Providers cache for the bilingual pane label + refinement badge. Lazily
// populated by loadProviders(); falls back to null IDs if the call fails.
let _providersCache = null;
// Single re-poll handle for loadChapter. Replaces the prior two separate
// pollTimer / loaderPollTimer variables — those carried two bugs:
//   1. Neither was cleared on chapter navigation. The setTimeout closure
//      captures `num`, so a navigation from chapter A to B left A's timer
//      live; when it fired, loadChapter(A) ran on top of B and snapped
//      the URL back via history.replaceState.
//   2. They were independent, so a status transition that scheduled one
//      while the other was already pending produced double-polls.
// _cancelPoll() is called at the top of every loadChapter, inside
// stopLoader, and before scheduling any new poll — so at most one timer
// is ever in flight and it always points at the currently-active chapter.
let pollHandle = null;
function _cancelPoll() {
  if (pollHandle) { clearTimeout(pollHandle); pollHandle = null; }
}

// Consecutive 404 count, keyed on chapter_num. The old 404 branch
// re-polled every 3s indefinitely (backoff capped at 30s); a typoed
// ?ch=999 URL or a deleted novel would hammer the server forever.
// Cap at _NOT_FOUND_MAX retries then surface a definitive "not found"
// UI with a back-to-library link.
const _notFoundCount = new Map();
const _NOT_FOUND_MAX = 10;
function _resetNotFoundCount(num) {
  _notFoundCount.delete(num);
}

/* ---- Loading-screen state (translation) ---- */
const chapterLoader = document.getElementById("chapter-loader");
const loaderLabel   = document.getElementById("chapter-loader-label");
const loaderElapsed = document.getElementById("chapter-loader-elapsed");

// IDs hidden when rendering the chapter "queued — waiting" card, so only the
// waiting message shows (not the prior chapter's body or end matter).
const BODY_CHROME_HIDE_IDS = ["ch-h1-en", "ch-h1-zh-sub", "body-en", "end-block"];

const stageStarts = new Map();   // `${chapterNum}:${stage}` → performance.now()
// Chapters where the user clicked "Start translation" but the backend has
// not yet flipped the row from pending → translating. Without this, the
// post-click 1.2s poll can land on a still-pending response and the pending
// branch re-renders the CTA (looking like the action "didn't take") because
// the backend background task hadn't claimed the row yet. Value is the
// timestamp of the click so a stale hint can expire if the backend never
// confirms (e.g. the user cancels the queue immediately after queueing).
const awaitingQueueStart = new Map(); // chapterNum → performance.now()
const AWAITING_QUEUE_TTL_MS = 8_000;

// Bulk-uploaded chapters get title_zh = the filename stem. When the file is
// named like 0283.txt that produces "0283", which isn't a meaningful subtitle
// next to "Ch. 283" — suppress purely-numeric stems from display.
function displayTitleZh(s) {
  if (!s) return "";
  return /^\s*\d+\s*$/.test(s) ? "" : s;
}

// Sync the chapter-bar id / title / zh-subtitle for one chapter row.
// When title_en is missing we fall back to the raw 中文 title, which already
// contains 第N章 — so showing the "Ch. N" chip beside it prints the number
// twice. Hide the chip in that case; keep it when the English title is
// either present or fully-synthetic (no Chinese fallback either).
function setChapterBarTitle(num, titleEn, titleZh) {
  const zhDisplay = stripChapterPrefix(displayTitleZh(titleZh), true);
  if (titleEn) {
    chIdEl.textContent = `Ch. ${num}`;
    chIdEl.classList.remove("hidden");
    chTitleEl.textContent = stripChapterPrefix(titleEn);
    chTitleZhEl.textContent = zhDisplay;
  } else if (zhDisplay) {
    chIdEl.classList.add("hidden");
    chTitleEl.textContent = zhDisplay;
    chTitleZhEl.textContent = "";
  } else {
    chIdEl.textContent = `Ch. ${num}`;
    chIdEl.classList.remove("hidden");
    chTitleEl.textContent = `Chapter ${num}`;
    chTitleZhEl.textContent = "";
  }
}

// Per-chapter polling start time so the loader can grow its interval if the
// backend has been stuck on the same row for minutes (a Gemini timeout, a
// hung Claude CLI, etc.). Backing off cuts polling traffic by ~10x for a
// chapter that has been "translating" for 5 min instead of 5s.
const pollStarts = new Map(); // chapterNum → performance.now()
function pollInterval(num, baseMs) {
  const start = pollStarts.get(num);
  if (!start) {
    pollStarts.set(num, performance.now());
    return baseMs;
  }
  const elapsed = performance.now() - start;
  // After 2 min stuck on the same chapter, scale the interval up. Linear,
  // capped at 30s — keeps the UI responsive once state finally changes.
  if (elapsed < 120_000) return baseMs;
  const scaled = Math.min(30_000, baseMs * (1 + (elapsed - 120_000) / 60_000));
  return Math.round(scaled);
}
function clearPollStart(num) { pollStarts.delete(num); }
function expireAwaitingQueueStart(num) {
  const t = awaitingQueueStart.get(num);
  if (t !== undefined && performance.now() - t > AWAITING_QUEUE_TTL_MS) {
    awaitingQueueStart.delete(num);
  }
}
// B3: chapters the user queued and then navigated away from used to stay in
// awaitingQueueStart until they happened to revisit. Scan periodically and
// purge anything past its TTL so the map stays bounded.
setInterval(() => {
  const now = performance.now();
  for (const [num, t] of awaitingQueueStart) {
    if (now - t > AWAITING_QUEUE_TTL_MS) awaitingQueueStart.delete(num);
  }
}, 30_000);
let rafHandle = null;
let activeLoader = null; // { chapterNum, stage, eta, t0 }

// `escapeHtml` lives in frontend/js/utils.js (loaded ahead of this script).

// FTS snippets come back from the backend with literal `<mark>...</mark>`
// strings wrapping the matched terms (see routes/chapters.py: snippet(...,
// '<mark>', '</mark>', ...)). Escaping the whole snippet defends against any
// HTML the LLM may have emitted into translated_text; we then re-promote the
// two escaped tag forms back to real tags so the highlighting still renders.
function highlightSnippet(raw) {
  return escapeHtml(raw || "")
    .replace(/&lt;mark&gt;/g, "<mark>")
    .replace(/&lt;\/mark&gt;/g, "</mark>");
}

// confirmDialog lives in frontend/js/utils.js (C7).
function firstCJK(s) {
  const m = String(s || "").match(/[㐀-鿿]/);
  return m ? m[0] : "";
}

// Segmented controls show the *current* state, not the next-action label.
// The .on class on the active segment, plus aria-pressed, drive the visual
// + a11y indication so users can read the mode at a glance.
//
// `applyDual` is the legacy name kept so all the existing call sites in this
// file still work; it now routes the two-way Classic/Bilingual state machine.

function applyDual() {
  dualMode = viewMode === "bilingual";
  stage.dataset.dual = dualMode ? "on" : "off";
  stage.dataset.viewMode = viewMode;
  // In bilingual the ZH pane shows; Classic collapses to single-column.
  paneZh.classList.toggle("hidden", !dualMode);
  paneEnLabel.classList.toggle("hidden", !dualMode);
  toggleDual.querySelectorAll("button").forEach(b => {
    const active = b.dataset.mode === viewMode;
    b.classList.toggle("on", active);
    b.setAttribute("aria-pressed", active ? "true" : "false");
  });
}
toggleDual.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-mode]");
  if (!btn) return;
  const next = btn.dataset.mode;
  if (!VALID_MODES.includes(next)) return;
  if (next === viewMode) return;
  viewMode = next;
  localStorage.setItem(VIEW_MODE_KEY, viewMode);
  applyDual();
  if (lastChapter) renderChapterBody(lastChapter);
});

// 2026-05-27: translation-source picker. Hidden when the current chapter
// doesn't have BOTH a polished body and a free-draft body — showing the
// toggle on chapters where it's a no-op clutters the bar. The stored
// preference is preserved across navigation; _displayedEnglish's guard
// makes "Free draft selected, but this chapter has no free_draft_text"
// fall through to the polished fallback chain.
function applyTranslationSource(ch) {
  if (!toggleSource) return;
  const polishedAvailable = !!(ch && (ch.translated_text || ch.refined_text));
  const freeDraftAvailable = !!(ch && ch.free_draft_text);
  const bothExist = polishedAvailable && freeDraftAvailable;
  toggleSource.hidden = !bothExist;
  stage.dataset.translationSource = translationSource;
  toggleSource.querySelectorAll("button[data-source]").forEach(b => {
    const active = b.dataset.source === translationSource;
    b.classList.toggle("on", active);
    b.setAttribute("aria-pressed", active ? "true" : "false");
  });
}
toggleSource?.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-source]");
  if (!btn) return;
  if (btn.disabled) return;  // edit mode locks the picker to Polished.
  const next = btn.dataset.source;
  if (!VALID_SOURCES.includes(next)) return;
  if (next === translationSource) return;
  translationSource = next;
  localStorage.setItem(TRANSLATION_SOURCE_KEY, translationSource);
  applyTranslationSource(lastChapter);
  if (lastChapter) renderChapterBody(lastChapter);
});

/* ---- Read/Edit mode toggle (2026-05-25) ----
 *
 * The reader has two modes: 'read' (clean reading experience) and 'edit'
 * (translator's workbench with all editing/observability tooling visible).
 * Single source of truth: body[data-reader-mode]. Edit-only chrome is
 * scoped via .edit-only CSS class — read mode hides it via display:none.
 *
 * Edit mode forces viewMode='bilingual' so the source is visible while
 * editing. The view-mode picker is disabled in edit mode. Flipping back
 * to read restores the user's previously-selected viewMode.
 *
 * Persisted globally to localStorage.readerMode. Default = 'read'.
 */
const READER_MODE_KEY = "readerMode";
const READER_MODE_VIEW_KEY = "readerMode_savedViewMode";  // stash on flip-to-edit
const READER_MODE_SOURCE_KEY = "readerMode_savedTranslationSource";  // 2026-05-27
let readerMode = localStorage.getItem(READER_MODE_KEY) === "edit" ? "edit" : "read";
const readerModeToggle = document.getElementById("reader-mode-toggle");
const viewModePicker = document.getElementById("toggle-dual");

function _applyReaderMode() {
  document.body.dataset.readerMode = readerMode;
  // Sync the toggle buttons' pressed state.
  if (readerModeToggle) {
    readerModeToggle.querySelectorAll("button[data-reader-mode]").forEach(b => {
      b.setAttribute("aria-pressed", b.dataset.readerMode === readerMode ? "true" : "false");
    });
  }
  // Edit mode: disable view-mode picker (forced bilingual for source visibility).
  if (viewModePicker) {
    viewModePicker.querySelectorAll("button").forEach(b => {
      b.disabled = (readerMode === "edit");
      if (readerMode === "edit") {
        b.title = "Edit mode requires the source visible. Switch to Read to choose a view mode.";
      } else {
        b.removeAttribute("title");
      }
    });
  }
  // 2026-05-27: edit mode also locks translationSource to "polished" — the
  // paragraph-edit machinery (_captureParagraphMeta) computes indices against
  // translated_text / refined_text, so editing while the free draft is on
  // screen would splice user edits against the wrong chunks. Disable the
  // source picker to make the constraint visible.
  if (toggleSource) {
    toggleSource.querySelectorAll("button").forEach(b => {
      b.disabled = (readerMode === "edit");
      if (readerMode === "edit") {
        b.title = "Edit mode operates on the polished translation. Switch to Read to view the free draft.";
      } else {
        b.removeAttribute("title");
      }
    });
  }
}

function setReaderMode(next) {
  if (next !== "read" && next !== "edit") return;
  if (next === readerMode) return;
  let viewModeChanged = false;
  let sourceChanged = false;
  if (next === "edit") {
    // Stash the current viewMode so we can restore on flip back to read.
    localStorage.setItem(READER_MODE_VIEW_KEY, viewMode);
    if (viewMode !== "bilingual") {
      viewMode = "bilingual";
      localStorage.setItem(VIEW_MODE_KEY, viewMode);
      applyDual();
      viewModeChanged = true;
    }
    // 2026-05-27: same stash-and-force pattern for translationSource. The
    // paragraph-edit machinery only knows about translated_text /
    // refined_text columns; force to "polished" so edits land on the
    // visible body. Stash the prior choice so flipping back to Read
    // restores it.
    localStorage.setItem(READER_MODE_SOURCE_KEY, translationSource);
    if (translationSource !== "polished") {
      translationSource = "polished";
      localStorage.setItem(TRANSLATION_SOURCE_KEY, translationSource);
      sourceChanged = true;
    }
  } else {
    // Restore the prior viewMode if we stashed one; otherwise leave as-is.
    const saved = localStorage.getItem(READER_MODE_VIEW_KEY);
    if (saved && VALID_MODES.includes(saved) && saved !== viewMode) {
      viewMode = saved;
      localStorage.setItem(VIEW_MODE_KEY, viewMode);
      applyDual();
      viewModeChanged = true;
    }
    // 2026-05-27: restore stashed translationSource on exit-edit, symmetric
    // with the viewMode restore above.
    const savedSource = localStorage.getItem(READER_MODE_SOURCE_KEY);
    if (savedSource && VALID_SOURCES.includes(savedSource) && savedSource !== translationSource) {
      translationSource = savedSource;
      localStorage.setItem(TRANSLATION_SOURCE_KEY, translationSource);
      sourceChanged = true;
    }
  }
  readerMode = next;
  localStorage.setItem(READER_MODE_KEY, readerMode);
  _applyReaderMode();
  // Re-render the chapter body unconditionally — even if viewMode didn't
  // flip, the term-highlight gate depends on readerMode, so the rendered
  // DOM differs between read and edit. sourceChanged ALSO forces a
  // re-render because _displayedEnglish picks a different body — and that
  // matters even when viewModeChanged is also true (applyDual only flips
  // CSS, not body innerHTML).
  if (lastChapter && (!viewModeChanged || sourceChanged)) {
    renderChapterBody(lastChapter);
  }
}

readerModeToggle?.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-reader-mode]");
  if (!btn) return;
  setReaderMode(btn.dataset.readerMode);
  // Reveal the discoverability hint the first time the user flips into
  // Edit mode (and only the first time — sticky dismissal lives in
  // localStorage). The hint is managed purely via [hidden] so we don't
  // have to fight .edit-only's `display: revert` cascade.
  if (btn.dataset.readerMode === "edit") {
    _maybeShowEditModeHint();
  } else {
    _hideEditModeHint();
  }
});

// Apply on load before any chapter renders.
_applyReaderMode();
if (readerMode === "edit" && viewMode !== "bilingual") {
  viewMode = "bilingual";
  applyDual();
}
// 2026-05-27: same boot-time constraint for translationSource — edit mode
// requires the polished body to be the one on screen so paragraph edits
// land on the right column.
if (readerMode === "edit" && translationSource !== "polished") {
  localStorage.setItem(READER_MODE_SOURCE_KEY, translationSource);
  translationSource = "polished";
  localStorage.setItem(TRANSLATION_SOURCE_KEY, translationSource);
}

/* ---- Edit-mode discoverability ----
 * The select-to-add-glossary popover already works in Edit mode without
 * entering Edit-paragraphs (which only flips paragraphs to contenteditable
 * for style learning). But that's invisible until you happen to drag-
 * select something. The hint banner + the always-visible "+ Term" button
 * make the action discoverable. */
const EDIT_HINT_SEEN_KEY = "editModeHintSeen";
const editModeHint = document.getElementById("edit-mode-hint");
const editModeHintDismiss = document.getElementById("edit-mode-hint-dismiss");
const addTermBtn = document.getElementById("add-term-btn");

function _maybeShowEditModeHint() {
  if (!editModeHint) return;
  if (localStorage.getItem(EDIT_HINT_SEEN_KEY) === "1") return;
  editModeHint.hidden = false;
}
function _hideEditModeHint() {
  if (editModeHint) editModeHint.hidden = true;
}
editModeHintDismiss?.addEventListener("click", () => {
  localStorage.setItem(EDIT_HINT_SEEN_KEY, "1");
  if (editModeHint) editModeHint.hidden = true;
});
// Standalone "+ Term" button: opens the Add form blank (no preselected
// text, no source-paragraph context). Useful when the user wants to add
// a term they remember without having to find and select it in the body.
addTermBtn?.addEventListener("click", () => {
  showAddForm("", false, null, null);
});
// Boot-time: if the user already lives in Edit mode and hasn't seen the
// hint yet, show it on this load too.
if (readerMode === "edit") _maybeShowEditModeHint();

tocToggle.addEventListener("click", () => {
  const on = stage.dataset.toc === "on";
  stage.dataset.toc = on ? "off" : "on";
  tocToggle.textContent = on ? "show rail" : "hide rail";
});

/* ---- Mobile TOC drawer ----
 * At or below TOC_DRAWER_BREAKPOINT the rail becomes an off-canvas drawer
 * (see reader.css for the matching "TOC_DRAWER_BREAKPOINT" comment marker —
 * the two values MUST stay in sync). HTML defaults data-toc="on" so the
 * desktop rail is visible at load — on mobile that would slide the drawer
 * in over the reading column. Match the matchMedia at the breakpoint and
 * force "off" on narrow screens at boot. */
const TOC_DRAWER_BREAKPOINT = 900;
const mobileMql = window.matchMedia(`(max-width: ${TOC_DRAWER_BREAKPOINT}px)`);
function setMobileDefault() {
  if (mobileMql.matches) {
    stage.dataset.toc = "off";
    tocToggle.textContent = "show rail";
  }
}
setMobileDefault();
mobileMql.addEventListener("change", setMobileDefault);
// R7: boot.js pre-painted the mobile drawer in the closed position via
// html[data-toc-init]. Now that JS owns the data-toc state, drop the boot
// flag so subsequent user-triggered "open" actions aren't suppressed by
// the pre-paint override in reader.css.
document.documentElement.removeAttribute("data-toc-init");

if (mobileTocToggle) {
  mobileTocToggle.addEventListener("click", () => {
    stage.dataset.toc = "on";
    // Keep the in-rail toggle's label in sync so tapping it inside the drawer
    // reads as a close action ("hide rail") rather than re-opening.
    tocToggle.textContent = "hide rail";
  });
}
if (tocBackdrop) {
  tocBackdrop.addEventListener("click", () => {
    stage.dataset.toc = "off";
    tocToggle.textContent = "show rail";
  });
}
// R4: auto-close the drawer when the user taps a chapter row. Was capture-
// phase, which fired before the row's stopPropagation could shield the small
// "×" toc-cancel button — tapping the queue-remove × closed the drawer
// alongside cancelling the chapter. Now runs in the bubble phase with an
// explicit guard on .toc-cancel targets; the cancel button still does its
// own stopPropagation, but we no longer race it.
tocList.addEventListener("click", (e) => {
  if (!mobileMql.matches) return;
  if (e.target.closest(".toc-cancel")) return;
  if (e.target.closest(".toc-row")) {
    stage.dataset.toc = "off";
    tocToggle.textContent = "show rail";
  }
});

/* ---- TOC rendering ---- */
// Status glyphs (color-blind safe + redundancy of color). Active row keeps no
// glyph since it gets the seal-mark treatment via CSS.
function statusGlyph(state) {
  return state === "done" ? "✓"
       : state === "translating" ? "◐"
       : state === "queued" ? "⏳"
       : state === "error" ? "!"
       : state === "stale" ? "↻"
       : "·";
}
function chapterState(c) {
  // Single-pass pipeline: translator state is the only state. Queued is
  // distinct from pending so the user can see queue depth in the TOC.
  if (c.status === "error") return "error";
  if (c.status === "translating") return "translating";
  if (c.status === "done") return "done";
  if (c.translate_queued) return "queued";
  if (c.status === "stale") return "stale";
  return "pending";
}
let _ftsHits = null; // {q: lastQuery, matches: [...]}

// `chaptersCache` is loaded once at page open and never refreshed
// automatically, so the TOC glyphs (queued, translating, done, stale, …)
// drift out of date during a long session: chapters move through the queue,
// glossary edits in another tab flip rows to 'stale', etc. Whenever we
// re-fetch a single chapter we compare it to the cached entry and only
// re-pull the whole list when something visible to the TOC actually moved.
// One-navigation-behind staleness in exchange for a single small GET.
// loadChapters() (defined below) is the unconditional refresh; callers that
// already know state moved (retranslate/cancel) should use it.
async function refreshTocIfStale(ch) {
  if (!ch) return;
  const cached = chaptersCache.find(c => c.chapter_num === ch.chapter_num);
  const drift = !cached || (
    cached.status !== ch.status ||
    Boolean(cached.translate_queued) !== Boolean(ch.translate_queued)
  );
  if (!drift) return;
  try { await loadChapters(); }
  catch (_) { /* transient failure — keep the stale cache */ }
}

function renderToc() {
  const q = (tocSearch.value || "").toLowerCase();
  const filtered = chaptersCache.filter(c => {
    if (!q) return true;
    const t = (c.title_en || c.title_zh || "").toLowerCase();
    return t.includes(q) || String(c.chapter_num).includes(q);
  });
  tocList.setAttribute("aria-busy", "false");
  if (filtered.length === 0) {
    // No title hit — surface a "Search body" CTA that runs the FTS query.
    // Cached on `_ftsHits` so repeat keystrokes that produce the same query
    // don't re-fire the request.
    if (_ftsHits && _ftsHits.q === q && _ftsHits.matches.length > 0) {
      tocList.innerHTML = _ftsHits.matches.map(m => `
        <div class="toc-row toc-fts-row" data-ch="${m.chapter_num}">
          <span class="ti">${escapeHtml(m.title_en || displayTitleZh(m.title_zh) || "")}</span>
          <div class="toc-snippet">${highlightSnippet(m.snippet)}</div>
        </div>
      `).join("");
      tocList.querySelectorAll(".toc-row").forEach(row => {
        row.addEventListener("click", () => loadChapter(parseInt(row.dataset.ch, 10)));
      });
      return;
    }
    tocList.innerHTML = `
      <div class="muted" style="padding: 12px 14px;">
        No title matches.
        <button type="button" class="btn-ghost" id="toc-fts-search" style="margin-left:6px;">Search body text</button>
      </div>`;
    document.getElementById("toc-fts-search")?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Searching…";
      try {
        const res = await api.searchChapters(novelId, q);
        _ftsHits = { q, matches: res.matches || [] };
        renderToc();
      } catch (err) {
        btn.disabled = false;
        btn.textContent = `Search failed: ${err.message}`;
      }
    });
    return;
  }
  tocList.innerHTML = filtered.map(c => {
    const state = chapterState(c);
    const cls = state !== "pending" ? state : "";
    const title = c.title_en || displayTitleZh(c.title_zh) || `Chapter ${c.chapter_num}`;
    const active = c.chapter_num === currentCh ? " active" : "";
    const aria = `aria-label="Chapter ${c.chapter_num}, ${state}"`;
    // Queued rows get a click-to-cancel × in place of the static ⏳ glyph.
    // chapterState() only returns "queued" for rows that are flagged AND not
    // in-flight, so the × never appears on the chapter currently holding a
    // stage lock (which can't be cancelled mid-LLM-call anyway).
    const trailing = active
      ? ""
      : state === "queued"
        ? `<button class="toc-cancel" data-cancel="${c.chapter_num}" title="Remove from queue" aria-label="Remove chapter ${c.chapter_num} from queue">×</button>`
        : `<span class="st">${statusGlyph(state)}</span>`;
    return `<div class="toc-row ${cls}${active}" data-ch="${c.chapter_num}" ${aria}>
      <span class="ti">${escapeHtml(title)}</span>
      ${trailing}
    </div>`;
  }).join("");
  tocList.querySelectorAll(".toc-row").forEach(row => {
    row.addEventListener("click", (e) => {
      // Don't navigate when the user clicked the row's cancel button.
      if (e.target.closest(".toc-cancel")) return;
      loadChapter(parseInt(row.dataset.ch, 10));
    });
  });
  tocList.querySelectorAll(".toc-cancel").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      cancelOneFromQueue(parseInt(btn.dataset.cancel, 10), btn);
    });
  });
  const total = chaptersCache.length;
  const done = chaptersCache.filter(c => c.status === "done").length;
  const queued = chaptersCache.filter(c => c.translate_queued).length;
  // Surface the queue depth in the TOC footer so the user knows what's
  // outstanding without having to scan the rail. When queued > 0, also offer
  // a one-click "Clear queue" affordance — the bulk DELETE endpoint leaves
  // the single in-flight chapter per stage running (it can't be cancelled
  // mid-LLM-call) but drops every waiting chapter.
  if (queued > 0) {
    tocFootStats.innerHTML =
      `${done} / ${total} translated · ${queued} queued ` +
      `<button type="button" id="toc-clear-queue" class="toc-clear-queue" ` +
      `title="Remove every waiting chapter from the queue (in-flight chapter finishes)">Clear</button>`;
    const clearBtn = document.getElementById("toc-clear-queue");
    if (clearBtn) clearBtn.addEventListener("click", () => cancelAllFromQueue(clearBtn));
  } else {
    tocFootStats.textContent = `${done} / ${total} translated`;
  }
  // Surface the jump-to-pending button only when there's actually a pending
  // chapter to jump to. Status counts as "pending" if it's untranslated or
  // still mid-translate — anything that won't be readable right now.
  const jumpBtn = document.getElementById("toc-jump-pending");
  if (jumpBtn) {
    const firstPending = chaptersCache.find(c => c.status !== "done" && c.status !== "error");
    if (firstPending) {
      jumpBtn.hidden = false;
      jumpBtn.textContent = `Jump to next pending (Ch. ${firstPending.chapter_num}) →`;
      jumpBtn.onclick = () => loadChapter(firstPending.chapter_num);
    } else {
      jumpBtn.hidden = true;
    }
  }
  // Mass-queue button. Visible whenever at least one chapter is queueable
  // (i.e. not currently translating, not already done, and not already
  // waiting in the queue). Errored chapters count too because the dialog's
  // include-errors checkbox lets the user opt them in.
  const massQueueBtn = document.getElementById("toc-mass-queue");
  if (massQueueBtn) {
    const queueable = chaptersCache.some(c =>
      c.status !== "done" && c.status !== "translating" && !c.translate_queued
    );
    massQueueBtn.hidden = !queueable;
  }
  // 2026-05-25: error banner. Counts chapters in `error` status and
  // surfaces a one-click Retry-all action. Replaces the old stale-
  // chapter banner — per user feedback the stale concept stayed
  // server-side but stopped being a sidebar concern. Visible in both
  // Read and Edit modes since a failed translation is a state the
  // reader cares about regardless of mode.
  const errorBanner = document.getElementById("toc-error-banner");
  const errorCount = document.getElementById("toc-error-count");
  if (errorBanner && errorCount) {
    const failed = chaptersCache.filter(c => c.status === "error");
    if (failed.length > 0) {
      errorBanner.hidden = false;
      errorCount.textContent =
        `${failed.length} failed chapter${failed.length === 1 ? "" : "s"}`;
    } else {
      errorBanner.hidden = true;
    }
  }

  const active = tocList.querySelector(".toc-row.active");
  if (active && !tocUserScrolling()) active.scrollIntoView({ block: "nearest" });
}

/* ---- Mass-queue dialog ---- */
// One-click bulk translate. Lets the user queue many chapters from the TOC
// rail in one gesture instead of clicking each chapter's Retranslate. The
// preview count is derived from chaptersCache so we don't pay a round-trip
// before the user commits — chaptersCache is also what /api uses, so the
// preview matches the server's view as long as the page isn't stale.
(function bindMassQueueDialog() {
  const openBtn = document.getElementById("toc-mass-queue");
  const dialog = document.getElementById("mass-queue-dialog");
  if (!openBtn || !dialog) return;
  const rangeBox = dialog.querySelector("#mass-queue-range");
  const fromInput = dialog.querySelector("#mass-queue-from");
  const toInput = dialog.querySelector("#mass-queue-to");
  const includeErrorsCb = dialog.querySelector("#mass-queue-include-errors");
  const preview = dialog.querySelector("#mass-queue-preview");
  const errorEl = dialog.querySelector("#mass-queue-error");
  const submitBtn = dialog.querySelector("#mass-queue-submit");
  const cancelBtn = dialog.querySelector("#mass-queue-cancel");

  function selectedMode() {
    const checked = dialog.querySelector("input[name='mass-queue-mode']:checked");
    return checked ? checked.value : "all_untranslated";
  }

  function clearError() {
    errorEl.style.display = "none";
    errorEl.textContent = "";
  }
  function showError(msg) {
    errorEl.style.display = "";
    errorEl.textContent = msg;
  }

  function refreshPreview() {
    clearError();
    const mode = selectedMode();
    rangeBox.hidden = mode !== "range";
    const includeErrors = includeErrorsCb.checked;
    let candidates = chaptersCache;
    if (mode === "range") {
      const from = parseInt(fromInput.value, 10);
      const to = parseInt(toInput.value, 10);
      if (!Number.isFinite(from) || !Number.isFinite(to)) {
        preview.textContent = "Enter a From and To chapter number to see the count.";
        return;
      }
      if (from > to) {
        preview.textContent = "From chapter must be less than or equal to To.";
        return;
      }
      candidates = chaptersCache.filter(c =>
        c.chapter_num >= from && c.chapter_num <= to
      );
    }
    let willQueue = 0;
    let skipDone = 0, skipInFlight = 0, skipAlreadyQueued = 0, skipError = 0;
    for (const c of candidates) {
      if (c.status === "translating") { skipInFlight++; continue; }
      if (c.status === "done") { skipDone++; continue; }
      if (c.translate_queued) { skipAlreadyQueued++; continue; }
      if (c.status === "error") {
        if (includeErrors) willQueue++;
        else skipError++;
        continue;
      }
      willQueue++;
    }
    const parts = [`${willQueue} chapter${willQueue === 1 ? "" : "s"} will be queued.`];
    const skipBits = [];
    if (skipDone) skipBits.push(`${skipDone} already done`);
    if (skipInFlight) skipBits.push(`${skipInFlight} translating now`);
    if (skipAlreadyQueued) skipBits.push(`${skipAlreadyQueued} already queued`);
    if (skipError) skipBits.push(`${skipError} failed (skipped)`);
    if (skipBits.length) parts.push(`Skipping ${skipBits.join(", ")}.`);
    preview.textContent = parts.join(" ");
    submitBtn.disabled = willQueue === 0;
  }

  function openDialog() {
    clearError();
    // Default range bounds = full chapter span. Saves the user typing on the
    // common case ("queue chapters 50–200") and makes the preview meaningful
    // immediately when the user flips to Range.
    const nums = chaptersCache.map(c => c.chapter_num);
    if (nums.length) {
      fromInput.value = Math.min(...nums);
      toInput.value = Math.max(...nums);
    }
    // Always reset to the default mode on open so a previous Range selection
    // doesn't surprise the user the next time they open the dialog.
    const allRadio = dialog.querySelector("input[name='mass-queue-mode'][value='all_untranslated']");
    if (allRadio) allRadio.checked = true;
    includeErrorsCb.checked = true;
    rangeBox.hidden = true;
    refreshPreview();
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function closeDialog() {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  openBtn.addEventListener("click", openDialog);
  cancelBtn.addEventListener("click", closeDialog);
  dialog.querySelectorAll("input[name='mass-queue-mode']").forEach(r => {
    r.addEventListener("change", refreshPreview);
  });
  fromInput.addEventListener("input", refreshPreview);
  toInput.addEventListener("input", refreshPreview);
  includeErrorsCb.addEventListener("change", refreshPreview);

  submitBtn.addEventListener("click", async () => {
    const mode = selectedMode();
    const body = { mode, include_errors: includeErrorsCb.checked };
    if (mode === "range") {
      const from = parseInt(fromInput.value, 10);
      const to = parseInt(toInput.value, 10);
      if (!Number.isFinite(from) || !Number.isFinite(to) || from > to) {
        showError("Enter a valid From / To range.");
        return;
      }
      body.from_chapter = from;
      body.to_chapter = to;
    }
    submitBtn.disabled = true;
    const oldLabel = submitBtn.textContent;
    submitBtn.textContent = "Queueing…";
    try {
      const res = await api.massQueueChapters(novelId, body);
      closeDialog();
      // Refresh the TOC so the queued glyphs appear immediately.
      if (typeof loadChapters === "function") await loadChapters();
      // Best-effort toast through the existing status pipeline. Falls back to
      // a quiet console line when the toast helper isn't on this page.
      const msg = `Queued ${res.queued_count} chapter${res.queued_count === 1 ? "" : "s"} for translation.`;
      if (typeof window.toast === "function") window.toast(msg);
      else console.info(msg);
    } catch (err) {
      showError(`Queue failed: ${err.message || err}`);
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = oldLabel;
    }
  });
})();

// Retry-all handler for the error banner. Loops through every `error`
// chapter and re-queues it via /retranslate; failures don't abort the
// loop (we want best-effort coverage and the user can see the per-row
// status afterward).
document.getElementById("toc-retry-failed")?.addEventListener("click", async () => {
  const failed = chaptersCache.filter(c => c.status === "error");
  if (failed.length === 0) return;
  const btn = document.getElementById("toc-retry-failed");
  if (btn) { btn.disabled = true; btn.textContent = "Queueing…"; }
  try {
    for (const c of failed) {
      try { await api.retranslate(novelId, c.chapter_num); }
      catch (_) { /* keep going; surface aggregate result */ }
    }
    if (typeof loadChapters === "function") await loadChapters();
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Retry all"; }
  }
});

/* Suppress auto-scroll of the active TOC row when the user is actively
 * scrolling the rail themselves. Resets 500ms after the last scroll event. */
let tocLastScrolledAt = 0;
tocList.addEventListener("scroll", () => { tocLastScrolledAt = Date.now(); }, { passive: true });
function tocUserScrolling() {
  return Date.now() - tocLastScrolledAt < 500;
}
let tocSearchTimer = null;
tocSearch.addEventListener("input", () => {
  // Novels can have thousands of chapters; debounce so typing doesn't tear
  // down and rebuild the entire TOC list on every keystroke.
  if (tocSearchTimer) clearTimeout(tocSearchTimer);
  tocSearchTimer = setTimeout(renderToc, 120);
});

/* ---- Glossary highlighting ---- */
// R8: glossary highlight regex was rebuilt on every renderChapterBody —
// each chapter nav, each toggle, each search. On a 2000-term glossary
// that's two ~2000-alternative unions compiled per render. We key the
// cache on the glossaryCache reference identity; loadGlossary / create /
// update all replace the array (or invalidate explicitly) so a stale
// cache can't outlive the data it was built from.
let _termPatternCache = null;
let _termPatternCacheKey = null;
function invalidateTermPattern() {
  _termPatternCache = null;
  _termPatternCacheKey = null;
}
function buildTermPattern() {
  if (!glossaryCache.length) return null;
  if (_termPatternCacheKey === glossaryCache) return _termPatternCache;
  // Sort longest first so "Master Yan" beats "Yan".
  const escapeRe = s => s.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&");

  const enTerms = glossaryCache
    .filter(g => g.term_en && g.term_en.trim().length >= 2)
    .map(g => g.term_en.trim())
    .sort((a, b) => b.length - a.length);
  // Case-insensitive: a glossary term may be stored lowercase but appear
  // title-cased at a sentence start (and vice versa). The displayed text still
  // uses the prose's own casing — only matching is case-folded.
  const enRe = enTerms.length
    ? new RegExp("\\b(" + enTerms.map(escapeRe).join("|") + ")\\b", "gi")
    : null;

  const zhTerms = glossaryCache
    .filter(g => g.term_zh && g.term_zh.trim())
    .map(g => g.term_zh.trim())
    .sort((a, b) => b.length - a.length);
  const zhRe = zhTerms.length
    ? new RegExp("(" + zhTerms.map(escapeRe).join("|") + ")", "g")
    : null;

  const result = (!enRe && !zhRe) ? null : { en: enRe, zh: zhRe };
  _termPatternCache = result;
  _termPatternCacheKey = glossaryCache;
  return result;
}

function termInfo(text, side /* "en" | "zh" */) {
  const k = side === "en" ? "term_en" : "term_zh";
  const needle = text.trim();
  // EN matching is case-insensitive (mirrors the case-insensitive term regex);
  // ZH stays exact since Chinese has no case.
  if (side === "en") {
    const lc = needle.toLowerCase();
    return glossaryCache.find(g => (g[k] || "").trim().toLowerCase() === lc);
  }
  return glossaryCache.find(g => (g[k] || "").trim() === needle);
}
function termClass(g) {
  if (!g) return "term";
  if (g.category === "technique" || g.category === "item") return "term alt";
  return "term";
}

function renderParagraphsWithTerms(text, side, pattern) {
  const re = pattern && pattern[side];
  const raw = String(text || "");
  // Detect single-newline-paragraph CN raws: when there are no blank-line
  // separators but there ARE internal newlines, treat each non-empty line as
  // its own paragraph. Without this branch, the whole chapter renders as one
  // giant <p> with internal <br>s, while the EN side (rendered via marked
  // with breaks:true) shows paragraph-level breaks — bilingual mode then
  // visibly mismatches.
  let paras;
  if (raw.includes("\n") && !/\n\s*\n/.test(raw)) {
    paras = raw.split(/\n+/).map(p => p.trim()).filter(Boolean);
  } else {
    paras = raw.split(/\n\s*\n/).map(p => p.trim()).filter(Boolean);
  }
  return paras.map(para => {
    const safeBefore = escapeHtml(para).replace(/\n/g, "<br>");
    if (!re) return `<p>${safeBefore}</p>`;
    // Run regex over the raw (unescaped) paragraph so word boundaries work,
    // but render each piece via escapeHtml afterwards.
    const out = [];
    let lastIdx = 0;
    let m;
    re.lastIndex = 0;
    while ((m = re.exec(para))) {
      if (m.index > lastIdx) out.push(escapeHtml(para.slice(lastIdx, m.index)));
      const matched = m[0];
      const g = termInfo(matched, side);
      out.push(`<span class="${termClass(g)}" data-term="${escapeHtml(g ? (side === "en" ? g.term_zh : g.term_en) : matched)}" title="${escapeHtml(g ? `${g.term_zh} ↔ ${g.term_en}${g.category ? " · " + g.category : ""}` : matched)}">${escapeHtml(matched)}</span>`);
      lastIdx = m.index + matched.length;
    }
    if (lastIdx < para.length) out.push(escapeHtml(para.slice(lastIdx)));
    return `<p>${out.join("").replace(/\n/g, "<br>")}</p>`;
  }).join("");
}

function renderEnglishMarkdownWithTerms(text, pattern) {
  // The translator emits Markdown: **bold** around 【】 system
  // blocks, *italics* for first-person present-tense thought / recited text /
  // titles of works, ALL-CAPS for sound effects. Parse with marked, then
  // overlay glossary-term highlighting on the resulting text segments while
  // leaving tag content alone so we don't corrupt <strong>/<em>/<br> markup.
  const re = pattern && pattern.en;
  const src = String(text || "");
  // marked@12 does NOT sanitize raw HTML in its input; the LLM output is
  // untrusted (prompt-injection / passthrough from source can yield <script>),
  // so DOMPurify is mandatory. If either lib failed to load, degrade to the
  // plain escape-and-render path rather than risk XSS.
  if (!window.marked || !window.DOMPurify || !src) {
    return renderParagraphsWithTerms(src, "en", pattern);
  }
  const rawHtml = window.DOMPurify.sanitize(
    window.marked.parse(src, { breaks: true, gfm: true })
  );
  if (!re) return rawHtml;
  return rawHtml.split(/(<[^>]+>)/).map(seg => {
    if (!seg) return "";
    if (seg.startsWith("<")) return seg;
    // seg is HTML-escaped text from marked + sanitized by DOMPurify. Run the
    // term regex on it and wrap matches in glossary spans. For terms
    // containing HTML-special characters this would miss; cultivation
    // glossary terms don't, so it's acceptable.
    const out = [];
    let lastIdx = 0;
    let m;
    re.lastIndex = 0;
    while ((m = re.exec(seg))) {
      if (m.index > lastIdx) out.push(seg.slice(lastIdx, m.index));
      const matched = m[0];
      const g = termInfo(matched, "en");
      out.push(`<span class="${termClass(g)}" data-term="${escapeHtml(g ? g.term_zh : matched)}" title="${escapeHtml(g ? `${g.term_zh} ↔ ${g.term_en}${g.category ? " · " + g.category : ""}` : matched)}">${matched}</span>`);
      lastIdx = m.index + matched.length;
    }
    if (lastIdx < seg.length) out.push(seg.slice(lastIdx));
    return out.join("");
  }).join("");
}

/* ---- Selection popover ---- */
const glossaryMiniDialog = document.getElementById("glossary-mini-dialog");
// Clean up the form reference when the dialog is closed by any means
// (Esc, programmatic close, button) so the next selection doesn't re-attach
// stale state.
glossaryMiniDialog?.addEventListener("close", () => {
  if (formEl) { formEl.remove(); formEl = null; }
  if (popoverEl) { popoverEl.remove(); popoverEl = null; }
  clearSelectedTerms();
});
let popoverEl = null;
let formEl = null;
// Glossary terms in the body are quiet by default (dotted underline only);
// the highlight pill is shown via the .selected class while the term is the
// subject of the selection popover, then cleared when the popover closes.
let selectedTerms = [];
function clearSelectedTerms() {
  for (const el of selectedTerms) el.classList.remove("selected");
  selectedTerms = [];
}
function markSelectedTerms(range) {
  clearSelectedTerms();
  const node = range.commonAncestorContainer;
  const scope = node.nodeType === 1 ? node : node.parentElement;
  if (!scope) return;
  const own = scope.closest ? scope.closest(".term") : null;
  if (own) { own.classList.add("selected"); selectedTerms.push(own); }
  scope.querySelectorAll?.(".term").forEach((el) => {
    if (range.intersectsNode(el) && !el.classList.contains("selected")) {
      el.classList.add("selected");
      selectedTerms.push(el);
    }
  });
}
function clearPopover() {
  if (popoverEl) { popoverEl.remove(); popoverEl = null; }
  if (formEl) { formEl.remove(); formEl = null; }
  clearSelectedTerms();
  if (glossaryMiniDialog && glossaryMiniDialog.open) glossaryMiniDialog.close();
}
function showPopoverForSelection() {
  clearPopover();
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) return;
  const text = sel.toString().trim();
  if (!text || text.length > 60) return;
  const range = sel.getRangeAt(0);
  // Only react if the selection is inside the reader pane.
  if (!bodyEn.contains(range.commonAncestorContainer) && !bodyZh.contains(range.commonAncestorContainer)) return;
  const inZh = bodyZh.contains(range.commonAncestorContainer);
  const rect = range.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return;

  // Pin-and-revise: an exact match on the appropriate side flips the primary
  // action from "add new" to "revise" the existing entry. termInfo already
  // does the case-sensitive trim-equality lookup against glossaryCache.
  const existing = termInfo(text, inZh ? "zh" : "en");

  // Pill any glossary term spans the selection touches while the popover is up.
  markSelectedTerms(range);

  popoverEl = document.createElement("div");
  popoverEl.className = "sel-pop";
  // Wireframes redesign: the .sel-pop is now the unified contextual
  // toolbar — adds 記 bookmark + 尋 concordance alongside the existing
  // glossary + copy actions, so the standalone #concordance-trigger
  // can retire. Each action wires straight into an existing handler:
  //   add → showAddForm           (the same flow the glossary page uses)
  //   revise → showReviseForm
  //   copy → navigator.clipboard
  //   bookmark → opens bookmark-add-dialog with the selection's
  //              paragraph captured (instead of the scroll-top fallback)
  //   concordance → _openConcordanceDialog (TM search)
  // retranslate-paragraph is intentionally NOT here: the backend's
  // retranslate endpoint operates on a chapter, not a paragraph. The
  // chapter-wide ⟲ in the ⋯ menu covers the use case; per-paragraph
  // retranslation can land when there's an endpoint to call.
  const extraActs = `
      <span class="sep"></span>
      <button data-act="bookmark" title="Add a bookmark on the paragraph containing this selection">記 Bookmark</button>
      <span class="sep"></span>
      <button data-act="concordance" title="Search the translation memory for this phrase">尋 Concordance</button>
  `;
  if (existing) {
    popoverEl.innerHTML = `
      <span class="sel-pop-hint">${escapeHtml(existing.term_zh)} ↔ ${escapeHtml(existing.term_en)}</span>
      <span class="sep"></span>
      <button data-act="revise">✎ Revise</button>
      <span class="sep"></span>
      <button data-act="copy">＂ Copy</button>
      ${extraActs}
    `;
  } else {
    popoverEl.innerHTML = `
      <button data-act="add">＋ Add to glossary</button>
      <span class="sep"></span>
      <button data-act="copy">＂ Copy</button>
      ${extraActs}
    `;
  }
  document.body.appendChild(popoverEl);
  // Clamp to viewport on all four sides so selecting near an edge doesn't
  // render the popover off-screen.
  const desiredTop = window.scrollY + rect.top - popoverEl.offsetHeight - 10;
  const desiredLeft = window.scrollX + rect.left + (rect.width / 2) - (popoverEl.offsetWidth / 2);
  const maxLeft = window.scrollX + window.innerWidth - popoverEl.offsetWidth - 8;
  const maxTop  = window.scrollY + window.innerHeight - popoverEl.offsetHeight - 8;
  popoverEl.style.top = `${Math.min(Math.max(window.scrollY + 8, desiredTop), maxTop)}px`;
  popoverEl.style.left = `${Math.min(Math.max(8, desiredLeft), maxLeft)}px`;

  popoverEl.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    if (btn.dataset.act === "copy") {
      navigator.clipboard?.writeText(text);
      showFloatToast("Copied", rect);
      clearPopover();
      return;
    }
    if (btn.dataset.act === "add") {
      // Capture which paragraph the selection sits in so the Add form can
      // render the partner-side paragraph (EN→ZH or ZH→EN) as a click-to-
      // fill reference for non-native users. Falls back to null when the
      // selection isn't cleanly inside a single <p>.
      const paraIdx = _paragraphIndexFromRange(range, inZh ? bodyZh : bodyEn);
      showAddForm(text, inZh, rect, paraIdx);
    } else if (btn.dataset.act === "revise") {
      showReviseForm(existing, rect);
    } else if (btn.dataset.act === "bookmark") {
      // Capture the paragraph that the selection sits in (more accurate
      // than the scroll-top fallback the ☆ button uses). Walk the range's
      // common ancestor up to the nearest <p> inside body-en/zh, then
      // index it.
      const para = _paragraphIndexFromRange(range, inZh ? bodyZh : bodyEn);
      _openBookmarkAddDialogFromSelection(para, text);
      clearPopover();
      window.getSelection()?.removeAllRanges();
    } else if (btn.dataset.act === "concordance") {
      _openConcordanceDialog(text);
      clearPopover();
      window.getSelection()?.removeAllRanges();
    }
  });
}

/* ---- Inline term-edit popover (WTR-lab style click-to-edit) ----
 *
 * Edit mode only. Click a highlighted .term span, edit its English
 * rendering in a small floating form, save: PATCH the glossary entry
 * (implicitly locks it) + apply-in-place across all chapters of this
 * novel (word-boundary, case-sensitive — see
 * backend/services/find_replace.py::apply_in_place_for_glossary_term).
 * No three-way choice dialog — the WTR-lab feel is instant rewrite.
 */
let termEditPop = null;
function clearTermEditPop() {
  if (termEditPop) { termEditPop.remove(); termEditPop = null; }
}

function showTermEditPop(span, entry, side) {
  clearTermEditPop();
  // The selection popover and the term-edit popover are mutually
  // exclusive — close the other side so they don't overlap.
  clearPopover();

  const rect = span.getBoundingClientRect();
  const pop = document.createElement("div");
  pop.className = "sel-form term-edit-pop";
  // Pin the locked checkbox to checked by default: the user is editing,
  // which already implicitly locks the entry server-side (see
  // backend/services/glossary.py::update_entry). Surfacing it as checked
  // matches the truth so users aren't surprised.
  const categories = ["character", "place", "technique", "item", "other", "idiom"];
  const cat = entry.category || "other";
  pop.innerHTML = `
    <div class="term-edit-head">
      <span class="term-edit-zh">${escapeHtml(entry.term_zh)}</span>
      <span class="term-edit-sep">↔</span>
      <span class="muted">${escapeHtml(entry.term_en)}</span>
    </div>
    <div class="row"><label style="width:64px;">English</label><input id="te-en" value="${escapeHtml(entry.term_en)}"></div>
    <div class="row"><label style="width:64px;">Category</label>
      <select id="te-cat">
        ${categories.map(c => `<option value="${c}"${c === cat ? " selected" : ""}>${c}</option>`).join("")}
      </select>
    </div>
    <div class="row"><label style="width:64px;">Locked</label>
      <input id="te-lock" type="checkbox"${entry.locked ? " checked" : ""}>
      <span class="muted" style="font-size:11.5px;">Editing locks automatically.</span>
    </div>
    <div id="te-err" class="muted" style="color: var(--signal-error); min-height: 1em;"></div>
    <div class="actions">
      <button class="btn-ghost" data-act="delete" title="Remove this glossary entry">Remove</button>
      <span style="flex:1;"></span>
      <button class="btn-ghost" data-act="cancel">Cancel</button>
      <button class="btn-primary" data-act="save">Save</button>
    </div>
  `;
  document.body.appendChild(pop);

  // Position like .sel-pop: prefer above the span, fall back below if no
  // room. Viewport-clamp on all four sides so edge clicks don't render
  // off-screen.
  const popH = pop.offsetHeight;
  const popW = pop.offsetWidth;
  const above = window.scrollY + rect.top - popH - 10;
  const below = window.scrollY + rect.bottom + 10;
  const desiredTop = (rect.top > popH + 20) ? above : below;
  const desiredLeft = window.scrollX + rect.left + (rect.width / 2) - (popW / 2);
  const maxLeft = window.scrollX + window.innerWidth - popW - 8;
  const maxTop  = window.scrollY + window.innerHeight - popH - 8;
  pop.style.top  = `${Math.min(Math.max(window.scrollY + 8, desiredTop), maxTop)}px`;
  pop.style.left = `${Math.min(Math.max(8, desiredLeft), maxLeft)}px`;

  termEditPop = pop;

  const enInput = pop.querySelector("#te-en");
  const catSel  = pop.querySelector("#te-cat");
  const lockChk = pop.querySelector("#te-lock");
  const errEl   = pop.querySelector("#te-err");

  // Focus + select-all so the user can just start typing.
  setTimeout(() => { enInput.focus(); enInput.select(); }, 0);

  const close = () => clearTermEditPop();

  // Esc dismisses; Enter saves.
  pop.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { ev.preventDefault(); close(); }
    else if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      pop.querySelector("[data-act='save']").click();
    }
  });

  pop.querySelector("[data-act='cancel']").addEventListener("click", close);

  pop.querySelector("[data-act='delete']").addEventListener("click", async () => {
    if (!confirm(`Remove "${entry.term_zh} ↔ ${entry.term_en}" from the glossary? Existing chapter text is unchanged.`)) return;
    try {
      await api.deleteGlossary(entry.id);
      await loadGlossary();
      invalidateTermPattern();
      if (lastChapter) renderChapterBody(lastChapter);
      showFloatToast("Removed from glossary", rect);
      close();
    } catch (e) {
      errEl.textContent = `Delete failed: ${e.message}`;
    }
  });

  pop.querySelector("[data-act='save']").addEventListener("click", async () => {
    const newEn = enInput.value.trim();
    if (!newEn) { errEl.textContent = "English term cannot be empty."; return; }
    const newCat = catSel.value;
    const newLock = lockChk.checked;
    const oldEn = (entry.term_en || "").trim();

    // Build the PATCH body from changed fields only. Skip the request
    // entirely if nothing changed (avoid a useless lock bump).
    const patch = {};
    if (newEn !== oldEn) patch.term_en = newEn;
    if (newCat !== (entry.category || "other")) patch.category = newCat;
    if (newLock !== !!entry.locked) patch.locked = newLock;
    if (Object.keys(patch).length === 0) { close(); return; }

    const saveBtn = pop.querySelector("[data-act='save']");
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    errEl.textContent = "";

    try {
      await api.updateGlossary(entry.id, patch);

      let chaptersUpdated = 0;
      let titlesUpdated = 0;
      let inPlaceFailed = false;
      if (patch.term_en && oldEn) {
        try {
          const res = await api.glossaryApplyInPlace(entry.id, oldEn, newEn);
          chaptersUpdated = res.chapters_updated || 0;
          titlesUpdated = res.rows_updated_titles || 0;
        } catch (e) {
          inPlaceFailed = true;
        }
      }

      // Refresh in-memory state so the live chapter pane reflects the
      // server-side rewrite without a manual reload.
      await loadGlossary();
      invalidateTermPattern();
      // If the title changed, the TOC entry needs updating too.
      if (titlesUpdated > 0) {
        try { await loadChapters(); } catch { /* best-effort */ }
      }
      // Refetch the current chapter so bodyEn / bodyZh pick up the
      // server-side substitution. Skip if we never had a chapter loaded
      // (defensive — shouldn't happen since the user clicked a term).
      if (lastChapter && typeof currentCh === "number") {
        try {
          const fresh = await api.chapter(novelId, currentCh);
          lastChapter = fresh;
          renderChapterBody(fresh);
        } catch {
          // Server unreachable mid-save — fall back to local re-render of
          // the stale chapter; the user will see the new highlight color
          // / term, but the body text won't reflect the rewrite until
          // they navigate. Better than a blank screen.
          renderChapterBody(lastChapter);
        }
      }

      if (inPlaceFailed) {
        showFloatToast("Glossary updated, but in-place rewrite failed. Try Glossary page.", rect);
      } else if (patch.term_en) {
        const chPart = `Renamed in ${chaptersUpdated} chapter${chaptersUpdated === 1 ? "" : "s"}`;
        const titlePart = titlesUpdated > 0 ? ` · ${titlesUpdated} title${titlesUpdated === 1 ? "" : "s"}` : "";
        showFloatToast(`${chPart}${titlePart}`, rect);
      } else {
        showFloatToast("Saved", rect);
      }
      close();
    } catch (e) {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save";
      errEl.textContent = `Save failed: ${e.message}`;
    }
  });
}

function _handleTermClick(ev, side) {
  if (readerMode !== "edit") return;
  // Defer to the selection popover when the user is making a selection.
  const sel = window.getSelection();
  if (sel && !sel.isCollapsed && sel.toString().trim()) return;
  const span = ev.target.closest(".term");
  if (!span) return;
  const dataTerm = span.getAttribute("data-term") || "";
  // The span renders the same-language text; data-term carries the
  // opposite-language partner. Look up by the side the click happened on.
  const shownText = (span.textContent || "").trim();
  const entry = termInfo(shownText, side) || (dataTerm ? termInfo(dataTerm, side === "en" ? "zh" : "en") : null);
  if (!entry) return;
  ev.preventDefault();
  ev.stopPropagation();
  showTermEditPop(span, entry, side);
}

bodyEn.addEventListener("click", (ev) => _handleTermClick(ev, "en"));
bodyZh.addEventListener("click", (ev) => _handleTermClick(ev, "zh"));

// Outside-click dismiss for the term-edit popover. Matches the
// mousedown handler the selection popover already uses; kept as a
// separate listener so they can't accidentally cancel each other.
document.addEventListener("mousedown", (ev) => {
  if (termEditPop && !termEditPop.contains(ev.target)) {
    // Don't dismiss when the click landed on another .term span — the
    // click handler above will rebuild the popover for the new target.
    if (!ev.target.closest(".term")) clearTermEditPop();
  }
});

// Walks up from `range.commonAncestorContainer` to the nearest <p> child of
// `pane` and returns its 0-based index among siblings (any-tag), matching the
// indexing convention bookmarks already use. Returns null when no enclosing
// <p> is found (e.g. selection inside the chapter mark or another container).
function _paragraphIndexFromRange(range, pane) {
  if (!range || !pane) return null;
  let node = range.commonAncestorContainer;
  if (node.nodeType === Node.TEXT_NODE) node = node.parentNode;
  while (node && node !== pane) {
    if (node.tagName === "P" && node.parentNode === pane) {
      const kids = Array.from(pane.children).filter(el => el.tagName === "P");
      const idx = kids.indexOf(node);
      return idx >= 0 ? idx : null;
    }
    node = node.parentNode;
  }
  return null;
}

// Opens the existing bookmark-add-dialog with the paragraph index pre-set
// from the selection. Reuses the same _pendingBookmarkParagraph state and
// the same dialog the ☆ button uses — no duplicated submit handler.
function _openBookmarkAddDialogFromSelection(paraIndex, selText) {
  _pendingBookmarkParagraph = paraIndex;
  if (bookmarkAddContext) {
    const paraTxt = paraIndex != null
      ? `paragraph ${paraIndex + 1}`
      : "chapter-level (no paragraph)";
    const excerpt = selText && selText.length > 60
      ? `${selText.slice(0, 60)}…`
      : selText || "";
    bookmarkAddContext.textContent =
      `Saving to Chapter ${currentCh} · ${paraTxt}${excerpt ? ` · "${excerpt}"` : ""}.`;
  }
  if (bookmarkAddNote) bookmarkAddNote.value = "";
  if (!bookmarkAddDialog.open) bookmarkAddDialog.showModal();
}

/* Tiny ephemeral toast anchored near a rect (selection rect or button rect).
 * Used for "Copied", "Queued", etc. — auto-clears after 1.6s. */
function showFloatToast(msg, rect) {
  const el = document.createElement("div");
  el.className = "float-toast";
  el.textContent = msg;
  document.body.appendChild(el);
  // Anchor it just above the selection / element.
  const left = window.scrollX + rect.left + (rect.width / 2);
  const top  = window.scrollY + rect.top - 30;
  el.style.left = `${Math.max(8, left)}px`;
  el.style.top  = `${Math.max(window.scrollY + 8, top)}px`;
  el.style.transform = "translateX(-50%)";
  setTimeout(() => el.remove(), 1600);
}

// Host the glossary mini-form inside a native <dialog> so we get a focus
// trap, Esc-to-close, and a modal backdrop for free — and so the form can't
// render off-screen near a viewport edge. `formEl` is still the `.sel-form`
// div, but it's now a child of the dialog instead of position:absolute on
// document.body.
function mountForm(_rect) {
  glossaryMiniDialog.innerHTML = "";
  glossaryMiniDialog.appendChild(formEl);
  if (popoverEl) popoverEl.style.display = "none";
  if (!glossaryMiniDialog.open) glossaryMiniDialog.showModal();
}

// Returns the raw paragraph text at `idx` on the given side, splitting the
// chapter body on \n\n. Used by the Add form's source-reference panel to
// surface the partner-side paragraph when the user selected from one pane.
function _paragraphTextAt(side, idx) {
  if (!lastChapter || idx == null || idx < 0) return "";
  let body = "";
  if (side === "zh") {
    body = lastChapter.original_text || "";
  } else {
    // Mirror renderChapterBody's body-picker: refined > translated.
    body = (lastChapter.refinement_status === "done" && lastChapter.refined_text)
      ? lastChapter.refined_text
      : (lastChapter.translated_text || "");
  }
  return body.split("\n\n")[idx] || "";
}

function showAddForm(text, inZh, rect, paragraphIdx) {
  formEl = document.createElement("div");
  formEl.className = "sel-form";
  const guessZh = inZh ? text : "";
  const guessEn = !inZh ? text : "";
  // Source-paragraph reference panel: when the user opened the form from a
  // selection, show the partner-side paragraph and let them click/drag in
  // it to fill the opposite input. Skipped when paragraphIdx is null (the
  // standalone "+ Term" button case, or a selection that didn't resolve
  // to a single <p>).
  const partnerSide = inZh ? "en" : "zh";
  const partnerText = _paragraphTextAt(partnerSide, paragraphIdx);
  const targetInputId = inZh ? "gf-en" : "gf-zh";
  const sourcePanel = (paragraphIdx != null && partnerText)
    ? `
    <div class="sel-form-source src-${partnerSide}" data-target="${targetInputId}">
      <div class="sel-form-source-label">
        <span>${partnerSide === "zh" ? "Source · 中文" : "Translation · English"} · ¶${paragraphIdx + 1}</span>
        <span class="hint">Select text to copy into the form ↓</span>
      </div>
      <div class="sel-form-source-body">${escapeHtml(partnerText)}</div>
    </div>`
    : "";
  formEl.innerHTML = `
    <div class="muted">Add to glossary</div>
    ${sourcePanel}
    <div class="row"><label style="width:60px;">Chinese</label><input id="gf-zh" value="${escapeHtml(guessZh)}" placeholder="中文 term" ${guessZh ? "readonly" : ""}></div>
    <div class="row"><label style="width:60px;">English</label><input id="gf-en" value="${escapeHtml(guessEn)}" placeholder="English term" ${guessEn ? "readonly" : ""}></div>
    <div class="row"><label style="width:60px;">Category</label>
      <select id="gf-cat">
        <option value="character" selected>character</option>
        <option value="place">place</option>
        <option value="technique">technique</option>
        <option value="item">item</option>
        <option value="other">other</option>
        <option value="idiom">idiom</option>
      </select>
    </div>
    <div class="row"><label style="width:60px;">Notes</label><input id="gf-notes" placeholder="optional"></div>
    <div id="gf-err" class="muted" style="color: var(--signal-error);"></div>
    <div class="actions">
      <button class="btn-ghost" data-act="cancel">Cancel</button>
      <button class="btn-primary" data-act="save">Save</button>
    </div>
  `;
  mountForm(rect);

  // Wire source-panel click-to-fill. On mouseup inside the panel, if the
  // user has a non-empty selection, copy it into the partner input and
  // briefly flash the panel so the action is acknowledged.
  formEl.querySelectorAll(".sel-form-source").forEach(panel => {
    panel.addEventListener("mouseup", () => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) return;
      const picked = sel.toString().trim();
      if (!picked) return;
      // Only react when the selection lives inside this panel — otherwise
      // text selected elsewhere in the dialog (e.g. a notes field) would
      // also overwrite the input.
      const anchor = sel.anchorNode;
      if (!anchor || !panel.contains(anchor.nodeType === 1 ? anchor : anchor.parentNode)) return;
      const input = formEl.querySelector("#" + panel.dataset.target);
      if (!input || input.hasAttribute("readonly")) return;
      input.value = picked;
      panel.classList.remove("flash-fill");
      void panel.offsetWidth; /* restart animation */
      panel.classList.add("flash-fill");
      sel.removeAllRanges();
    });
  });

  formEl.querySelector("[data-act='cancel']").addEventListener("click", clearPopover);
  formEl.querySelector("[data-act='save']").addEventListener("click", async () => {
    const zh = formEl.querySelector("#gf-zh").value.trim();
    const en = formEl.querySelector("#gf-en").value.trim();
    const cat = formEl.querySelector("#gf-cat").value;
    const notes = formEl.querySelector("#gf-notes").value.trim();
    const errEl = formEl.querySelector("#gf-err");
    if (!zh || !en) { errEl.textContent = "Chinese and English are both required."; return; }
    try {
      // Backend already inserts manual entries as locked=1; no need to pass it.
      const created = await api.createGlossary(novelId, {
        term_zh: zh, term_en: en, category: cat,
        notes: notes || null,
      });
      // createGlossary can either insert OR overwrite a prior unlocked entry,
      // so reconcile by id rather than always pushing.
      const idx = glossaryCache.findIndex(g => g.id === created.id);
      if (idx >= 0) glossaryCache[idx] = created; else glossaryCache.push(created);
      invalidateTermPattern();
      window.getSelection()?.removeAllRanges();
      reHighlight();
      showPostSavePrompt(created, rect, "add");
    } catch (e) {
      errEl.textContent = `Failed: ${e.message}`;
    }
  });
}

function showReviseForm(entry, rect) {
  formEl = document.createElement("div");
  formEl.className = "sel-form";
  const cats = ["character","place","technique","item","other","idiom"];
  // Surface the "this PATCH will implicitly lock the entry" behavior. The
  // server's update_entry() flips locked=1 on any non-locked field change
  // unless locked is passed explicitly. Showing a checkbox here lets the
  // user opt OUT of the implicit lock for a quick correction they don't
  // want to make permanent.
  const lockedChecked = entry.locked ? "checked" : "";
  const wasAutoTag = entry.locked ? "" : "<span class=\"sel-form-tag\">was auto</span>";
  formEl.innerHTML = `
    <div class="muted">Revise glossary term ${wasAutoTag}</div>
    <div class="row"><label style="width:60px;">Chinese</label><input id="gf-zh" value="${escapeHtml(entry.term_zh)}" readonly title="term_zh is the lookup key. Delete and re-add to change it"></div>
    <div class="row"><label style="width:60px;">English</label><input id="gf-en" value="${escapeHtml(entry.term_en)}"></div>
    <div class="row"><label style="width:60px;">Category</label>
      <select id="gf-cat">
        ${cats.map(c => `<option value="${c}" ${c === entry.category ? "selected" : ""}>${c}</option>`).join("")}
      </select>
    </div>
    <div class="row"><label style="width:60px;">Notes</label><input id="gf-notes" value="${escapeHtml(entry.notes || "")}" placeholder="optional"></div>
    <div class="row"><label style="width:60px;">Locked</label>
      <label style="display:flex;align-items:center;gap:6px;font-size:0.95em;">
        <input type="checkbox" id="gf-locked" ${lockedChecked}>
        <span class="muted">Protect from auto-overwrite by future translations</span>
      </label>
    </div>
    <div id="gf-err" class="muted" style="color: var(--signal-error);"></div>
    <div class="actions">
      <button class="btn-ghost" data-act="cancel">Cancel</button>
      <button class="btn-primary" data-act="save">Save</button>
    </div>
  `;
  mountForm(rect);

  formEl.querySelector("[data-act='cancel']").addEventListener("click", clearPopover);
  formEl.querySelector("[data-act='save']").addEventListener("click", async () => {
    const en = formEl.querySelector("#gf-en").value.trim();
    const cat = formEl.querySelector("#gf-cat").value;
    const notes = formEl.querySelector("#gf-notes").value.trim();
    const locked = formEl.querySelector("#gf-locked").checked;
    const errEl = formEl.querySelector("#gf-err");
    if (!en) { errEl.textContent = "English is required."; return; }
    try {
      // Pass locked explicitly so the server uses the user's choice instead
      // of its implicit lock-on-edit default.
      const updated = await api.updateGlossary(entry.id, {
        term_en: en,
        category: cat,
        notes: notes || null,
        locked,
      });
      const idx = glossaryCache.findIndex(g => g.id === updated.id);
      if (idx >= 0) glossaryCache[idx] = updated; else glossaryCache.push(updated);
      invalidateTermPattern();
      window.getSelection()?.removeAllRanges();
      reHighlight();
      showPostSavePrompt(updated, rect, "revise");
    } catch (e) {
      errEl.textContent = `Failed: ${e.message}`;
    }
  });
}

async function showPostSavePrompt(entry, rect, mode) {
  // Transition the in-place form into a propagation prompt: list how many
  // already-translated chapters contain this term, and offer one-click
  // re-translation via the existing /glossary/{id}/retranslate-affected
  // endpoint. We keep the same formEl mounted so the click target stays put.
  if (!formEl) {
    formEl = document.createElement("div");
    formEl.className = "sel-form";
    mountForm(rect);
  }
  const verb = mode === "revise" ? "Revised" : "Saved";
  formEl.innerHTML = `
    <div class="muted">${verb} <strong>${escapeHtml(entry.term_zh)}</strong> → <strong>${escapeHtml(entry.term_en)}</strong></div>
    <div class="muted" id="gf-affected">Checking which chapters use this term…</div>
    <div class="actions">
      <button class="btn-ghost" data-act="dismiss">Done</button>
      <button class="btn-primary" data-act="retranslate" disabled>Re-translate</button>
    </div>
  `;
  formEl.querySelector("[data-act='dismiss']").addEventListener("click", clearPopover);
  const goBtn = formEl.querySelector("[data-act='retranslate']");
  const affectedEl = formEl.querySelector("#gf-affected");

  let affected;
  try {
    affected = await api.affectedChapters(entry.id);
  } catch (e) {
    affectedEl.textContent = `Couldn't list affected chapters: ${e.message}`;
    goBtn.remove();
    return;
  }
  if (!affected || !affected.length) {
    affectedEl.textContent = "No prior chapters contain this term. Nothing to re-translate.";
    goBtn.remove();
    return;
  }
  const nums = affected.map(c => c.chapter_num);
  const shown = nums.slice(0, 8).join(", ");
  const more = nums.length > 8 ? `, +${nums.length - 8} more` : "";
  const n = nums.length;
  affectedEl.innerHTML = `<strong>${n}</strong> chapter${n === 1 ? "" : "s"} use this term: ${escapeHtml(shown + more)}.`;
  goBtn.disabled = false;
  goBtn.textContent = `Re-translate ${n} chapter${n === 1 ? "" : "s"}`;
  goBtn.addEventListener("click", async () => {
    goBtn.disabled = true;
    goBtn.textContent = "Queuing…";
    try {
      const res = await api.retranslateAffected(entry.id);
      const queued = res.queued_count || 0;
      const queuedNums = res.chapter_nums || [];
      statusEl.className = "status info";
      const preview = queuedNums.slice(0, 12).join(", ");
      const tail = queuedNums.length > 12 ? "…" : "";
      statusEl.textContent =
        `Re-translation queued for ${queued} chapter${queued === 1 ? "" : "s"}` +
        (preview ? `: ${preview}${tail}.` : ".");
      clearPopover();
      // Refresh the TOC so reset chapters show their pending state, and
      // re-load the current chapter if it was caught in the sweep so the
      // reader switches into its in-progress view. Reset the per-chapter
      // backoff so the first post-action poll fires at base cadence (the
      // chapter may have been stuck mid-translate before; we don't want
      // to inherit its old 30s poll interval).
      loadChapters();
      if (queuedNums.includes(currentCh)) {
        clearPollStart(currentCh);
        loadChapter(currentCh);
      }
    } catch (e) {
      goBtn.disabled = false;
      goBtn.textContent = "Re-translate";
      affectedEl.textContent = `Failed: ${e.message}`;
    }
  });
}

document.addEventListener("mouseup", (e) => {
  // Don't re-evaluate selection when the mouseup is inside the popover or
  // the add-to-glossary form — that would close them mid-interaction.
  if (popoverEl && popoverEl.contains(e.target)) return;
  if (formEl && formEl.contains(e.target)) return;
  setTimeout(showPopoverForSelection, 1);
});
document.addEventListener("mousedown", (e) => {
  if (popoverEl && popoverEl.contains(e.target)) return;
  if (formEl && formEl.contains(e.target)) return;
  clearPopover();
});

// Hide the floating reading-rail whenever the user has an active text
// selection in either reader pane — otherwise it collides with the selection
// popover at the bottom-right corner.
document.addEventListener("selectionchange", () => {
  const sel = window.getSelection();
  let active = false;
  if (sel && !sel.isCollapsed && sel.toString().trim().length > 0 && sel.rangeCount > 0) {
    const node = sel.getRangeAt(0).commonAncestorContainer;
    if (bodyEn.contains(node) || bodyZh.contains(node)) active = true;
  }
  document.body.classList.toggle("has-selection", active);
});

function reHighlight() {
  // Re-render the current body in place using the current glossary cache.
  if (lastChapter) renderChapterBody(lastChapter);
}

/* ---- Chapter loading & rendering ---- */
async function loadNovel() {
  novelMeta = await api.novel(novelId);
  tocNovelName.textContent = novelMeta.title;
  tocNovelMeta.textContent = `${novelMeta.total_chapters} chapters · ${novelMeta.source_type || ""}`;
  document.title = `${novelMeta.title} · Reader`;
  const crumbNovel = document.getElementById("crumb-novel");
  if (crumbNovel) {
    crumbNovel.textContent = novelMeta.title;
    // 2026-05-25: novel title in the breadcrumb routes to the per-novel
    // overview page rather than back to the library. The library is one
    // hop further up the trail (the existing "Library" link to the left).
    crumbNovel.href = `/novel?id=${novelId}`;
  }
}

// Populate the chapter masthead's mono dateline + prev/next chips +
// jade progress underline. Called every time we land on a chapter; the
// title text itself is set elsewhere (chH1En, chH1ZhSub).
function updateMasthead(num) {
  const total = chaptersCache.length;
  const idxN = document.getElementById("masthead-index-n");
  const idxTot = document.getElementById("masthead-index-tot");
  const idxWrap = document.getElementById("masthead-index");
  if (idxN && idxTot && total > 0) {
    // Pad to the width of the total so 1/30 reads "01/30" and 1/1424
    // reads "0001/1424". Keeps the dateline visually stable as the user
    // walks through chapters.
    const pad = String(total).length;
    idxN.textContent = String(num).padStart(pad, "0");
    idxTot.textContent = String(total).padStart(pad, "0");
    if (idxWrap) idxWrap.setAttribute("aria-label", `Chapter ${num} of ${total}`);
  } else if (idxN) {
    idxN.textContent = String(num);
    if (idxTot) idxTot.textContent = "";
    if (idxWrap) idxWrap.setAttribute("aria-label", `Chapter ${num}`);
  }
  const bar = document.getElementById("masthead-progress-bar");
  const prog = document.getElementById("masthead-progress");
  if (bar && total > 0) {
    const pct = Math.max(0, Math.min(100, (num / total) * 100));
    bar.style.width = `${pct.toFixed(2)}%`;
  }
  if (prog && total > 0) {
    prog.setAttribute("aria-valuenow", String(num));
    prog.setAttribute("aria-valuemax", String(total));
  }
}

async function loadChapters() {
  chaptersCache = await api.chapters(novelId);
  renderToc();
  // Keep the end-of-chapter card in sync with cache changes (new chapters
  // landing, next-chapter status flipping from translating → done, etc.).
  if (lastChapter && lastChapter.status === "done") paintEndCard(lastChapter);
}

async function loadGlossary() {
  try { glossaryCache = await api.glossary(novelId); }
  catch { glossaryCache = []; }
}

async function loadProviders() {
  // Cached for the bilingual pane label. A null cache means we couldn't
  // resolve providers — pane label silently falls back to plain "English".
  try { _providersCache = await api.providers(); }
  catch { _providersCache = []; }
}

function providerNameById(id) {
  if (!id || !_providersCache) return null;
  const p = _providersCache.find(x => x.id === id);
  return p ? p.name : null;
}

// Mirror for the banner-copy branch: the reader needs to distinguish
// "free-tier rough draft" (provider_type='google_translate_free') from "LLM
// polished" so the quality banner can say something honest. Returns null when the
// provider can't be resolved (cache miss / pre-migration row).
function providerTypeById(id) {
  if (!id || !_providersCache) return null;
  const p = _providersCache.find(x => x.id === id);
  return p ? p.provider_type : null;
}

// Render the bilingual EN pane label with provider attribution and an inline
// "refined by X" chip when the chapter shipped through a successful refinement
// pass. Stays minimal when provider info is unresolvable — never breaks the
// reading column.
function updatePaneEnLabel(ch) {
  if (paneEnLabel) {
    const tname = providerNameById(novelMeta?.translator_provider_id);
    let html = tname
      ? `English · <span class="prov-name">${escapeHtml(tname)}</span>`
      : "English";
    const refined = ch && ch.refinement_status === "done" && _displayedEnglish(ch) === (ch.refined_text || "");
    if (refined) {
      const rname = providerNameById(ch.refined_by_provider_id);
      const chipText = rname ? `refined by ${rname}` : "refined";
      html += ` <span class="refinement-chip">${escapeHtml(chipText)}</span>`;
    }
    paneEnLabel.innerHTML = html;
  }
}

async function cancelOneFromQueue(chapterNum, btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await api.cancelQueueChapter(novelId, chapterNum);
    if (res.cancelled_translate) {
      statusEl.className = "status info";
      statusEl.textContent = `Chapter ${chapterNum} removed from the translation queue.`;
    } else if (res.in_flight_translate) {
      statusEl.className = "status info";
      statusEl.textContent = `Chapter ${chapterNum} is currently being translated. Can't cancel mid-call. It will finish on its own.`;
    }
    loadChapters();
    if (chapterNum === currentCh) loadChapter(currentCh);
  } catch (e) {
    statusEl.className = "status err";
    statusEl.textContent = `Cancel failed: ${e.message}`;
    if (btn) btn.disabled = false;
  }
}

async function cancelAllFromQueue(btn) {
  const queuedNow = chaptersCache.filter(c => c.translate_queued).length;
  if (queuedNow === 0) return;
  const ok = await confirmDialog({
    title: "Clear queue?",
    body: `<p>Remove <strong>${queuedNow}</strong> chapter${queuedNow === 1 ? "" : "s"} from the queue?</p><p class="muted">The chapter currently being processed will still finish. It can't be cancelled mid-call.</p>`,
    okText: "Clear queue",
    danger: true,
  });
  if (!ok) return;
  const original = btn ? btn.textContent : null;
  if (btn) { btn.disabled = true; btn.textContent = "Clearing…"; }
  try {
    const res = await api.cancelQueueAll(novelId);
    const total = res.cancelled_translate || 0;
    const inFlight = res.in_flight_translate || 0;
    statusEl.className = "status info";
    if (total === 0 && inFlight > 0) {
      statusEl.textContent = `Nothing waiting. ${inFlight} chapter still in flight, it will finish on its own.`;
    } else {
      const suffix = inFlight > 0 ? ` (${inFlight} still in flight)` : "";
      statusEl.textContent = `Cleared ${total} chapter${total === 1 ? "" : "s"} from the queue${suffix}.`;
    }
    loadChapters();
    loadChapter(currentCh);
  } catch (e) {
    statusEl.className = "status err";
    statusEl.textContent = `Clear failed: ${e.message}`;
    if (btn) { btn.disabled = false; btn.textContent = original; }
  }
}

function startLoader(ch, stage) {
  const key = `${ch.chapter_num}:${stage}`;
  const sameStage = activeLoader
    && activeLoader.chapterNum === ch.chapter_num
    && activeLoader.stage === stage;
  if (!stageStarts.has(key)) stageStarts.set(key, performance.now());
  activeLoader = {
    chapterNum: ch.chapter_num,
    stage,
    t0: stageStarts.get(key),
  };
  loaderLabel.textContent = `Translating chapter ${ch.chapter_num}…`;
  // The bar is a CSS indeterminate animation — nothing to reset between poll
  // ticks. Only the real elapsed counter needs seeding on a fresh stage.
  if (!sameStage) loaderElapsed.textContent = "0s elapsed";
  chapterLoader.classList.remove("hidden");
  // Hide the entire reading column (Chinese pane, chapter mark/seal, English
  // body) so the loading screen shows nothing but the progress bar.
  document.getElementById("dual-grid").classList.add("hidden");
  bodyEn.innerHTML = "";
  if (!rafHandle) tickLoader();
}

// The bar carries no real progress (a chapter is one LLM call), so the only
// thing to keep current is the honest "Ns elapsed" readout.
function tickLoader() {
  if (!activeLoader) { rafHandle = null; return; }
  const elapsed = performance.now() - activeLoader.t0;
  loaderElapsed.textContent = Math.floor(elapsed / 1000) + "s elapsed";
  rafHandle = requestAnimationFrame(tickLoader);
}

function stopLoader() {
  if (rafHandle) { cancelAnimationFrame(rafHandle); rafHandle = null; }
  _cancelPoll();
  activeLoader = null;
  chapterLoader.classList.add("hidden");
  // Restore the reading column hidden by startLoader. Callers run renderChapter
  // synchronously right after, so the revealed grid is repopulated before the
  // browser paints — no flash of the previous chapter.
  document.getElementById("dual-grid").classList.remove("hidden");
}

function clearStageStart(chapterNum, stage) {
  stageStarts.delete(`${chapterNum}:${stage}`);
}

/* ---- Per-chapter scroll position memory ----
 * Saves window.scrollY per (novel, chapter) to localStorage, debounced on
 * scroll. Restored after the body actually renders (double rAF). Reading
 * progress within a long chapter is the single most-missed feature in a
 * reading app; without this, every chapter open lands at the top. */
const SCROLL_SAVE_DEBOUNCE_MS = 250;
let _scrollSaveTimer = null;
// While true, the scroll listener doesn't queue a save. Set by code paths
// that issue programmatic scrollTo (chapter nav reset, scroll-position
// restore) so the synthetic scroll events those generate don't overwrite
// the user's saved offset with the current y=0 transient state.
let _scrollIgnore = false;
let _scrollIgnoreTimer = null;
function _ignoreScrollFor(ms = 400) {
  _scrollIgnore = true;
  if (_scrollIgnoreTimer) clearTimeout(_scrollIgnoreTimer);
  _scrollIgnoreTimer = setTimeout(() => {
    _scrollIgnore = false;
    _scrollIgnoreTimer = null;
  }, ms);
}
function _scrollKey(num) { return `scrollPos:${novelId}:${num}`; }
function _persistCurrentScroll() {
  if (!currentCh) return;
  const y = Math.round(window.scrollY);
  // Don't store negligible offsets — wastes keys and a 0 restore is
  // indistinguishable from "never read."
  if (y > 16) localStorage.setItem(_scrollKey(currentCh), String(y));
  else localStorage.removeItem(_scrollKey(currentCh));
}
function _scheduleScrollSave() {
  if (_scrollIgnore) return;
  if (_scrollSaveTimer) return;
  _scrollSaveTimer = setTimeout(() => {
    _scrollSaveTimer = null;
    if (_scrollIgnore) return; // re-check at fire time
    _persistCurrentScroll();
  }, SCROLL_SAVE_DEBOUNCE_MS);
}
window.addEventListener("scroll", _scheduleScrollSave, { passive: true });
window.addEventListener("beforeunload", _persistCurrentScroll);

function restoreScrollFor(num) {
  const raw = localStorage.getItem(_scrollKey(num));
  if (!raw) return;
  const y = parseInt(raw, 10);
  if (!Number.isFinite(y) || y <= 0) return;
  // Double rAF: rendered HTML needs a layout pass before scroll positions are
  // honored. One rAF is the layout frame; two ensures any deferred Markdown /
  // DOMPurify render has settled.
  _ignoreScrollFor(400);
  requestAnimationFrame(() => requestAnimationFrame(() => {
    window.scrollTo({ top: y, behavior: "auto" });
  }));
}

// lastChapter declared near the top of the script — see comment there.
// F14 (2026-05-25): pre-render next chapter cache. When loadChapter
// commits a `done` chapter, we kick off a background fetch for ch+1 and
// cache the response. On Next, _prefetchedChapter is consumed if fresh.
// 2-minute TTL; eviction on chapter status transition (pending→done
// in the chapter we're caching, which would invalidate the cached body).
const _PREFETCH_TTL_MS = 2 * 60_000;
const _prefetchedChapter = { num: null, data: null, at: 0 };

function _prefetchNext(currentDoneChapter) {
  const nextNum = currentDoneChapter + 1;
  // Find the next chapter in cache list; only prefetch when it's also done.
  const cached = chaptersCache.find(c => c.chapter_num === nextNum);
  if (!cached || cached.status !== "done") return;
  if (_prefetchedChapter.num === nextNum
      && Date.now() - _prefetchedChapter.at < _PREFETCH_TTL_MS) {
    return; // already fresh
  }
  api.chapter(novelId, nextNum)
    .then(d => {
      _prefetchedChapter.num = nextNum;
      _prefetchedChapter.data = d;
      _prefetchedChapter.at = Date.now();
    })
    .catch(() => { /* best-effort */ });
}

async function loadChapter(num) {
  // Cancel any prior poll handle unconditionally. This is the single guard
  // that prevents a stale timer captured against an old `num` from firing
  // loadChapter(oldNum), snapping the URL back via history.replaceState,
  // and rendering the old chapter over the new one.
  _cancelPoll();

  // F14 (2026-05-25): 404 fast-fail. If the chapters list is already
  // loaded and doesn't contain `num`, this is a URL typo (?ch=999 on a
  // 50-chapter novel) — skip the retry-with-backoff loop and surface
  // "not found" immediately. We only fast-fail when chaptersCache is
  // non-empty; an empty cache means we genuinely don't know yet.
  if (chaptersCache.length > 0
      && !chaptersCache.some(c => c.chapter_num === num)) {
    bodyEn.innerHTML = `<p class="muted">Chapter ${num} doesn't exist in this novel. <a href="/library">Back to library</a>.</p>`;
    bodyZh.innerHTML = "";
    return;
  }

  // F14 (2026-05-25): consume the pre-render cache if fresh. The cache
  // hit avoids the API round-trip; the user sees the next chapter
  // render essentially instantly.
  if (_prefetchedChapter.num === num
      && _prefetchedChapter.data
      && Date.now() - _prefetchedChapter.at < _PREFETCH_TTL_MS) {
    const cachedCh = _prefetchedChapter.data;
    _prefetchedChapter.num = null;
    _prefetchedChapter.data = null;
    // Apply the same prologue as the normal path so the loader / scroll
    // reset still fires before render — easier than carving out a
    // shortcut path.
    if (currentCh !== num) {
      if (typeof _exitEditMode === "function") _exitEditMode();
      if (_scrollSaveTimer) { clearTimeout(_scrollSaveTimer); _scrollSaveTimer = null; }
      _persistCurrentScroll();
      _ignoreScrollFor(400);
      window.scrollTo(0, 0);
    }
    currentCh = num;
    history.replaceState(null, "", `/reader?novel=${novelId}&ch=${num}`);
    updateMasthead(num);
    lastChapter = cachedCh;
    stopLoader();
    bodyEn.innerHTML = "";
    bodyZh.innerHTML = "";
    document.getElementById("quality-banner")?.remove();
    document.getElementById("glossary-merge-error-card")?.remove();
    renderToc();
    renderChapterBody(cachedCh);
    // Kick off the next prefetch.
    if (cachedCh.status === "done") _prefetchNext(num);
    return;
  }
  // When navigating to a different chapter, tear down any in-flight loader.
  // When polling the same chapter, leave it running — startLoader keys off
  // stageStarts so the bar resumes from the right elapsed time. (stopLoader
  // calls _cancelPoll() too — that's a no-op given the unconditional clear
  // above, but keeps stopLoader self-contained for its other callers.)
  if (currentCh !== num) {
    stopLoader();
  }
  // When navigating to a DIFFERENT chapter, reset the scroll position to the
  // top of the new chapter. Without this, the previous chapter's offset is
  // kept and the new chapter loads mid-page (or past the bottom if the new
  // chapter is shorter). Skip the reset on same-chapter re-entry (a poll
  // tick) so the user's scroll progress is preserved during in-flight polls.
  if (currentCh !== num) {
    // If the user was editing a paragraph, exit edit mode now. The
    // re-render inside _exitEditMode replaces bodyEn.innerHTML which steals
    // focus from any contenteditable <p>; the blur handler fires using the
    // dataset captured at focus time, so the pending edit posts against
    // the ORIGINAL chapter, not the one we're about to navigate to.
    if (typeof _exitEditMode === "function") _exitEditMode();
    // Flush any pending save for the OUTGOING chapter (the user may have
    // navigated faster than the debounce timer).
    if (_scrollSaveTimer) { clearTimeout(_scrollSaveTimer); _scrollSaveTimer = null; }
    _persistCurrentScroll();
    // Programmatic scroll fires a scroll event that would otherwise queue a
    // save of y=0 for the new chapter — suppress for the next 400ms.
    _ignoreScrollFor(400);
    window.scrollTo(0, 0);
  }
  currentCh = num;
  history.replaceState(null, "", `/reader?novel=${novelId}&ch=${num}`);
  updateMasthead(num);
  statusEl.className = "status";
  statusEl.textContent = "";
  statusEl.hidden = false;
  statusEl.removeAttribute("role");
  // Preserve the loader DOM when the active loader is for this chapter; the
  // "Loading…" placeholder would otherwise flash behind the loader card.
  if (!activeLoader || activeLoader.chapterNum !== num) {
    bodyEn.innerHTML = '<p class="muted">Loading…</p>';
  }
  bodyZh.innerHTML = "";
  // Wipe any prior chapter's quality / merge banners up front so early-return
  // paths (pending / 404 / load-fail) don't leave them on screen.
  document.getElementById("quality-banner")?.remove();
  document.getElementById("glossary-merge-error-card")?.remove();
  renderToc();

  try {
    const ch = await api.chapter(novelId, num);
    lastChapter = ch;
    // R10: surface the chapter status to CSS so the reading-rail can hide
    // itself while the chapter is pending/translating instead of reporting a
    // stale 0% from the prior chapter.
    document.body.dataset.chapterStatus = ch.status || "";
    // Reset the 404 retry counter on any successful load — a chapter that
    // was being created mid-poll has now materialised.
    _resetNotFoundCount(num);
    // Fire-and-forget: if this chapter's state has drifted from the cache
    // (queue progressed, glossary edit flipped status to 'stale', …), pull
    // the chapter list so the TOC glyphs catch up. Awaiting would block the
    // render for a network round-trip the user doesn't need.
    refreshTocIfStale(ch);
    if (ch.status === "pending" || ch.status === "translating") {
      setChapterBarTitle(num, null, ch.title_zh);
      // Title hasn't been translated yet; fall back to the Han title (with
      // any 第N章 prefix stripped) or "Chapter N" as last resort. The Han
      // subtitle below gets the same source so we don't render twice.
      const zhStripped = stripChapterPrefix(displayTitleZh(ch.title_zh), true);
      chH1En.textContent = zhStripped || `Chapter ${num}`;
      if (chH1ZhSub) chH1ZhSub.textContent = zhStripped || "";
      endBlock.classList.add("hidden");
      // 2026-05-26 resumable imports: a chapter with no original_text yet
      // is a skeleton row from a recipe scrape whose runner hasn't
      // reached this chapter. Show a distinct "awaiting fetch" hint
      // instead of an empty "Show original Chinese" disclosure that
      // expands to nothing useful.
      const stillImporting =
        novelMeta && novelMeta.import_status === "in_progress"
        && (!ch.original_text || ch.original_text.length === 0);
      if (stillImporting) {
        bodyEn.innerHTML =
          `<p class="muted"><strong>Awaiting source fetch.</strong> ` +
          `This chapter hasn't been downloaded yet. The importer is still ` +
          `working through the chapter list. Library card shows live progress; ` +
          `this view refreshes automatically.</p>`;
        pollHandle = setTimeout(() => loadChapter(num), pollInterval(num, 4000));
        return;
      }
      const zhDetails = ch.original_text
        ? `<details style="margin-top: 18px;"><summary class="muted">Show original Chinese</summary>${escapeHtml(ch.original_text).replace(/\n/g, "<br>")}</details>`
        : "";
      if (ch.status === "translating") {
        startLoader(ch, "translate");
        pollHandle = setTimeout(() => loadChapter(num), pollInterval(num, 3000));
      } else if (ch.translate_queued || (expireAwaitingQueueStart(num), awaitingQueueStart.has(num))) {
        // The chapter is in the durable queue but a different chapter is
        // currently holding the translator lock. Show a distinct "queued,
        // waiting" state — not the in-flight loader.
        if (ch.translate_queued) awaitingQueueStart.delete(num);
        // Show the (provisional) English title with a "not yet translated"
        // tag in the H1 so the user knows what this chapter IS even before
        // the translator runs. Han subtitle below carries the source title.
        if (ch.title_en) {
          chH1En.innerHTML = `${escapeHtml(stripChapterPrefix(ch.title_en))} <span class="pending-tag">· queued</span>`;
        } else {
          chH1En.innerHTML = `${escapeHtml(stripChapterPrefix(displayTitleZh(ch.title_zh), true) || `Chapter ${num}`)} <span class="pending-tag">· queued</span>`;
        }
        bodyEn.innerHTML =
          `<p class="muted"><strong>Queued for translation. Waiting on the translator.</strong> The translator processes one chapter at a time; this chapter will start as soon as it's its turn.</p>` +
          zhDetails;
        // Re-poll so we flip to the translate loader once this chapter
        // claims the lock.
        pollHandle = setTimeout(() => loadChapter(num), pollInterval(num, 3000));
      } else {
        // pending = imported but never queued. Don't poll — nothing will
        // change until the user starts translation. Render the design §2
        // pre-translation cockpit: raw preview with glossary highlights on
        // the left, glossary preflight list on the right, dark CTA bar
        // beneath. Saturation fetches automatically; the cockpit IS the
        // glossary check.
        if (ch.title_en) {
          chH1En.innerHTML = `${escapeHtml(stripChapterPrefix(ch.title_en))} <span class="pending-tag">· not yet translated</span>`;
        } else {
          chH1En.innerHTML = `${escapeHtml(stripChapterPrefix(displayTitleZh(ch.title_zh), true) || `Chapter ${num}`)} <span class="pending-tag">· not yet translated</span>`;
        }
        renderCockpit(ch, num, zhDetails);
        const startBtn = document.getElementById("start-pending");
        startBtn.addEventListener("click", async () => {
          startBtn.disabled = true;
          const originalLabel = startBtn.textContent;
          startBtn.textContent = "Queuing…";
          try {
            await api.retranslate(novelId, num);
            statusEl.className = "status info";
            statusEl.textContent = `Chapter ${num} queued.`;
            // The /retranslate response is synchronous-after-DB-write, so
            // the next poll will see translate_queued=1. Set the local
            // hint so the very next render shows the queued state without
            // flickering through the pending CTA again. Also refresh the
            // TOC right now so the queued glyph appears on this row
            // immediately instead of one navigation later.
            awaitingQueueStart.set(num, performance.now());
            loadChapters();
            pollHandle = setTimeout(() => loadChapter(num), 1200);
          } catch (e) {
            statusEl.className = "status err";
            statusEl.textContent = `Failed to queue: ${e.message}`;
            startBtn.disabled = false;
            startBtn.textContent = originalLabel;
          }
        });
      }
      return;
    }
    if (ch.status === "error") {
      statusEl.className = "status err";
      // Show a short summary inline; full error sits behind a disclosure for
      // long stack traces / JSON dumps the backend now persists (up to 4k).
      const full = String(ch.error_msg || "unknown");
      const head = full.length > 200 ? full.slice(0, 200) + "…" : full;
      statusEl.setAttribute("role", "alert");
      statusEl.innerHTML = `
        <span class="msg">Error: ${escapeHtml(head)}</span>
        ${full.length > 200
          ? `<details class="error-full"><summary>Show full error</summary><pre class="error-full-text">${escapeHtml(full)}</pre></details>`
          : ""}
        <button type="button" class="status-dismiss" aria-label="Dismiss error">×</button>
      `;
      // Click handler: delegated from statusEl at module init.
    } else if (ch.status === "stale") {
      // Glossary changed since this chapter was translated — surface the
      // mismatch and offer one-click retranslation. The button shares the
      // existing #retranslate logic via the delegation on statusEl.
      statusEl.className = "status info";
      statusEl.removeAttribute("role");
      statusEl.innerHTML = `Glossary updated since this was translated. <button type="button" class="stale-action" id="stale-retranslate">Retranslate</button>`;
    }
    // Reached terminal state — discard timer anchors and close the loader if
    // it was running for this chapter.
    if (activeLoader && activeLoader.chapterNum === ch.chapter_num) {
      stopLoader();
    }
    awaitingQueueStart.delete(ch.chapter_num);
    clearPollStart(ch.chapter_num);
    clearStageStart(ch.chapter_num, "translate");
    renderChapterBody(ch);
    if (ch.status === "done") {
      persistLastRead(ch);
      endBlock.classList.remove("hidden");
      paintEndCard(ch);
      // Restore the user's last scroll position within this chapter. Only on
      // status=done — for pending/error states there's nothing meaningful to
      // scroll into yet.
      restoreScrollFor(num);
      // Phase 4: refinement-in-flight continuation poll. The chapter is
      // displayed (draft body), but the refiner is still running; re-poll
      // so the banner updates and the body switches to refined_text when
      // the refiner finishes.
      if (
        ch.refinement_status === "pending"
        || ch.refinement_status === "in_progress"
      ) {
        pollHandle = setTimeout(() => loadChapter(num), pollInterval(num, 3000));
      }
    } else {
      endBlock.classList.add("hidden");
    }
    // 2026-05-26 — keep polling while the mechanical-NMT free draft is in
    // flight so the reader switches from "nothing to display" to
    // free_draft_text once the worker finishes (typically a few seconds for
    // Google Translate). Same pattern as the refinement continuation poll above.
    if (
      ch.free_draft_status === "pending"
      || ch.free_draft_status === "in_progress"
    ) {
      pollHandle = setTimeout(() => loadChapter(num), pollInterval(num, 3000));
    }
  } catch (e) {
    if (e.message && e.message.startsWith("404:")) {
      // Bounded retry. The original intent (auto-recovery when a chapter
      // is being created mid-poll) is preserved for the first few attempts.
      // Past _NOT_FOUND_MAX we surface a definitive "not found" UI rather
      // than polling forever — a typoed ?ch=999 URL or a deleted novel
      // otherwise hammers the server every 3s indefinitely.
      const count = (_notFoundCount.get(num) || 0) + 1;
      _notFoundCount.set(num, count);
      if (count > _NOT_FOUND_MAX) {
        setChapterBarTitle(num, `Chapter ${num} not found`, null);
        bodyEn.innerHTML =
          `<p class="muted">Chapter ${num} does not exist on this novel. ` +
          `<a href="/library">Back to library</a>.</p>`;
        return;
      }
      setChapterBarTitle(num, `Chapter ${num}`, null);
      bodyEn.innerHTML =
        `<p class="muted">Chapter not yet available. Polling ` +
        `(${count}/${_NOT_FOUND_MAX})…</p>`;
      pollHandle = setTimeout(() => loadChapter(num), pollInterval(num, 3000));
      return;
    }
    statusEl.className = "status err";
    statusEl.textContent = `Load failed: ${e.message}`;
    bodyEn.innerHTML = "";
  }
}

// Phase 4: choose which English body to display. Refined text wins when
// refinement_status='done'; otherwise fall back to the translator's draft.
// All bodyEn render / copy / word-count / last-read sites route through
// here so a single change point handles the switch.
function _displayedEnglish(ch) {
  if (!ch) return "";
  // 2026-05-27 — explicit "show me the mechanical NMT draft" branch.
  // Only fires when the user has flipped #toggle-source to "free_draft" AND
  // the chapter actually has a free_draft body. If free_draft_text is
  // missing on this chapter (e.g., user picked Free draft on a chapter that
  // had both, then navigated to one that only has polished), this branch
  // falls through to the polished fallback chain below — the toggle is
  // hidden in that case by applyTranslationSource so the picker can't lie.
  if (translationSource === "free_draft" && ch.free_draft_text) {
    return ch.free_draft_text;
  }
  // 2026-05-27: when the novel has a refiner configured AND polishing
  // is mid-flight, suppress the draft. The user explicitly does not want
  // to see the draft body and then watch it get replaced by the refined
  // version a minute later — they only want the polished output. The
  // refinement banner still surfaces the "polishing in progress" status.
  if (ch.refinement_status === "pending" || ch.refinement_status === "in_progress") {
    return "";
  }
  if (ch.refinement_status === "done" && ch.refined_text) {
    return ch.refined_text;
  }
  if (ch.translated_text) return ch.translated_text;
  // 2026-05-26 — free-tier rough draft fallback. When the LLM translation
  // hasn't completed (or hasn't been requested), but the mechanical NMT
  // free draft is ready, render that so the reader has something to read.
  // The applyQualityBanner branch above renders the matching banner.
  if (ch.free_draft_text) return ch.free_draft_text;
  return "";
}

// Phase 4: surface the refinement state when the novel has a refiner
// configured. Mirrors applyGlossaryMergeBanner — inserted just above
// bodyEn so the user notices it without leaving the chapter view.
function applyRefinementBanner(ch) {
  const prior = document.getElementById("refinement-banner");
  if (prior) prior.remove();
  if (!ch || !ch.refinement_status || ch.refinement_status === "none") return;
  if (ch.refinement_status === "done") return;
  const card = document.createElement("div");
  card.id = "refinement-banner";
  card.className = "alert-banner refinement-banner";
  card.setAttribute("role", "status");
  if (ch.refinement_status === "pending") {
    card.innerHTML = `<span class="msg">Refinement queued. The polished version will appear here when polishing completes.</span>`;
  } else if (ch.refinement_status === "in_progress") {
    card.innerHTML = `<span class="msg">Polishing in progress. The polished version will appear when complete…</span>`;
  } else if (ch.refinement_status === "error") {
    const msg = ch.refinement_error || "unknown error";
    card.innerHTML = `
      <span class="msg">Refinement failed: <span class="muted">${escapeHtml(msg)}</span></span>
      <button type="button" class="retry" id="refinement-retry">↻ Retry refinement</button>
    `;
  }
  bodyEn.parentElement.insertBefore(card, bodyEn);
  const retryBtn = card.querySelector("#refinement-retry");
  if (retryBtn) {
    retryBtn.addEventListener("click", async () => {
      retryBtn.disabled = true;
      try {
        await api.retryRefinement(novelId, ch.chapter_num);
        loadChapter(ch.chapter_num);
      } catch (e) {
        retryBtn.disabled = false;
        alert(`Retry failed: ${e.message}`);
      }
    });
  }
}

// Pre-translation cockpit (design §2). Renders into bodyEn for a
// chapter in `pending` state. Two panes (raw preview + glossary
// preflight) + dark CTA bar. Saturation is auto-fetched so the user
// sees candidates immediately — the cockpit IS the glossary check, no
// separate "Check glossary first" button anymore.
function renderCockpit(ch, num, zhDetailsHtml) {
  const raw = ch.original_text || "";
  // Count how many CJK characters are in the body so we can show the
  // "X 字" rune in the pane header. Use the same Unicode block as the
  // backend's parser (kHan + extensions).
  const cjkCount = (raw.match(/[㐀-鿿]/g) || []).length;
  const paraCount = raw.split(/\n\s*\n/).filter(p => p.trim()).length || 1;

  // Slice the first ~280 CJK chars worth of raw. Counting bytes / code
  // points is easier than counting CJK exactly; ~360 code points is a
  // reasonable proxy. The fade gradient masks the trailing edge.
  const preview = raw.slice(0, 360);

  // Build a per-entry hit count against the raw text. Only entries
  // that fire on THIS chapter end up in the preflight list. Cap the
  // result so we don't enumerate 1900 zero-hit terms.
  const escapeRe = s => s.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&");
  const lockedHits = [];
  const autoHits = [];
  for (const g of glossaryCache) {
    if (!g.term_zh) continue;
    const re = new RegExp(escapeRe(g.term_zh), "g");
    const m = raw.match(re);
    if (!m) continue;
    const row = { ...g, _count: m.length };
    if (g.locked) lockedHits.push(row);
    else autoHits.push(row);
  }
  lockedHits.sort((a, b) => b._count - a._count);
  autoHits.sort((a, b) => b._count - a._count);

  // Apply highlights to the preview text in priority order: locked >
  // auto > candidate (candidates filled in once saturation resolves).
  function applyHighlights(text, lockedTerms, autoTerms, candidateTerms) {
    if (!text) return "";
    let html = escapeHtml(text);
    const mark = (terms, cls) => {
      for (const term of terms) {
        if (!term) continue;
        const re = new RegExp(escapeRe(term), "g");
        html = html.replace(re, m => `<span class="gloss-hit ${cls}">${m}</span>`);
      }
    };
    mark(lockedTerms, "locked");
    mark(autoTerms, "");
    mark(candidateTerms, "candidate");
    // Split on double newline OR single newline (for raws with no
    // blank-line separators).
    const paras = html.split(/\n\s*\n|\n/).filter(p => p.trim());
    return paras.map(p => `<p>${p}</p>`).join("");
  }

  const lockedTermStrings = lockedHits.map(h => h.term_zh);
  const autoTermStrings = autoHits.map(h => h.term_zh);

  function renderGlossList(candidates) {
    const rows = [];
    const totalHits = lockedHits.length + autoHits.length;
    const newCount = candidates.length;
    if (totalHits > 0) {
      rows.push(`<div class="gloss-section-head"><span>locked &amp; auto</span><span class="c">${totalHits}</span></div>`);
      for (const h of [...lockedHits, ...autoHits].slice(0, 14)) {
        const badge = h.locked ? "locked" : "auto";
        const note = h.usage_note ? `<span class="note">${escapeHtml(h.usage_note)}</span>` : "";
        rows.push(`
          <div class="gloss-item">
            <span class="han-mini">${escapeHtml(h.term_zh)}</span>
            <div class="body">
              <span class="en">${escapeHtml(h.term_en || "")}</span>
              ${note}
            </div>
            <span class="freq">×${h._count}</span>
            <span class="badge-mini ${badge}">${badge}</span>
          </div>
        `);
      }
    }
    if (newCount > 0) {
      rows.push(`<div class="gloss-section-head new"><span>new candidates</span><span class="c">${newCount}</span></div>`);
      for (const c of candidates.slice(0, 14)) {
        rows.push(`
          <div class="gloss-item">
            <span class="han-mini">${escapeHtml(c.term)}</span>
            <div class="body">
              <span class="en muted" style="color:var(--muted);font-style:italic;">(not in glossary)</span>
            </div>
            <span class="freq">×${c.count}</span>
            <span class="badge-mini new">new</span>
          </div>
        `);
      }
    }
    if (!rows.length) {
      rows.push(`<p class="muted" style="font-style:italic;">No glossary terms detected in this chapter yet.</p>`);
    }
    return rows.join("");
  }

  // Initial render — no candidates yet (we fetch them async below).
  const previewHtml = applyHighlights(preview, lockedTermStrings, autoTermStrings, []);
  const initialList = renderGlossList([]);
  const initialNewCount = 0;

  bodyEn.innerHTML = `
    <div class="cockpit">
      <div class="cockpit-pane">
        <div class="pane-head">
          <span class="pin-han">原</span>
          <span class="t">Raw source · <em>first 280 字</em></span>
          <span class="spacer"></span>
          <span class="n">${cjkCount.toLocaleString()} 字 · ${paraCount} para${paraCount === 1 ? "" : "s"}</span>
        </div>
        <div class="pane-body">
          <div class="raw-preview" id="cockpit-preview">${previewHtml}<div class="fade"></div></div>
          <div class="preview-foot">
            <span class="legend"><span class="swatch locked"></span>locked term</span>
            <span class="legend"><span class="swatch auto"></span>auto-gloss</span>
            <span class="legend"><span class="swatch cand"></span>candidate · not in glossary</span>
          </div>
        </div>
      </div>
      <div class="cockpit-pane">
        <div class="pane-head">
          <span class="pin-han jade">詞</span>
          <span class="t">Glossary preflight · <em>what'll fire</em></span>
          <span class="spacer"></span>
          <span class="n" id="cockpit-counts">${lockedHits.length + autoHits.length} hit${(lockedHits.length + autoHits.length) === 1 ? "" : "s"} · <span id="cockpit-new-count">${initialNewCount}</span> new</span>
        </div>
        <div class="pane-body">
          <div class="gloss-list" id="cockpit-gloss-list">${initialList}</div>
        </div>
      </div>
    </div>
    <div class="cockpit-cta">
      <div class="left">
        <div class="t">Translate <em>第${num}章</em> against the current glossary?</div>
      </div>
      <div class="right">
        <button class="b-primary" id="start-pending"><span class="han">譯</span>Translate now</button>
        <a class="b-ghost" href="/glossary?novel=${novelId}">Lock candidates first</a>
      </div>
    </div>
    ${zhDetailsHtml}
  `;

  // Async: fetch candidates and re-render highlights + the right pane.
  api.chapterSaturation(novelId, num).then(res => {
    const cands = res?.candidates || [];
    if (!cands.length) return;
    const candTerms = cands.map(c => c.term);
    const newPreview = applyHighlights(preview, lockedTermStrings, autoTermStrings, candTerms);
    const previewEl = document.getElementById("cockpit-preview");
    if (previewEl) previewEl.innerHTML = newPreview + '<div class="fade"></div>';
    const listEl = document.getElementById("cockpit-gloss-list");
    if (listEl) listEl.innerHTML = renderGlossList(cands);
    const newCountEl = document.getElementById("cockpit-new-count");
    if (newCountEl) newCountEl.textContent = String(cands.length);
  }).catch(() => {
    // Best-effort — the cockpit still works without candidate data.
  });
}

function renderChapterBody(ch) {
  // Glossary-term highlights are an edit-mode affordance. In read mode
  // the chapter renders as clean prose — the dotted underlines + hover
  // pills clutter a relaxed read. The cockpit's own raw preview still
  // marks terms unconditionally because the cockpit IS pre-translation
  // prep.
  const pattern = (readerMode === "edit") ? buildTermPattern() : null;
  // Masthead title layout: prefix-stripped English title in the H1
  // (the chapter index lives in the mono dateline now, not in the H1
  // string), Han subtitle directly below, Chinese-pane H1 in bilingual
  // mode also carries the Han for the parallel reading layout.
  setChapterBarTitle(ch.chapter_num, ch.title_en, ch.title_zh);
  if (ch.title_en) {
    chH1En.textContent = stripChapterPrefix(ch.title_en);
  } else {
    chH1En.textContent = stripChapterPrefix(displayTitleZh(ch.title_zh), true) || `Chapter ${ch.chapter_num}`;
  }
  if (chH1ZhSub) chH1ZhSub.textContent = stripChapterPrefix(displayTitleZh(ch.title_zh), true) || "";
  chH1Zh.textContent = displayTitleZh(ch.title_zh) || "";
  // 2026-05-27: sync translation-source toggle visibility / pressed state
  // before computing enSource so the picker reflects which body is about
  // to render. The visibility rule (both bodies non-empty) means a hidden
  // toggle + a "Free draft"-selected preference falls through to polished
  // via _displayedEnglish's guard — no extra render-path needed.
  applyTranslationSource(ch);
  const enSource = _displayedEnglish(ch);
  bodyEn.innerHTML = renderEnglishMarkdownWithTerms(enSource, pattern);
  bodyZh.innerHTML = renderParagraphsWithTerms(ch.original_text || "", "zh", pattern);
  // F14 (2026-05-25): pre-render next chapter so Next click feels
  // instant. Only fires when the current chapter is done; pending /
  // translating chapters skip (no point cacheing what isn't ready).
  if (ch.status === "done") _prefetchNext(ch.chapter_num);
  applyGlossaryMergeBanner(ch);
  applyQualityBanner(ch);
  applyRefinementBanner(ch);
  updatePaneEnLabel(ch);
  // QA dashboard (Initiative 1) — fire-and-forget; the observations panel
  // populates whenever the chapter has any persisted detect_* rows. Hidden
  // entirely when count is 0 so unflagged chapters don't see the chip.
  loadObservationsForChapter(ch.chapter_num);
  // Bookmark button highlight (Initiative 2) — cheap, reads in-memory
  // cache, runs every time the chapter switches.
  if (typeof _updateBookmarkButtonState === "function") {
    _updateBookmarkButtonState();
  }
  copyChapterBtn.disabled = !enSource;
  const words = enSource.split(/\s+/).filter(Boolean).length;
  const min = Math.max(1, Math.round(words / 230));
  endStat.textContent = `End of chapter · ${min} min read · ${words.toLocaleString()} words`;
}

function applyGlossaryMergeBanner(ch) {
  const prior = document.getElementById("glossary-merge-error-card");
  if (prior) prior.remove();
  if (!ch.glossary_merge_error) return;
  const card = document.createElement("div");
  card.id = "glossary-merge-error-card";
  card.className = "alert-banner";
  card.setAttribute("role", "alert");
  card.innerHTML = `
    <span class="msg">Glossary auto-update failed for this chapter. New terms may be missing. <span class="muted">${escapeHtml(ch.glossary_merge_error)}</span></span>
    <button type="button" class="retry" id="glossary-merge-retry">↻ Retranslate to re-extract</button>
  `;
  bodyEn.parentElement.insertBefore(card, bodyEn);
  // Click handler: delegated from bodyEn.parentElement at module init.
}

// Architectural invariant: a chapter the translator flagged degraded
// (translation_degraded=1 — the plain-text fallback path) never silently
// ships as ordinary canonical text. Banner offers Retranslate as the
// recovery action.
//
// 2026-05-26 — split copy by provider provenance:
//   * Free-tier draft only (translated_text NULL + free_draft_text set):
//     "Reading free-tier draft. Translate with [provider] for polish."
//   * Final translation came from google_translate_free (translation_degraded=1 +
//     provider_type='google_translate_free'): "Free-tier rough draft. Switch
//     to an LLM provider for polished prose."
//   * Final translation degraded for any other reason: existing
//     plain-text-fallback copy.
//   * refinement_status='done' suppresses the banner entirely (the
//     displayed text is the refined LLM output).
function applyQualityBanner(ch) {
  const priorBanner = document.getElementById("quality-banner");
  if (priorBanner) priorBanner.remove();
  bodyEn.classList.remove("chapter-degraded");
  if (ch.status === "translating") return;
  if (ch.refinement_status === "done") return;

  // Case 1: no final translation yet, but a free draft is available — the
  // reader's body has rendered the free draft as a stand-in. Surface that
  // honestly with a Translate-now affordance.
  if (!ch.translated_text && ch.free_draft_text) {
    bodyEn.classList.add("chapter-degraded");
    const card = document.createElement("div");
    card.id = "quality-banner";
    card.className = "alert-banner quality-banner";
    card.setAttribute("role", "alert");
    card.innerHTML = `
      <span class="msg">Reading free-tier draft (Google Translate, mechanical). <span class="muted">Translate with your LLM provider for polish.</span></span>
      <button type="button" class="retry" id="quality-recover">▶ Translate now</button>
    `;
    bodyEn.parentElement.insertBefore(card, bodyEn);
    _wireQualityRecover(card, ch);
    return;
  }

  if (!ch.translation_degraded) return;

  bodyEn.classList.add("chapter-degraded");
  const card = document.createElement("div");
  card.id = "quality-banner";
  card.className = "alert-banner quality-banner";
  card.setAttribute("role", "alert");

  // Case 2: the final translation ITSELF came from google_translate_free
  // (free-tier user, no LLM provider configured). Banner copy admits it's
  // a rough draft and points at switching providers — not at retranslating
  // with the same backend.
  const translatorType = providerTypeById(ch.translated_by_provider_id);
  if (translatorType === "google_translate_free") {
    card.innerHTML = `
      <span class="msg">Free-tier rough draft (Google Translate, no LLM). <span class="muted">Switch to an LLM provider in Settings → Providers for polished prose.</span></span>
      <button type="button" class="retry" id="quality-recover">↻ Retranslate</button>
    `;
    bodyEn.parentElement.insertBefore(card, bodyEn);
    _wireQualityRecover(card, ch);
    return;
  }

  // Case 3: legacy plain-text-fallback path. Same copy as before.
  card.innerHTML = `
    <span class="msg">Chapter committed via the translator's plain-text fallback. Glossary terms may be missing. <span class="muted">Retranslate to retry the structured path.</span></span>
    <button type="button" class="retry" id="quality-recover">↻ Retranslate</button>
  `;
  bodyEn.parentElement.insertBefore(card, bodyEn);
  _wireQualityRecover(card, ch);
}

function _wireQualityRecover(card, ch) {
  card.querySelector("#quality-recover").addEventListener("click", async () => {
    const btn = card.querySelector("#quality-recover");
    btn.disabled = true;
    btn.textContent = "Queuing…";
    try {
      const resp = await fetch(
        `/api/novels/${novelId}/chapters/${ch.chapter_num}/retranslate`,
        { method: "POST" }
      );
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({ detail: resp.statusText }));
        btn.disabled = false;
        btn.textContent = "↻ Retranslate";
        alert(`Retranslate refused: ${body.detail || resp.statusText}`);
        return;
      }
      // Worker is queued. loadChapter re-fetches the row; the existing poll
      // loop picks up state changes and re-renders the banner / clears it on
      // success.
      loadChapter(ch.chapter_num);
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "↻ Retry recovery";
      alert(`Recovery request failed: ${err}`);
    }
  });
}

// Find the chapter_num of the cached chapter immediately before / after
// `targetCh`, or null when at the edge. Replaces `currentCh ± 1` arithmetic
// and `chaptersCache.length` comparisons throughout nav. Works for sparse
// chapter numbers (partial imports starting at 296), gaps (deletions inside
// a novel: 1, 2, 5, 6), and prologues numbered 0. The API returns
// chaptersCache ordered by chapter_num so neighbour is just the adjacent
// index.
function neighborChapter(targetCh, direction) {
  if (!chaptersCache.length) return null;
  const idx = chaptersCache.findIndex(c => c.chapter_num === targetCh);
  if (idx < 0) return null;
  const next = chaptersCache[idx + direction];
  return next ? next.chapter_num : null;
}

function paintEndCard(ch) {
  const nextNum = neighborChapter(ch.chapter_num, +1);
  const hasNext = nextNum != null;
  nextCard.classList.toggle("disabled", !hasNext);
  if (!hasNext) {
    nextTitle.textContent = "Last chapter";
    nextStatus.textContent = "";
    nextGo.disabled = true;
    return;
  }
  const next = chaptersCache.find(c => c.chapter_num === nextNum);
  nextTitle.textContent = next
    ? (next.title_en || displayTitleZh(next.title_zh) || `Chapter ${next.chapter_num}`)
    : `Chapter ${nextNum}`;
  nextStatus.textContent = next ? next.status : "";
  nextGo.disabled = false;
}

function persistLastRead(ch) {
  if (ch.status !== "done") return;
  const displayed = _displayedEnglish(ch);
  const paras = String(displayed).split(/\n\s*\n/).map(p => p.trim()).filter(Boolean);
  const lastLine = paras.length ? paras[paras.length - 1].slice(0, 240) : null;
  localStorage.setItem(`lastRead:${novelId}`, JSON.stringify({
    ch: ch.chapter_num, ts: Date.now(), lastLine,
  }));
}

/* ---- Nav / actions ----
 * All four affordances (prev / next / nextGo / nextCard) navigate via
 * `neighborChapter` against the ordered TOC cache instead of arithmetic on
 * currentCh, so partial-import novels (chapters 296-298) and novels with
 * deletion gaps (1, 2, 5, 6) still work. The chapter that's actually next
 * may be currentCh+5, not currentCh+1. */
prevBtn.addEventListener("click", () => {
  const n = neighborChapter(currentCh, -1);
  if (n != null) loadChapter(n);
});
nextBtn.addEventListener("click", () => {
  const n = neighborChapter(currentCh, +1);
  if (n != null) loadChapter(n);
});
nextGo.addEventListener("click", () => {
  if (nextGo.disabled) return;
  const n = neighborChapter(currentCh, +1);
  if (n != null) loadChapter(n);
});
nextCard.addEventListener("click", (e) => {
  if (e.target.closest("button")) return;
  if (nextCard.classList.contains("disabled")) return;
  const n = neighborChapter(currentCh, +1);
  if (n != null) loadChapter(n);
});
rereadBtn.addEventListener("click", () => loadChapter(currentCh));

// Swap the icon for a spinner while a queue request is in flight; the
// existing chapter polling (pollHandle) will refresh the body when the
// status flips back to done.
async function runAction(btn, fn, queuedMsg) {
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinning" aria-hidden="true">⟳</span>`;
  try {
    await fn();
    statusEl.className = "status info";
    statusEl.textContent = queuedMsg;
    // Show a near-button toast so the action feels acknowledged even if the
    // user has scrolled away from the status banner.
    const rect = btn.getBoundingClientRect();
    showFloatToast("Queued", rect);
    // The action moved this chapter's state immediately on the server
    // (translate_queued=1); pull the TOC now so the queued glyph appears
    // without waiting for the 1.2s reload.
    loadChapters();
    // Reset the per-chapter backoff so the first post-action poll fires
    // at ~1.2s instead of 30s — a chapter that was stuck for >2 min and
    // is now being explicitly retried by the user shouldn't inherit its
    // old "stuck" cadence.
    clearPollStart(currentCh);
    _cancelPoll();
    pollHandle = setTimeout(() => loadChapter(currentCh), 1200);
  } catch (e) {
    statusEl.className = "status err";
    statusEl.textContent = `Failed: ${e.message}`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
}
async function _confirmPreCheck(novelId, chapter) {
  // Cheap GET — fires per click rather than caching, because the
  // chapter's pre-check inputs (length, glossary saturation) can change
  // between clicks if the user edits the source or the glossary.
  let warnings;
  try {
    const r = await api.chapterPreCheck(novelId, chapter);
    warnings = (r && r.warnings) || [];
  } catch (_e) {
    // Network failure on the pre-check doesn't block translation — the
    // user can still proceed. Quiet about it.
    return true;
  }
  if (warnings.length === 0) return true;
  const summary = warnings
    .map(w => `[${w.severity}] ${w.message}`)
    .join("\n\n");
  // Native confirm() is intentionally low-tech here — the dedicated
  // dialog would be overkill for a one-off "are you sure?" gate. The
  // user already lives with the existing browser dialogs (alert on
  // failures, etc.).
  return window.confirm(
    `${warnings.length} pre-flight check${warnings.length === 1 ? "" : "s"} flagged this chapter:\n\n${summary}\n\nTranslate anyway?`
  );
}

retranslateBtn.addEventListener("click", async () => {
  if (!await _confirmPreCheck(novelId, currentCh)) return;
  await runAction(
    retranslateBtn,
    () => api.retranslate(novelId, currentCh),
    "Re-translation queued. Refreshing when done."
  );
});

// 2026-05-27 — manual refresh of the mechanical-NMT free draft. The free
// draft is generated lazily on chapter open and otherwise has no in-app
// recompute path; if Google Translate was rate-limited or returned stale
// output, the stuck text would otherwise pollute the PEMT reference forever.
// This button clears free_draft_text and re-queues the worker.
const refreshFreeDraftBtn = document.getElementById("refresh-free-draft");
if (refreshFreeDraftBtn) {
  refreshFreeDraftBtn.addEventListener("click", async () => {
    await runAction(
      refreshFreeDraftBtn,
      () => api.refreshFreeDraft(novelId, currentCh),
      "Free draft regeneration queued. The new draft will appear shortly."
    );
  });
}

// Style-note dialog: view / edit the per-novel voice brief. The brief is
// injected into every chapter translation prompt as a voice anchor.
const styleNoteBtn = document.getElementById("style-note-btn");
const styleNoteDialog = document.getElementById("style-note-dialog");
const styleNoteText = document.getElementById("style-note-text");
const styleNoteStatus = document.getElementById("style-note-status");
const styleNoteSave = document.getElementById("style-note-save");
const styleNoteCancel = document.getElementById("style-note-cancel");
if (styleNoteBtn && styleNoteDialog) {
  styleNoteBtn.addEventListener("click", () => {
    styleNoteText.value = (novelMeta && novelMeta.style_note) || "";
    styleNoteStatus.textContent = "";
    styleNoteDialog.showModal();
  });
  styleNoteCancel.addEventListener("click", () => styleNoteDialog.close());
  styleNoteSave.addEventListener("click", async () => {
    styleNoteSave.disabled = true;
    styleNoteStatus.textContent = "Saving…";
    try {
      const updated = await api.updateNovel(novelId, {
        style_note: styleNoteText.value.trim() || null,
      });
      novelMeta = { ...novelMeta, ...updated };
      styleNoteStatus.textContent = "Saved.";
      setTimeout(() => styleNoteDialog.close(), 500);
    } catch (err) {
      styleNoteStatus.textContent = `Save failed: ${err.message || err}`;
    } finally {
      styleNoteSave.disabled = false;
    }
  });
}

// Copy the whole chapter (English title + body) to the clipboard. Reads the
// source markdown string straight from the loaded chapter so the copy matches
// the .md download shape rather than the rendered, term-highlighted DOM.
copyChapterBtn.addEventListener("click", async () => {
  const ch = lastChapter;
  if (!ch) return;
  const enTitle = ch.title_en || displayTitleZh(ch.title_zh)
    || `Chapter ${ch.chapter_num}`;
  const body = _displayedEnglish(ch);
  const text = `${enTitle}\n\n${body}`.trim();
  if (!text) return;
  await navigator.clipboard?.writeText(text);
  showFloatToast("Chapter copied", copyChapterBtn.getBoundingClientRect());
});

/* ---- Per-paragraph edit mode (style learning) ----
 * Toggling enters an in-place edit state where each paragraph of body-en
 * becomes contenteditable. On blur, if the text changed, the diff is sent
 * to /edit-paragraph which updates the stored chapter AND records a row in
 * style_edits — future translations get those edits as "preferred rewrites"
 * examples in the prompt, so the LLM learns the user's voice.
 *
 * Each editable <p> carries three dataset attributes captured at focus time:
 *   data-chapter-num    — the chapter this paragraph belongs to (locks the
 *                         save to the original chapter even if the user
 *                         clicks Next while typing).
 *   data-paragraph-index — the <p>'s position among body-en's <p> elements,
 *                         which (under the assumption that each markdown
 *                         "\\n\\n" chunk renders to one <p>) maps 1:1 to
 *                         the index in `lastChapter[col].split('\\n\\n')`.
 *   data-before-md      — the EXACT markdown chunk at that index, so the
 *                         backend can verify nothing changed under us
 *                         before applying the edit. */
let editMode = false;
const editBtn = document.getElementById("edit-mode");

function _editVariant(ch) {
  // 'refined' when the reader is currently displaying refined_text (which
  // happens iff refinement_status='done' AND refined_text is non-empty —
  // same condition as the body-picker in renderChapterBody). Otherwise
  // 'draft' for the translator's output.
  if (
    ch
    && ch.refinement_status === "done"
    && ch.refined_text
    && ch.refined_text.length > 0
  ) {
    return "refined";
  }
  return "draft";
}
function _editColumn(variant) {
  return variant === "refined" ? "refined_text" : "translated_text";
}

function _stripGlossSpans(p) {
  // Replace term-highlight spans with plain text so the contenteditable
  // surface is pure text — gloss anchor markup would otherwise be captured
  // verbatim in p.textContent.
  p.querySelectorAll("span.gloss, span.gloss-en, span.gloss-zh").forEach(s => {
    s.replaceWith(document.createTextNode(s.textContent));
  });
}

function _captureParagraphMeta(p) {
  // Compute and stash paragraph_index + before_md + chapter_num for this
  // <p>. Called on edit-mode entry and on focusin so the metadata is fresh
  // even if the DOM gets re-rendered between toggles.
  if (!lastChapter) return;
  const variant = _editVariant(lastChapter);
  const col = _editColumn(variant);
  const body = lastChapter[col] || "";
  const chunks = body.split("\n\n");
  const allPs = Array.from(bodyEn.querySelectorAll("p"));
  const idx = allPs.indexOf(p);
  if (idx < 0 || idx >= chunks.length) return;
  p.dataset.chapterNum = String(currentCh);
  p.dataset.paragraphIndex = String(idx);
  p.dataset.beforeMd = chunks[idx];
  p.dataset.variant = variant;
}

function applyEditMode() {
  editBtn?.classList.toggle("on", editMode);
  editBtn?.setAttribute("aria-pressed", editMode ? "true" : "false");
  bodyEn.classList.toggle("edit-mode", editMode);
  if (editMode) {
    bodyEn.querySelectorAll("p").forEach(p => {
      p.setAttribute("contenteditable", "true");
      _stripGlossSpans(p);
      _captureParagraphMeta(p);
    });
  } else {
    // Re-render the whole chapter body so glossary highlights come back
    // and any pre-edit markdown is reflected. The contenteditable / dataset
    // attributes vanish naturally when bodyEn.innerHTML is replaced.
    if (lastChapter) renderChapterBody(lastChapter);
  }
}

function _blurFocusedEditable() {
  // Force-blur any currently-focused contenteditable <p> so the blur
  // handler's save fires BEFORE we replace innerHTML in renderChapterBody.
  // Browsers don't reliably fire blur when a focused element is removed
  // via innerHTML replacement, so we trigger it explicitly.
  const ae = document.activeElement;
  if (ae && ae.matches && ae.matches("p[contenteditable='true']")) {
    ae.blur();
  }
}

function _exitEditMode() {
  if (!editMode) return;
  _blurFocusedEditable();
  editMode = false;
  applyEditMode();
  statusEl.className = "status";
  statusEl.textContent = "";
}

editBtn?.addEventListener("click", () => {
  // Toggling off mid-edit: force-blur the focused <p> first so its save
  // fires before applyEditMode re-renders the body.
  if (editMode) _blurFocusedEditable();
  // Section 8 (post-refinement edit support): paragraph edits route to
  // whichever body the reader is showing — refined_text when
  // refinement_status='done', translated_text otherwise. _editVariant /
  // _editColumn pick the column at edit-mode entry and at each focusin;
  // the save handler passes `source` to /edit-paragraph so the backend
  // mutates the right column.
  editMode = !editMode;
  applyEditMode();
  if (editMode) {
    statusEl.className = "status info";
    statusEl.textContent = "Edit mode on. Edit any paragraph; changes save on blur and teach future translations your style.";
  } else {
    statusEl.className = "status";
    statusEl.textContent = "";
  }
});

// Re-capture metadata on focus so a paragraph that becomes editable after a
// re-render (or that was previously the saved-flash target) carries the
// current chunk text and index. focusin bubbles; blur (capture phase) is
// what we use for the save trigger below.
bodyEn.addEventListener("focusin", (e) => {
  if (!editMode) return;
  const p = e.target.closest("p[contenteditable='true']");
  if (!p) return;
  // Refresh metadata only if missing — once captured at edit-mode entry
  // it's authoritative for the lifetime of the edit. Re-capturing on every
  // focusin would overwrite a freshly-edited p's before_md with the post-
  // edit text, breaking the next save's verification.
  if (!p.dataset.beforeMd) _captureParagraphMeta(p);
});

bodyEn.addEventListener("blur", async (e) => {
  // Intentionally NOT gated on `editMode`. When the user exits edit mode or
  // navigates away mid-edit, the focused paragraph loses focus and we still
  // want to flush the pending edit. The presence of data-before-md is the
  // authoritative "this <p> was being edited" signal.
  const p = e.target.closest("p");
  if (!p || !p.dataset.beforeMd) return;
  const beforeMd = p.dataset.beforeMd;
  const after = p.textContent.trim();
  // Use the chapter/index captured AT FOCUS, not the current globals — the
  // user may have navigated chapters while typing.
  const chapterNumAtFocus = parseInt(p.dataset.chapterNum || "", 10);
  const paragraphIndex = parseInt(p.dataset.paragraphIndex || "", 10);
  if (!Number.isFinite(chapterNumAtFocus) || !Number.isFinite(paragraphIndex)) return;
  if (!beforeMd || beforeMd.trim() === after) return;
  // Disable editing on this paragraph during the save to prevent re-fires.
  p.setAttribute("contenteditable", "false");
  p.classList.add("saving");
  // Variant was captured at focus time (paragraph metadata). It tells us
  // which column the backend should mutate AND which column to splice in
  // the local cache. Defaults to 'draft' so a paragraph that somehow lost
  // its dataset still works.
  const variant = p.dataset.variant || "draft";
  const column = variant === "refined" ? "refined_text" : "translated_text";
  try {
    await api.editParagraph(
      novelId, chapterNumAtFocus, paragraphIndex, beforeMd, after, variant,
    );
    // Update local cache the same way the backend did: split, replace index,
    // rejoin. Only when we're still on the chapter we edited — otherwise
    // lastChapter is for a different chapter now and we shouldn't touch it.
    if (lastChapter && currentCh === chapterNumAtFocus) {
      if (lastChapter[column]) {
        const chunks = lastChapter[column].split("\n\n");
        if (chunks[paragraphIndex] === beforeMd) {
          chunks[paragraphIndex] = after;
          lastChapter[column] = chunks.join("\n\n");
        }
      }
      // Re-capture so a follow-up edit on the same paragraph sees the new
      // before_md (the just-saved `after`).
      p.dataset.beforeMd = after;
    }
    p.classList.remove("saving");
    p.classList.add("saved-flash");
    setTimeout(() => p.classList.remove("saved-flash"), 1200);
  } catch (err) {
    p.classList.remove("saving");
    p.classList.add("save-failed");
    p.title = `Save failed: ${err.message}`;
  } finally {
    // Re-enable editing only when we're still in edit mode AND still on the
    // chapter this paragraph belongs to. If the user navigated, the <p> is
    // detached from DOM and this is a harmless no-op on a stale node.
    if (editMode && currentCh === chapterNumAtFocus) {
      p.setAttribute("contenteditable", "true");
    }
  }
}, true);

const shortcutsDlg = document.getElementById("shortcuts-dialog");
function openShortcuts() { if (shortcutsDlg && !shortcutsDlg.open) shortcutsDlg.showModal(); }
document.getElementById("shortcuts-btn")?.addEventListener("click", openShortcuts);
document.getElementById("shortcuts-close")?.addEventListener("click", () => shortcutsDlg.close());

/* ---- Reading-type settings (font size + line height) ----
 * Persists to localStorage; theme.js bootstraps the values on every page so
 * the choice survives navigation. CSS vars (--fs-body / --fs-body-lh) are
 * set on :root via inline style so they override the stylesheet defaults. */
const typeDlg = document.getElementById("type-settings-dialog");
const fsBodySlider = document.getElementById("fs-body-slider");
const fsLhSlider = document.getElementById("fs-lh-slider");
const fsBodyReadout = document.getElementById("fs-body-readout");
const fsLhReadout = document.getElementById("fs-lh-readout");
const focusModeToggle = document.getElementById("focus-mode-toggle");
const DEFAULT_FS_BODY = 17;
const DEFAULT_FS_LH = 1.75;
function _currentFsBody() {
  const stored = parseFloat(localStorage.getItem("readerFsBody") || "");
  return Number.isFinite(stored) ? stored : DEFAULT_FS_BODY;
}
function _currentFsLh() {
  const stored = parseFloat(localStorage.getItem("readerFsLh") || "");
  return Number.isFinite(stored) ? stored : DEFAULT_FS_LH;
}
function _syncTypeReadouts() {
  if (fsBodyReadout) fsBodyReadout.textContent = `${_currentFsBody()}px`;
  if (fsLhReadout) fsLhReadout.textContent = `${_currentFsLh().toFixed(2)}×`;
}
function _isFocusModeOn() {
  return document.documentElement.getAttribute("data-focus-mode") === "1";
}
function openTypeSettings() {
  if (!typeDlg || typeDlg.open) return;
  if (fsBodySlider) fsBodySlider.value = String(_currentFsBody());
  if (fsLhSlider) fsLhSlider.value = String(_currentFsLh());
  // Sync the focus-mode checkbox in case the attribute was changed elsewhere
  // (other tab, or initial bootstrap on first paint).
  if (focusModeToggle) focusModeToggle.checked = _isFocusModeOn();
  _syncTypeReadouts();
  typeDlg.showModal();
}
document.getElementById("type-settings-btn")?.addEventListener("click", openTypeSettings);
document.getElementById("type-settings-close")?.addEventListener("click", () => typeDlg?.close());
document.getElementById("type-settings-reset")?.addEventListener("click", () => {
  localStorage.removeItem("readerFsBody");
  localStorage.removeItem("readerFsLh");
  localStorage.removeItem("readerFocusMode");
  document.documentElement.style.removeProperty("--fs-body");
  document.documentElement.style.removeProperty("--fs-body-lh");
  document.documentElement.removeAttribute("data-focus-mode");
  if (fsBodySlider) fsBodySlider.value = String(DEFAULT_FS_BODY);
  if (fsLhSlider) fsLhSlider.value = String(DEFAULT_FS_LH);
  if (focusModeToggle) focusModeToggle.checked = false;
  _syncTypeReadouts();
});
fsBodySlider?.addEventListener("input", () => {
  const v = parseFloat(fsBodySlider.value);
  if (!Number.isFinite(v)) return;
  document.documentElement.style.setProperty("--fs-body", `${v}px`);
  localStorage.setItem("readerFsBody", String(v));
  _syncTypeReadouts();
});
fsLhSlider?.addEventListener("input", () => {
  const v = parseFloat(fsLhSlider.value);
  if (!Number.isFinite(v)) return;
  document.documentElement.style.setProperty("--fs-body-lh", String(v));
  localStorage.setItem("readerFsLh", String(v));
  _syncTypeReadouts();
});

/* Focus mode — checkbox in the type-settings dialog. The html[data-focus-mode]
 * attribute is the canonical state; the inline bootstrap in reader.html sets
 * it on page load (before stylesheet loads, to avoid a flash). This handler
 * flips the attribute when the user toggles the checkbox, and mirrors the
 * value to localStorage for the bootstrap to pick up on the next load. */
if (focusModeToggle) {
  focusModeToggle.checked = _isFocusModeOn();
  focusModeToggle.addEventListener("change", () => {
    const on = focusModeToggle.checked;
    if (on) document.documentElement.setAttribute("data-focus-mode", "1");
    else document.documentElement.removeAttribute("data-focus-mode");
    localStorage.setItem("readerFocusMode", on ? "1" : "0");
  });
}

document.addEventListener("keydown", (e) => {
  // Guard inputs so shortcuts don't fire while the user is typing in the TOC
  // search or the glossary mini-form. Modifiers are reserved for browser
  // shortcuts.
  if (e.target.matches("input, textarea, select")) return;
  if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
  if (e.key === "ArrowLeft" || e.key === "h" || e.key === "k") { e.preventDefault(); prevBtn.click(); }
  else if (e.key === "ArrowRight" || e.key === "l" || e.key === "j") { e.preventDefault(); nextBtn.click(); }
  else if (e.key === "b" || e.key === "B") {
    e.preventDefault();
    toggleDual.querySelector(`button[data-mode="${dualMode ? "english" : "bilingual"}"]`)?.click();
  }
  else if (e.key === "g" || e.key === "G") {
    e.preventDefault();
    location.href = glossaryLink.href;
  }
  else if (e.key === "/") {
    e.preventDefault();
    tocSearch.focus();
  }
  else if (e.key === "?") {
    e.preventDefault();
    openShortcuts();
  }
});

/* ---- Floating reading-rail (% through chapter) ---- */
function activeEnglishText(ch) {
  if (!ch) return "";
  return _displayedEnglish(ch);
}

function updateScrollPct() {
  const total = document.documentElement.scrollHeight - window.innerHeight;
  const pct = total <= 0 ? 0 : Math.min(100, Math.max(0, Math.round((window.scrollY / total) * 100)));
  readPct.textContent = `${pct}%`;
  const words = activeEnglishText(lastChapter).split(/\s+/).filter(Boolean).length;
  if (words > 0) {
    const min = Math.max(0, Math.round(((100 - pct) / 100) * (words / 230)));
    readEta.textContent = min > 0 ? `${min} min left` : "almost done";
  } else readEta.textContent = "";
}
// Coalesce scroll/resize bursts into one update per animation frame so the
// reading-rail recompute never runs more than ~60×/s during a fast scroll.
let scrollPctRaf = 0;
function requestScrollPct() {
  if (scrollPctRaf) return;
  scrollPctRaf = requestAnimationFrame(() => { scrollPctRaf = 0; updateScrollPct(); });
}
window.addEventListener("scroll", requestScrollPct, { passive: true });
window.addEventListener("resize", requestScrollPct);

/* ---- Sticky toolbar: collapse the duplicated chapter title ---- */
// While the big <h1> chapter heading is visible, the toolbar title just
// repeats it, so the toolbar stays minimal ("Ch. N"). Once the heading
// scrolls up under the sticky bar, .past-title reveals the toolbar title.
(() => {
  const bar = document.querySelector(".chapter-bar");
  if (!bar || !chH1En || !("IntersectionObserver" in window)) return;
  const obs = new IntersectionObserver((entries) => {
    for (const e of entries) bar.classList.toggle("past-title", !e.isIntersecting);
  }, { rootMargin: "-72px 0px 0px 0px", threshold: 0 });
  obs.observe(chH1En);
})();

/* ---- Boot ----
 * Wrapped in try/catch so a stale `ink:lastNovel` (the spine.js source of
 * truth for the Reader glyph's href) or a missing novel/chapter doesn't
 * silently halt the page mid-init and leave the user staring at the
 * "Loading…" placeholder. Three recovery paths:
 *   - NaN novelId (someone hit /reader with no ?novel=): redirect to /library.
 *   - 404/422 from loadNovel (the row was purged): clear ink:lastNovel,
 *     redirect to /library so the user lands somewhere usable.
 *   - Any other failure: surface the message in statusEl so it isn't lost.
 * loadChapter catches its own 404s internally; loadGlossary / loadProviders
 * already degrade gracefully. */
(async () => {
  if (Number.isNaN(novelId)) {
    location.replace("/library");
    return;
  }
  try {
    await loadNovel();
    await Promise.all([loadChapters(), loadGlossary(), loadProviders()]);
    // If the novel has zero chapters, loadChapter would have nothing to render
    // and the TOC skeletons would stay forever. Render an empty-state and
    // skip the chapter load — bouncing to /library would be more disruptive
    // than showing the novel that does exist but happens to be empty.
    if (chaptersCache.length === 0) {
      tocList.setAttribute("aria-busy", "false");
      tocList.innerHTML = `
        <div class="empty-state" style="padding: 24px 16px; text-align: center; color: var(--muted);">
          <div style="font-family: var(--font-family-display); font-size: 18px; margin-bottom: 6px;">No chapters yet</div>
          <div style="font-size: 12.5px;">Import some text from <a href="/?novel=${novelId}">the Import page</a> or <a href="/library">return to the library</a>.</div>
        </div>`;
      bodyEn.innerHTML = `<p class="muted">This novel has no chapters yet. <a href="/?novel=${novelId}">Import chapters</a> to begin.</p>`;
      bodyZh.innerHTML = "";
      return;
    }
    await loadChapter(currentCh);
  } catch (err) {
    const status = err && err.status;
    // Stale spine cache → /reader?novel=<dead_id> 404s the meta. Clear the
    // cache and bounce; the library is always a safe landing page.
    if (status === 404 || status === 422) {
      try { localStorage.removeItem("ink:lastNovel"); } catch (_) {}
      location.replace("/library");
      return;
    }
    // Any other error leaves the page stuck on skeleton placeholders + an
    // unhelpful statusEl message. Render a recoverable error in the main
    // pane and replace the TOC skeletons so the user isn't staring at
    // animated emptiness while they decide what to do.
    tocList.setAttribute("aria-busy", "false");
    tocList.innerHTML = `
      <div class="empty-state" style="padding: 24px 16px; text-align: center; color: var(--muted);">
        <div style="font-family: var(--font-family-display); font-size: 16px; margin-bottom: 6px;">Couldn't load this novel</div>
        <div style="font-size: 12px;">${escapeHtml(err.message || "Unknown error")}</div>
      </div>`;
    bodyEn.innerHTML = `<p class="muted">Couldn't load this novel: ${escapeHtml(err.message || "unknown error")}. <a href="/library">Back to library</a>.</p>`;
    bodyZh.innerHTML = "";
    if (statusEl) {
      statusEl.className = "status err";
      statusEl.textContent = `Couldn't load this novel: ${err.message}`;
    }
    return;
  }
  updateScrollPct();
  setInterval(() => {
    // Skip background polling when the tab is hidden — no point hitting the
    // DB for a view nobody is looking at.
    if (document.visibilityState === "visible") {
      loadChapters();
      refreshNovelMeta();
    }
  }, 6000);
  // When the tab becomes visible again, pick up any state that drifted.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      loadChapters();
      refreshNovelMeta();
    }
  });
  // Cross-tab refresh: when another tab appends chapters to this novel, pick
  // up the new total_chapters / queue counts without waiting for the 6s tick.
  // Safe to fail silently if the browser doesn't support BroadcastChannel.
  try {
    const bc = new BroadcastChannel("novel-changes");
    bc.onmessage = (e) => {
      if (e.data && e.data.novel_id === novelId) {
        loadChapters();
        refreshNovelMeta();
      }
    };
  } catch (_) { /* ignore */ }
})();

// Lightweight novel-meta refresh: only updates fields that can change without
// a full reload (total_chapters in the TOC header).
async function refreshNovelMeta() {
  try {
    const fresh = await api.novel(novelId);
    if (!fresh) return;
    const prevTotal = novelMeta?.total_chapters;
    novelMeta = { ...novelMeta, ...fresh };
    if (fresh.total_chapters !== prevTotal) {
      tocNovelMeta.textContent =
        `${fresh.total_chapters} chapters · ${fresh.source_type || ""}`;
    }
  } catch (_) { /* visible-tab poll: silent failure is fine */ }
}

// ===========================================================================
// QA dashboard (Initiative 1) — observations panel
// ===========================================================================
//
// One <dialog> for every observation kind: detect_* hits the queue worker
// stored in chapter_observations, plus the implicit translation_degraded and
// glossary_merge_error rows. The chapter bar shows a chip ⚠ N when the
// chapter has any undismissed rows; clicking opens the dialog. Dismissals
// are soft and DON'T survive a chapter retranslate (the worker's
// DELETE+INSERT in the success-commit transaction wipes the set).

let _observationsCache = []; // last fetched list for the current chapter
let _observationsChapterNum = null; // which chapter the cache belongs to

const observationsBtn = document.getElementById("observations-btn");
const observationsCount = document.getElementById("observations-count");
const observationsDialog = document.getElementById("observations-dialog");
const observationsList = document.getElementById("observations-list");
const observationsCloseBtn = document.getElementById("observations-close");

// Pretty kind labels. Anything not in this map falls through to the raw
// kind so a new observer can ship without a frontend code change.
const _OBSERVATION_KIND_LABELS = {
  missing_glossary_term: "Missing glossary term",
  missing_title_glossary_term: "Missing glossary term (title)",
  malformed_compound: "Malformed compound",
  mt_texture: "MT texture",
  double_possessive: "Double possessive",
  locked_idiom_grammar: "Locked idiom grammar",
  mid_sentence_paragraph_break: "Mid-sentence paragraph break",
  intensifier_inflation: "Intensifier inflation",
  glossary_predicate_loss: "Glossary predicate loss",
  translation_degraded: "Translation degraded",
  glossary_merge_error: "Glossary merge error",
  tm_inconsistency: "Translation drift (TM)",
  observation: "Observation",
};

function _updateObservationsBadge(count) {
  if (!observationsBtn) return;
  if (count > 0) {
    observationsBtn.hidden = false;
    if (observationsCount) observationsCount.textContent = String(count);
  } else {
    observationsBtn.hidden = true;
    if (observationsCount) observationsCount.textContent = "0";
  }
}

async function loadObservationsForChapter(chapterNum) {
  _observationsChapterNum = chapterNum;
  // Zero the badge immediately so a previous chapter's count never leaks
  // visually during the network round-trip.
  _updateObservationsBadge(0);
  _observationsCache = [];
  try {
    const list = await api.chapterObservations(novelId, chapterNum);
    // Stale-fetch guard: a fast prev/next can land an old fetch on a new
    // chapter; only apply the response if we're still on the chapter we
    // fired the fetch for.
    if (_observationsChapterNum !== chapterNum) return;
    _observationsCache = list || [];
    _updateObservationsBadge(_observationsCache.length);
    // If the dialog is open, re-render in place — the user might have just
    // dismissed something and we want the list to update.
    if (observationsDialog && observationsDialog.open) {
      _renderObservationsDialog();
    }
  } catch (e) {
    // Best-effort: never fail the chapter view because the QA panel is down.
    console.warn("observations fetch failed", e);
  }
}

function _renderObservationsDialog() {
  if (!observationsList) return;
  if (!_observationsCache.length) {
    observationsList.innerHTML =
      '<p class="observations-empty">No observations for this chapter.</p>';
    return;
  }
  const rows = _observationsCache.map(obs => {
    const label = _OBSERVATION_KIND_LABELS[obs.kind] || obs.kind;
    return `
      <div class="observation-row" data-obs-id="${obs.id}">
        <div>
          <div class="observation-kind">${escapeHtml(label)}</div>
          <div class="observation-excerpt">${escapeHtml(obs.excerpt)}</div>
        </div>
        <button type="button" class="observation-dismiss" data-dismiss="${obs.id}" title="Dismiss this observation">Dismiss</button>
      </div>
    `;
  });
  observationsList.innerHTML = rows.join("");
}

function _openObservationsDialog() {
  if (!observationsDialog) return;
  _renderObservationsDialog();
  if (!observationsDialog.open) observationsDialog.showModal();
}

if (observationsBtn) {
  observationsBtn.addEventListener("click", _openObservationsDialog);
}
if (observationsCloseBtn) {
  observationsCloseBtn.addEventListener("click", () => observationsDialog?.close());
}
// Event delegation for dismiss buttons — rows are re-rendered after every
// dismiss / chapter change, so individual handler binding would leak.
if (observationsList) {
  observationsList.addEventListener("click", async (e) => {
    const target = e.target.closest("[data-dismiss]");
    if (!target) return;
    const obsId = parseInt(target.dataset.dismiss, 10);
    if (!Number.isFinite(obsId)) return;
    target.disabled = true;
    target.textContent = "…";
    try {
      await api.dismissObservation(obsId);
      // Re-fetch so the badge + list reflect the server state precisely.
      // The chapter num the user is currently on may have shifted in the
      // meantime — loadObservationsForChapter handles its own staleness.
      if (_observationsChapterNum != null) {
        await loadObservationsForChapter(_observationsChapterNum);
      }
    } catch (err) {
      target.disabled = false;
      target.textContent = "Dismiss";
      console.warn("dismiss failed", err);
    }
  });
}

// ===========================================================================
// Bookmarks (Initiative 2)
// ===========================================================================
//
// Two dialogs:
//   * ☆ button → "Add bookmark" dialog (captures paragraph at scroll +
//     optional note → POST).
//   * ☰♡ button → "Bookmarks" panel (lists all, grouped by chapter, with
//     jump-to and delete).
// The ☆ button picks up a "has-bookmark" highlight when this chapter
// already has any bookmark, so the user can see at a glance.

const bookmarkAddBtn = document.getElementById("bookmark-add");
const bookmarksOpenBtn = document.getElementById("bookmarks-open");
const bookmarksDialog = document.getElementById("bookmarks-dialog");
const bookmarksList = document.getElementById("bookmarks-list");
const bookmarksCloseBtn = document.getElementById("bookmarks-close");
const bookmarkAddDialog = document.getElementById("bookmark-add-dialog");
const bookmarkAddNote = document.getElementById("bookmark-add-note");
const bookmarkAddContext = document.getElementById("bookmark-add-context");
const bookmarkAddCancelBtn = document.getElementById("bookmark-add-cancel");
const bookmarkAddSaveBtn = document.getElementById("bookmark-add-save");

let _bookmarksCache = []; // last fetched list for the novel
// Paragraph index captured at "Add bookmark" time. Computed from the
// topmost paragraph currently in the viewport.
let _pendingBookmarkParagraph = null;

function _currentTopParagraphIndex() {
  // The displayed body lives at #body-en; its direct children are <p>
  // elements indexed 0..N-1. Find the first one whose bounding rect's
  // bottom is below the chapter-bar (so partially-visible top paragraphs
  // don't get skipped).
  const body = document.getElementById("body-en");
  if (!body) return null;
  const paras = Array.from(body.children).filter(el => el.tagName === "P");
  if (!paras.length) return null;
  const bar = document.querySelector(".chapter-bar");
  const fold = bar ? bar.getBoundingClientRect().bottom + 8 : 0;
  for (let i = 0; i < paras.length; i++) {
    const r = paras[i].getBoundingClientRect();
    if (r.bottom > fold) return i;
  }
  return paras.length - 1;
}

function _updateBookmarkButtonState() {
  if (!bookmarkAddBtn) return;
  const hasAny = _bookmarksCache.some(b => b.chapter_num === currentCh);
  bookmarkAddBtn.classList.toggle("has-bookmark", hasAny);
}

async function loadBookmarksForNovel() {
  try {
    _bookmarksCache = (await api.bookmarks(novelId)) || [];
  } catch (e) {
    _bookmarksCache = [];
    console.warn("bookmarks fetch failed", e);
  }
  _updateBookmarkButtonState();
}

function _renderBookmarksDialog() {
  if (!bookmarksList) return;
  if (!_bookmarksCache.length) {
    bookmarksList.innerHTML =
      '<p class="bookmarks-empty">No bookmarks yet. Press ☆ on any chapter to add one.</p>';
    return;
  }
  // Group by chapter_num. Server already orders by (chapter_num,
  // paragraph_index, id), so a plain reduce preserves the order.
  const byChapter = new Map();
  for (const b of _bookmarksCache) {
    if (!byChapter.has(b.chapter_num)) byChapter.set(b.chapter_num, []);
    byChapter.get(b.chapter_num).push(b);
  }
  const parts = [];
  for (const [chNum, rows] of byChapter) {
    parts.push(`<div class="bookmark-group-head">Chapter ${chNum}</div>`);
    for (const b of rows) {
      const para = b.paragraph_index != null ? `¶${b.paragraph_index + 1}` : "…";
      const noteHtml = b.note
        ? `<div class="bookmark-note">${escapeHtml(b.note)}</div>`
        : `<div class="bookmark-note empty">(no note)</div>`;
      parts.push(`
        <div class="bookmark-row" data-bookmark-id="${b.id}" data-ch="${b.chapter_num}" data-para="${b.paragraph_index != null ? b.paragraph_index : ""}">
          ${noteHtml}
          <span class="bookmark-paragraph">${para}</span>
          <div style="display:flex;gap:4px;">
            <button type="button" class="bookmark-jump" data-jump="${b.id}">Jump</button>
            <button type="button" class="bookmark-delete" data-delete="${b.id}" title="Remove">×</button>
          </div>
        </div>`);
    }
  }
  bookmarksList.innerHTML = parts.join("");
}

async function openBookmarksDialog() {
  if (!bookmarksDialog) return;
  await loadBookmarksForNovel();
  _renderBookmarksDialog();
  if (!bookmarksDialog.open) bookmarksDialog.showModal();
}

function _scrollToParagraph(paraIndex) {
  if (paraIndex == null) return;
  const body = document.getElementById("body-en");
  if (!body) return;
  const paras = Array.from(body.children).filter(el => el.tagName === "P");
  const target = paras[paraIndex];
  if (!target) return;
  // Suppress the synthetic scroll save so the user's existing saved scroll
  // doesn't get overwritten by the jump's mid-restore state.
  _ignoreScrollFor(800);
  target.scrollIntoView({ behavior: "smooth", block: "center" });
}

if (bookmarksOpenBtn) {
  bookmarksOpenBtn.addEventListener("click", openBookmarksDialog);
}
if (bookmarksCloseBtn) {
  bookmarksCloseBtn.addEventListener("click", () => bookmarksDialog?.close());
}
// Delegated handlers for jump + delete inside the bookmarks list.
if (bookmarksList) {
  bookmarksList.addEventListener("click", async (e) => {
    const jumpEl = e.target.closest("[data-jump]");
    const delEl = e.target.closest("[data-delete]");
    if (jumpEl) {
      const row = jumpEl.closest(".bookmark-row");
      const targetCh = parseInt(row.dataset.ch, 10);
      const paraRaw = row.dataset.para;
      const para = paraRaw === "" ? null : parseInt(paraRaw, 10);
      bookmarksDialog?.close();
      if (targetCh === currentCh) {
        _scrollToParagraph(para);
      } else {
        // Navigate then jump after the body renders. Two rAFs match the
        // existing scroll-restore choreography for paint timing.
        await loadChapter(targetCh);
        requestAnimationFrame(() => requestAnimationFrame(() => _scrollToParagraph(para)));
      }
      return;
    }
    if (delEl) {
      const id = parseInt(delEl.dataset.delete, 10);
      if (!Number.isFinite(id)) return;
      delEl.disabled = true;
      try {
        await api.deleteBookmark(id);
        await loadBookmarksForNovel();
        _renderBookmarksDialog();
      } catch (err) {
        delEl.disabled = false;
        console.warn("bookmark delete failed", err);
      }
    }
  });
}

// "Add bookmark" flow.
if (bookmarkAddBtn) {
  bookmarkAddBtn.addEventListener("click", () => {
    _pendingBookmarkParagraph = _currentTopParagraphIndex();
    if (bookmarkAddContext) {
      const paraTxt = _pendingBookmarkParagraph != null
        ? `paragraph ${_pendingBookmarkParagraph + 1}`
        : "chapter-level (no paragraph)";
      bookmarkAddContext.textContent =
        `Saving to Chapter ${currentCh} · ${paraTxt}.`;
    }
    if (bookmarkAddNote) bookmarkAddNote.value = "";
    if (!bookmarkAddDialog.open) bookmarkAddDialog.showModal();
  });
}
if (bookmarkAddCancelBtn) {
  bookmarkAddCancelBtn.addEventListener("click", () => bookmarkAddDialog?.close());
}
if (bookmarkAddSaveBtn) {
  bookmarkAddSaveBtn.addEventListener("click", async () => {
    bookmarkAddSaveBtn.disabled = true;
    try {
      await api.createBookmark(novelId, currentCh, {
        paragraph_index: _pendingBookmarkParagraph,
        note: (bookmarkAddNote?.value || "").trim() || null,
      });
      bookmarkAddDialog.close();
      await loadBookmarksForNovel();
    } catch (err) {
      console.warn("bookmark create failed", err);
      bookmarkAddContext.textContent = `Save failed: ${err.message}`;
    } finally {
      bookmarkAddSaveBtn.disabled = false;
    }
  });
}

// Initial load on page open.
loadBookmarksForNovel();

// ===========================================================================
// Concordance (Initiative 5) — text-selection → TM lookup
// ===========================================================================
//
// When the user selects a non-trivial phrase inside either reader pane, a
// floating "Concordance" button appears next to the selection. Clicking it
// opens a dialog listing every TM-indexed paragraph in this novel that
// contains the phrase, with click-to-jump to the matched chapter+paragraph.

const concordanceTrigger = document.getElementById("concordance-trigger");
const concordanceDialog = document.getElementById("concordance-dialog");
const concordanceList = document.getElementById("concordance-list");
const concordanceQueryEl = document.getElementById("concordance-query");
const concordanceCloseBtn = document.getElementById("concordance-close");

const _CONCORDANCE_MIN_LEN = 2;
const _CONCORDANCE_MAX_LEN = 200;
let _concordanceSelectedText = "";

function _selectionInsideReaderPane() {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  // Selection must originate inside either body-en or body-zh — selections
  // elsewhere (chapter bar, toolbar, dialogs) shouldn't trigger concordance.
  const bodyEn = document.getElementById("body-en");
  const bodyZh = document.getElementById("body-zh");
  const container = range.commonAncestorContainer;
  const node = container.nodeType === Node.TEXT_NODE ? container.parentNode : container;
  if (!(bodyEn?.contains(node) || bodyZh?.contains(node))) return null;
  const text = sel.toString().trim();
  if (text.length < _CONCORDANCE_MIN_LEN || text.length > _CONCORDANCE_MAX_LEN) return null;
  return { text, range };
}

function _positionConcordanceTrigger(range) {
  const rects = range.getClientRects();
  if (!rects || rects.length === 0) return false;
  const last = rects[rects.length - 1];
  // Place button just below the end of the selection; nudge into view if it'd
  // clip off the bottom of the viewport.
  let top = last.bottom + 4;
  if (top + 30 > window.innerHeight) top = last.top - 30;
  let left = last.right;
  // Keep on-screen even on edge selections.
  const btnW = 130; // rough width estimate
  if (left + btnW > window.innerWidth - 8) left = window.innerWidth - btnW - 8;
  if (left < 8) left = 8;
  concordanceTrigger.style.top = `${top}px`;
  concordanceTrigger.style.left = `${left}px`;
  return true;
}

// Wireframes redesign: the standalone concordance-trigger button retired —
// concordance is now an action inside the unified .sel-pop popover (see
// showPopoverForSelection above). The element itself is kept hidden in
// the DOM with display:none so existing _selectionInsideReaderPane /
// _positionConcordanceTrigger references don't error if anything else
// touches them; _openConcordanceDialog stays in use, called by the
// popover's "concordance" action.

async function _openConcordanceDialog(query) {
  if (!concordanceDialog) return;
  concordanceQueryEl.innerHTML = `Searching for: <strong>${escapeHtml(query)}</strong>`;
  concordanceList.innerHTML = '<p class="muted">Loading…</p>';
  if (!concordanceDialog.open) concordanceDialog.showModal();
  let hits;
  try { hits = await api.tmConcordance(novelId, query); }
  catch (e) {
    concordanceList.innerHTML = `<p class="status err">Search failed: ${escapeHtml(e.message)}</p>`;
    return;
  }
  if (!hits || hits.length === 0) {
    concordanceList.innerHTML =
      '<p class="concordance-empty">No matches in the translation memory. '
      + 'Newly-translated chapters appear here once they finish translating.</p>';
    return;
  }
  concordanceList.innerHTML = hits.map(h => {
    const chTitle = h.chapter_title_en || `Chapter ${h.chapter_num}`;
    const sideTag = `<span class="concordance-matched-side">matched on ${h.matched_side === "source" ? "中文" : "EN"}</span>`;
    return `
      <div class="concordance-hit" data-ch="${h.chapter_num}" data-para="${h.paragraph_index}">
        <div>
          <div class="concordance-chapter">Ch. ${h.chapter_num} · ${escapeHtml(chTitle)}${sideTag}</div>
          <div class="concordance-source">${escapeHtml(h.source_text)}</div>
          <div class="concordance-target">${escapeHtml(h.target_text)}</div>
        </div>
        <span class="concordance-paragraph">¶${h.paragraph_index + 1}</span>
      </div>`;
  }).join("");
}

if (concordanceCloseBtn) {
  concordanceCloseBtn.addEventListener("click", () => concordanceDialog?.close());
}
// Click a hit → jump to chapter + paragraph (reuses Initiative 2's
// _scrollToParagraph helper). Same staleness handling as bookmarks.
if (concordanceList) {
  concordanceList.addEventListener("click", async (e) => {
    const row = e.target.closest(".concordance-hit");
    if (!row) return;
    const ch = parseInt(row.dataset.ch, 10);
    const para = parseInt(row.dataset.para, 10);
    if (!Number.isFinite(ch)) return;
    concordanceDialog?.close();
    if (ch === currentCh) {
      _scrollToParagraph(para);
    } else {
      await loadChapter(ch);
      requestAnimationFrame(() => requestAnimationFrame(() => _scrollToParagraph(para)));
    }
  });
}

/* ---- F22 (2026-05-25): translation attempts + last-prompt panels ----
 * Edit-mode-only diagnostics. Closes the F22 observability gap by
 * surfacing the actual prompt the LLM received and the per-attempt
 * status (parse failures, fallback paths, retry counts).
 */
const lastPromptDlg = document.getElementById("last-prompt-dialog");
const lastPromptBody = document.getElementById("last-prompt-body");
const lastPromptCopy = document.getElementById("last-prompt-copy");
const attemptsDlg = document.getElementById("attempts-dialog");
const attemptsBody = document.getElementById("attempts-body");

document.getElementById("view-last-prompt")?.addEventListener("click", async () => {
  if (!lastPromptDlg || !lastPromptBody) return;
  lastPromptBody.textContent = "Loading…";
  if (!lastPromptDlg.open) lastPromptDlg.showModal();
  try {
    const r = await api.chapterLastPrompt(novelId, currentCh);
    lastPromptBody.textContent = r.prompt || "(empty)";
  } catch (err) {
    lastPromptBody.textContent =
      err && err.status === 404
        ? "No prompt snapshot recorded for this chapter yet. Translate or retranslate to capture one."
        : `Failed to load: ${err.message}`;
  }
});

lastPromptCopy?.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(lastPromptBody?.textContent || "");
    lastPromptCopy.textContent = "Copied";
    setTimeout(() => { lastPromptCopy.textContent = "Copy"; }, 1500);
  } catch {
    // Permission / API not available — best-effort.
  }
});

lastPromptDlg?.querySelector("[data-act='cancel']")
  ?.addEventListener("click", () => lastPromptDlg.close());

document.getElementById("view-attempts")?.addEventListener("click", async () => {
  if (!attemptsDlg || !attemptsBody) return;
  attemptsBody.innerHTML = "<p class='muted'>Loading…</p>";
  if (!attemptsDlg.open) attemptsDlg.showModal();
  try {
    const rows = await api.chapterAttempts(novelId, currentCh);
    if (!rows.length) {
      attemptsBody.innerHTML =
        "<p class='muted'>No attempts recorded for this chapter yet. Translate or retranslate to capture one.</p>";
      return;
    }
    attemptsBody.innerHTML = rows.map(r => {
      const statusColor =
        r.status === "ok" ? "var(--accent)"
        : r.status === "fallback_plaintext" ? "var(--warn-fg, #b56a16)"
        : r.status === "error" ? "var(--cinnabar, #c8423a)"
        : "var(--muted)";
      const parseErr = r.parse_error
        ? `<div style="margin-top:6px; font-size:12px; color: var(--cinnabar, #c8423a);">parse error: ${escapeHtml(r.parse_error)}</div>`
        : "";
      return `
        <div style="border-bottom: 1px solid var(--border); padding: 8px 0;">
          <div style="display:flex; gap:12px; align-items:baseline;">
            <span style="font-weight:600; color:${statusColor};">${escapeHtml(r.status)}</span>
            <span class="muted" style="font-size:12px;">${escapeHtml(r.started_at || "")}</span>
            <span class="muted" style="font-size:12px;">${escapeHtml(r.model_id || "(unknown model)")}</span>
            ${r.retry_count > 0 ? `<span class="muted" style="font-size:12px;">retries: ${r.retry_count}</span>` : ""}
          </div>
          ${parseErr}
        </div>`;
    }).join("");
  } catch (err) {
    attemptsBody.innerHTML = `<p class='muted'>Failed to load: ${escapeHtml(err.message)}</p>`;
  }
});

attemptsDlg?.querySelector("[data-act='cancel']")
  ?.addEventListener("click", () => attemptsDlg.close());
