const params = new URLSearchParams(location.search);
const novelId = parseInt(params.get("novel"), 10);
// Was a chapter explicitly requested in the URL? Deep links and "continue
// from library" links carry ?ch=N and must win over the resume breadcrumb.
const hadExplicitCh = params.has("ch");
let currentCh = parseInt(params.get("ch") || "1", 10);

// Phase 6 (reader edit-mode retirement): legacy `?mode=edit` deep links (old
// bookmarks, pre-retirement worklists) redirect to the CAT editor, the single
// editing surface. location.replace does not halt script execution; the rest
// of the boot runs harmlessly while the navigation lands.
if (params.get("mode") === "edit" && !Number.isNaN(novelId)) {
  location.replace(
    `/editor?novel=${novelId}&ch=${Number.isFinite(currentCh) ? currentCh : 1}`
  );
}

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
const glossaryLink = document.getElementById("toc-glossary-link");

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
const dlRawTxt = document.getElementById("download-txt-source");
if (dlRawTxt) dlRawTxt.remove();
const dlRawMd = document.getElementById("download-md-source");
if (dlRawMd) dlRawMd.remove();
// Optional: only present after the reader.html append-chapters edit lands.
// Null-guarded so a cached-old-HTML + fresh-JS combo doesn't crash boot.
const appendChaptersLink = document.getElementById("append-chapters");
if (appendChaptersLink) appendChaptersLink.href = `/?novel=${novelId}`;
// CAT-editor deep link: seeded with the boot chapter here; loadChapter
// re-points it on every chapter change. Null-guarded like its siblings.
const openInEditorLink = document.getElementById("open-in-editor");
if (openInEditorLink) openInEditorLink.href = `/editor?novel=${novelId}&ch=${currentCh}`;

// Two-way view mode: "english" (Classic, default) and "bilingual" (ZH + EN
// side-by-side). Persisted to localStorage scoped per-novel. Storage values
// intentionally keep "english" / "bilingual" — renaming would invalidate
// every user's stored preference. UI label maps "english" → "Classic".
// (Focus mode was removed 2026-05-26; the standalone "Focus mode" chrome
// toggle in the type-settings dialog still works independently.)
// Which English body variant the reader displays for a chapter: 'refined'
// whenever refined_text is non-empty, else 'draft'. AUTHORITY:
// backend/services/segments.py::displayed_body (presence keying,
// 2026-07-31 retry-window fix: retained polish stays displayed through a
// refinement retry and after a failed retry). _displayedEnglish,
// _paragraphTextAt, and _editVariant all consume THIS helper so the
// display keying lives in exactly one frontend place.
function _displayedVariant(ch) {
  return ch && ch.refined_text ? "refined" : "draft";
}

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

// Page-turn transition (2026-06). `_pendingTurnDir` is stashed inside
// loadChapter's chapter-change guard (where currentCh still holds the OLD
// chapter) and consumed by renderChapterBody, so the animation plays on the
// real body, not the "Loading..." placeholder. Preference: off | fade | shift.
const PAGE_TURN_KEY = "readerPageTurn";
const _PAGE_TURN_FALLBACK_MS = 650;
let _pendingTurnDir = null;
let _pageTurnTimer = 0;
function _pageTurnPref() {
  const v = localStorage.getItem(PAGE_TURN_KEY);
  return (v === "off" || v === "fade" || v === "shift") ? v : "shift";
}
function _playPageTurn(direction) {
  const grid = document.getElementById("dual-grid");
  if (!grid) return;
  const pref = _pageTurnPref();
  if (pref === "off") return;
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const cls = pref === "fade" ? "turning-fade" : (direction === "prev" ? "turning-prev" : "turning-next");
  // Clear any in-flight turn first so a fast next/next can't leave two classes
  // or a parked element, then force a reflow so re-adding restarts the keyframe.
  grid.classList.remove("turning-fade", "turning-next", "turning-prev");
  if (_pageTurnTimer) { clearTimeout(_pageTurnTimer); _pageTurnTimer = 0; }
  void grid.offsetWidth;
  grid.classList.add(cls);
  const clear = () => {
    grid.classList.remove("turning-fade", "turning-next", "turning-prev");
    grid.removeEventListener("animationend", clear);
    if (_pageTurnTimer) { clearTimeout(_pageTurnTimer); _pageTurnTimer = 0; }
  };
  grid.addEventListener("animationend", clear);
  // Fallback: animation-fill-mode:both parks the grid at the hidden start-frame
  // (opacity:0) if the keyframe never paints (tab backgrounded mid-turn); the
  // timeout guarantees the resting state is always restored.
  _pageTurnTimer = setTimeout(clear, _PAGE_TURN_FALLBACK_MS);
}

let chaptersCache = [];
let glossaryCache = [];
let novelMeta = null;
// Session dismissal for the TOC failed-chapters banner. -1 = not dismissed;
// otherwise the failed-count at which the user dismissed it. The banner
// reappears only if the failed count grows beyond that (new failures), so
// dismissing a known set of errors does not nag while still surfacing fresh
// ones. Resets on reload.
let _errorBannerDismissedCount = -1;
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
const loaderCancel  = document.getElementById("chapter-loader-cancel");

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
  const next = btn.dataset.source;
  if (!VALID_SOURCES.includes(next)) return;
  if (next === translationSource) return;
  translationSource = next;
  localStorage.setItem(TRANSLATION_SOURCE_KEY, translationSource);
  applyTranslationSource(lastChapter);
  if (lastChapter) renderChapterBody(lastChapter);
});

/* Phase 6 (2026-08): the Read/Edit mode split retired. The reader is a pure
 * reading surface; every editing affordance (paragraph editing, glossary
 * popovers, terms/consistency rails, QA diagnostics, learn-from-edits) lives
 * in the CAT editor (/editor). localStorage `readerMode` and the
 * body[data-reader-mode] CSS scope are gone; `?mode=edit` URLs redirect at
 * the top of this file. */

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

