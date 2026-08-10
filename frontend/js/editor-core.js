// editor-core.js - CAT editor page: state, grid, editing loop, keyboard.
//
// Split point (Phase 4): the editor page loads ordered plain script tags,
// exactly like the reader-*.js convention (source order = concat-identical;
// top-level bindings are shared scope). THIS file loads first and owns the
// shared state (currentData, currentCh, focusSeg, editing, readOnly) and the
// editing loop; editor-assist.js loads after it and adds the assist rail,
// reading the shared state directly and listening for the CustomEvents this
// file dispatches ("editor:segments-loaded", "editor:active-seg",
// "editor:segment-updated").
//
// One fetch per chapter open: GET /api/novels/{id}/chapters/{n}/segments
// (the backend lazily builds / self-heals the segment store on that read).
// The page renders exactly what the server returns; there is no client-side
// alignment. Targets render as PLAIN TEXT (escapeHtml + pre-wrap), no
// marked/DOMPurify.
//
// Editing loop: click a target cell (or press Enter on the active row) to
// make it contenteditable; blur saves if changed (context captured at focus
// time so a late blur can't cross chapters; the reader-edit.js
// _commitParagraphSave pattern); Esc reverts without saving; Ctrl+Enter =
// save + confirm + jump to the next unconfirmed; Alt+ArrowDown/Up = save +
// move, keep editing. Confirms are optimistic (badge flips immediately,
// PATCH in the background, rollback + toast on failure). All writes ride a
// serialized promise chain so each PATCH sees the chapter_rev its
// predecessor returned. 409 = toast + re-GET + re-anchor the active row by
// source_hash.
//
// URL shape: /editor?novel=N&ch=M[&seg=K] (param names match reader-core.js
// so spine.js novel-capture works unchanged). A bare /editor?novel=N resumes
// the last-opened chapter via the localStorage breadcrumb editorLast:{N}.

const params = new URLSearchParams(location.search);
const novelId = Number.parseInt(params.get("novel"), 10);
if (!Number.isFinite(novelId)) {
  // Same bounce as the reader boot: no novel context, go pick one. Top-level
  // scripts cannot return, so the boot at the bottom of this file is gated
  // on a valid id; the statements in between are harmless while the
  // navigation lands.
  location.replace("/library");
}

const LAST_KEY = `editorLast:${novelId}`;
const FILTER_KEY = "editorFilter";
let currentCh = Number.parseInt(params.get("ch"), 10);
if (!Number.isFinite(currentCh)) {
  try {
    currentCh = JSON.parse(localStorage.getItem(LAST_KEY) || "{}").ch;
  } catch (_) { /* corrupted breadcrumb: fall through */ }
  if (!Number.isFinite(currentCh)) currentCh = 1;
}
let focusSeg = Number.parseInt(params.get("seg"), 10);
if (!Number.isFinite(focusSeg)) focusSeg = null;
// Reader -> editor paragraph handoff (Block 5, 2026-08-07): an explicit
// ?seg= always wins (it names an exact row); ?para= only applies when no
// ?seg= was given. Consumed once in renderState, then nulled out.
let pendingPara = Number.parseInt(params.get("para"), 10);
if (!Number.isFinite(pendingPara) || pendingPara < 0 || focusSeg != null) pendingPara = null;

// --- DOM handles (all ids are static in editor.html) ---
const crumbNovel = document.getElementById("editor-crumb-novel");
const crumbCh = document.getElementById("editor-crumb-ch");
const prevBtn = document.getElementById("prev-ch");
const nextBtn = document.getElementById("next-ch");
const chSelect = document.getElementById("ch-select");
const progressLabel = document.getElementById("progress-label");
const progressBar = document.getElementById("progress-bar");
const progressFill = document.getElementById("progress-fill");
const readerLink = document.getElementById("editor-reader-link");
const statusEl = document.getElementById("editor-status");
const gridHead = document.getElementById("seg-grid-head");
const grid = document.getElementById("seg-grid");
const filterToggle = document.getElementById("filter-toggle");
const confirmRestBtn = document.getElementById("confirm-rest");
const continueEl = document.getElementById("editor-continue");
const shortcutsBtn = document.getElementById("shortcuts-btn");
const shortcutsDlg = document.getElementById("shortcuts-dialog");
const shortcutsClose = document.getElementById("shortcuts-close");

crumbNovel.href = `/novel?id=${novelId}`;

let chaptersCache = []; // ChapterSummary list, ordered by chapter_num
let currentData = null; // last fetched segments payload
let pollTimer = null;
let loadSeq = 0; // stale-response guard for fast chapter switches
// Bumped every time currentData is REPLACED (any load outcome). A PATCH
// closure captures it at creation; a response whose generation moved is
// dropped, because "same chapter number" is not the same payload after a
// navigate-away-and-back round trip (see applyPatchResponse).
let dataGen = 0;
let readOnly = true; // true until a loaded chapter proves editable

// Term marks (Block 4, 2026-08-07): the glossary entries rowHtml wraps
// source/target text against, via the shared term-marks.js module. Assigned
// by editor-tools.js once loadGlossaryOnce() resolves (boot) and again after
// every glossary mutation; null until then, so early rows render plain.
let termMarkEntries = null;
function termPat() {
  return termMarkEntries && termMarkEntries.length
    ? TermMarks.buildPattern(termMarkEntries)
    : null;
}

// Active in-cell edit, or null. Captured AT EDIT START so a late blur
// saves against the right chapter/segment even after navigation:
// {chapterNum, segIndex, beforeText, chapterRev, beforeHash, chapterId,
//  cell, row}.
let editing = null;
let escCancel = false; // set by Esc so the blur handler skips the save
// Failed saves awaiting a retry: "ch:idx" -> {chapterNum, segIndex,
// after, action}. rowHtml renders a retry chip for entries of the
// current chapter, so chips survive per-row re-renders.
const failedSaves = new Map();
// Serialized PATCH chain: each write must see the chapter_rev returned
// by its predecessor, so writes never race each other. Ordering is
// load-bearing: commitSave attaches applyPatchResponse to the patch
// promise p BEFORE any successor chains onto the derived saveChain
// (p.catch), and promise handlers run in registration order, so the
// successor's run() always reads the currentData its predecessor wrote.
let saveChain = Promise.resolve();
let reloading = false; // re-entry guard for the 409 recovery reload

// --- URL + breadcrumb ---
function syncUrl() {
  const seg = focusSeg != null ? `&seg=${focusSeg}` : "";
  history.replaceState(null, "", `/editor?novel=${novelId}&ch=${currentCh}${seg}`);
}
function saveBreadcrumb() {
  try {
    localStorage.setItem(LAST_KEY, JSON.stringify({ ch: currentCh }));
  } catch (_) { /* storage full or blocked: resume just degrades */ }
}

// --- Editor bar ---
function chapterIndex(n) {
  return chaptersCache.findIndex(c => c.chapter_num === n);
}
function updateBar() {
  crumbCh.textContent = `Ch. ${currentCh}`;
  readerLink.href = `/reader?novel=${novelId}&ch=${currentCh}`;
  chSelect.value = String(currentCh);
  const i = chapterIndex(currentCh);
  prevBtn.disabled = i <= 0;
  nextBtn.disabled = i < 0 || i >= chaptersCache.length - 1;
}
// Editor -> reader paragraph handoff (Block 5, 2026-08-07): counts
// non-empty-target rows strictly before the given segment index. Companion
// to segIndexForDisplayedOrdinal below (the reverse direction); both walk
// the same "displayed paragraph" ordinal space the reader's <p> elements use.
function displayedOrdinalForSeg(idx) {
  if (!currentData || idx == null) return null;
  let count = 0;
  for (const s of currentData.segments) {
    if (s.index >= idx) break;
    if (s.target_text) count++;
  }
  return count;
}
// Rewrite the href at click time, not on every render, so it always carries
// the segment that's actually active right now. focusSeg == null (nothing
// selected) leaves the link at the plain chapter-open href updateBar set.
readerLink.addEventListener("click", (e) => {
  let href = `/reader?novel=${novelId}&ch=${currentCh}`;
  if (focusSeg != null) {
    const ordinal = displayedOrdinalForSeg(focusSeg);
    if (Number.isFinite(ordinal) && ordinal >= 0) href += `&para=${ordinal}`;
  }
  e.currentTarget.href = href;
});
function renderProgress(p) {
  progressLabel.textContent = `${p.confirmed} / ${p.total} confirmed`;
  progressBar.setAttribute("aria-valuemax", String(p.total));
  progressBar.setAttribute("aria-valuenow", String(p.confirmed));
  progressFill.style.width =
    p.total ? `${((100 * p.confirmed) / p.total).toFixed(1)}%` : "0";
}
// Idempotent confirmed count, read straight off the rows. Optimistic flips
// recompute instead of incrementing a counter, so a flip that interleaves
// with a server response (which stamps the server's own count) can never
// leave the display off by one.
function countConfirmed() {
  if (!currentData) return 0;
  return currentData.segments.filter(s => s.status === "confirmed").length;
}
function updateActions() {
  const p = currentData ? currentData.progress : null;
  confirmRestBtn.disabled =
    readOnly || !p || p.total === 0 || p.confirmed >= p.total;
  updateContinueCard();
}

// --- Continue card (Phase 5): shown when every segment is confirmed ---
let continueSeq = 0; // stale-response guard
let continueForCh = null; // chapter the visible card was fetched for
// Tear the card down and invalidate any in-flight editorNext response. Also
// called at the top of every load: the card's Read link and Continue button
// are baked to the chapter it was built for, so it must never stay painted
// across a chapter switch.
function resetContinueCard() {
  continueSeq++;
  continueForCh = null;
  continueEl.hidden = true;
  continueEl.innerHTML = "";
}
function updateContinueCard() {
  const p = currentData ? currentData.progress : null;
  const fullyConfirmed = !!p && p.total > 0 && p.confirmed >= p.total
    && currentData.chapter_status === "done";
  if (!fullyConfirmed) {
    resetContinueCard();
    return;
  }
  if (continueForCh === currentCh && !continueEl.hidden) return; // already up
  const seq = ++continueSeq;
  const forCh = currentCh;
  api.editorNext(novelId, forCh).then(r => {
    if (seq !== continueSeq || forCh !== currentCh) return;
    const next = r.next_chapter_num;
    continueForCh = forCh;
    // "Read this chapter" carries no &para=: a proofread-read starts at the
    // top of the chapter, not wherever the editor happened to be scrolled.
    const readLink = `<a class="btn-ghost" id="continue-read-link" href="/reader?novel=${novelId}&ch=${forCh}">Read this chapter</a>`;
    continueEl.innerHTML = next != null
      ? `<span class="continue-msg">Chapter fully confirmed</span>
         ${readLink}
         <button type="button" class="btn-primary" id="continue-next-btn" data-next="${next}">Continue to Ch. ${next}</button>`
      : `<span class="continue-msg">Chapter fully confirmed. Every chapter of this novel is fully confirmed.</span>
         ${readLink}`;
    continueEl.hidden = false;
  }).catch(() => {
    if (seq !== continueSeq || forCh !== currentCh) return;
    // Deliberately do NOT set continueForCh: the memo would pin this
    // buttonless fallback until a chapter switch, so leave it unset and
    // let the next updateActions() retry the editorNext fetch.
    continueEl.innerHTML =
      `<span class="continue-msg">Chapter fully confirmed</span>`;
    continueEl.hidden = false;
  });
}
continueEl.addEventListener("click", (e) => {
  const btn = e.target.closest("#continue-next-btn");
  if (!btn) return;
  gotoChapter(Number.parseInt(btn.dataset.next, 10));
});

// --- Status banner cards ---
function showStatus(kind, html) {
  statusEl.className = `status editor-status ${kind}`;
  statusEl.innerHTML = html;
  statusEl.hidden = false;
}
function clearStatus() {
  statusEl.hidden = true;
  statusEl.innerHTML = "";
}
function readerHref() {
  return `/reader?novel=${novelId}&ch=${currentCh}`;
}

// --- Segment grid ---
function statusLabel(s) {
  if (s === "edited") return "Edited";
  if (s === "confirmed") return "Confirmed";
  return "AI";
}
function segByIndex(idx) {
  if (!currentData) return null;
  return currentData.segments.find(s => s.index === idx) || null;
}
// Reader -> editor paragraph handoff (Block 5, 2026-08-07): the reader's
// paragraph ordinal counts rendered <p> elements, which is not the same
// index space as the segment ledger (Markdown non-paragraph blocks,
// single-newline raws, and merged/split alignment rows all skew the two
// orderings apart). Walks the chapter's non-empty-target segments in order
// and returns the seg index of the k-th one, clamped to the last one when k
// overshoots (null when the chapter has no non-empty rows at all).
// Deliberately approximate: the goal is to land the user near the right
// spot, never to advertise exact paragraph alignment.
function segIndexForDisplayedOrdinal(k) {
  if (!currentData || k == null || k < 0) return null;
  const nonEmpty = currentData.segments.filter(s => s.target_text);
  if (!nonEmpty.length) return null;
  return nonEmpty[Math.min(k, nonEmpty.length - 1)].index;
}
// One row's markup. Kept separate from renderSegments so a save/confirm
// re-renders a single row in place instead of blasting the whole grid.
// Term-mark spans (TermMarks.wrapText) NEVER enter contenteditable: startEdit
// resets the target cell from seg.target_text (plain data, not this markup),
// so a live edit always starts from bare text.
// The TM and "kept" chips both open the AI-suggests dialog, whose only
// action routes to revertSegment, so both carry revertSegment's own
// divergence predicate: with the stored AI rendering equal to (or absent
// behind) a machine row's text there is nothing the dialog can apply, and a
// chip that silently no-ops is worse than no chip.
function rowHtml(s) {
  const failed = failedSaves.has(`${currentCh}:${s.index}`);
  const pat = termPat();
  return `
    <div class="seg-row" data-seg="${s.index}" data-status="${escapeHtml(s.status)}"
         data-aligned="${s.aligned ? "1" : "0"}"
         data-provenance="${escapeHtml(s.origin)}">
      <span class="seg-idx">${s.index + 1}</span>
      <div class="seg-src" lang="zh">${TermMarks.wrapText(s.source_text, "zh", pat)}</div>
      <div class="seg-tgt">${
        s.target_text
          ? TermMarks.wrapText(s.target_text, "en", pat)
          : `<span class="seg-empty">(no paragraph assigned)</span>`
      }</div>
      <div class="seg-meta">
        <span class="seg-badge seg-badge-${escapeHtml(s.status)}">${statusLabel(s.status)}</span>
        ${s.origin === "tm_exact" && (s.status !== "machine" || s.machine_differs) ? `<button type="button" class="seg-chip-tm" data-act="ai-suggest" title="Pre-filled from translation memory, exact source match. Click to view the AI's own rendering.">TM</button>` : ""}
        ${s.status !== "machine" && s.machine_differs ? `<button type="button" class="seg-chip-kept" data-act="ai-suggest" title="Your text kept through retranslate; the AI suggests differently">kept</button>` : ""}
        ${s.aligned ? "" : `<span class="seg-chip-review" title="Automatic alignment could not pin this row to a single source paragraph. Check it against the source.">needs review</span>`}
        ${failed ? `<button type="button" class="seg-save-retry" data-act="retry">Save failed. Retry</button>` : ""}
        ${(s.status !== "machine" || s.machine_differs) && !readOnly ? `<button type="button" class="seg-revert" data-act="revert" title="Replace this text with the stored AI translation">Revert to AI</button>` : ""}
      </div>
    </div>`;
}
function renderSegments(data) {
  gridHead.hidden = false;
  grid.innerHTML = data.segments.map(rowHtml).join("");
}
function rowFor(idx) {
  return grid.querySelector(`.seg-row[data-seg="${idx}"]`);
}
// Replace one row's markup from its segment payload. Never touches the
// row currently being edited (that would destroy the contenteditable
// surface mid-keystroke); currentData already carries the fresh state.
function updateRow(seg) {
  if (!seg) return;
  if (editing && editing.chapterNum === currentCh && editing.segIndex === seg.index) return;
  const row = rowFor(seg.index);
  if (!row) return;
  const wasActive = row.classList.contains("active");
  row.outerHTML = rowHtml(seg);
  if (wasActive) rowFor(seg.index)?.classList.add("active");
}
function selectRow(row, { scroll = true } = {}) {
  grid.querySelectorAll(".seg-row.active").forEach(r => r.classList.remove("active"));
  row.classList.add("active");
  focusSeg = Number.parseInt(row.dataset.seg, 10);
  syncUrl();
  if (scroll) row.scrollIntoView({ block: "center", behavior: "smooth" });
  // Assist-rail hook (editor-assist.js): the active segment changed.
  document.dispatchEvent(new CustomEvent("editor:active-seg"));
}
function visibleRows() {
  return Array.from(grid.querySelectorAll(".seg-row"))
    .filter(r => r.offsetParent !== null);
}

// --- Filter (All <-> Unconfirmed), CSS class toggle, no re-fetch ---
function filterOn() {
  return localStorage.getItem(FILTER_KEY) === "unconfirmed";
}
function applyFilter() {
  const on = filterOn();
  grid.classList.toggle("filter-unconfirmed", on);
  filterToggle.setAttribute("aria-pressed", on ? "true" : "false");
}
function toggleFilter() {
  try {
    localStorage.setItem(FILTER_KEY, filterOn() ? "all" : "unconfirmed");
  } catch (_) { /* private mode: filter just won't persist */ }
  applyFilter();
  // If the active row vanished behind the filter, move to a visible one.
  const active = grid.querySelector(".seg-row.active");
  if (active && active.offsetParent === null) {
    const rows = visibleRows();
    if (rows.length) selectRow(rows[0], { scroll: false });
  }
}
filterToggle.addEventListener("click", toggleFilter);

// --- Serialized PATCH plumbing ---
// Rev + hash resolve at SEND time from currentData when the write targets
// the current chapter (so a retry after a 409 reload uses fresh guards);
// a late blur that crossed chapters falls back to the values captured at
// edit start.
function sendPatch(chapterNum, segIndex, action, after, fallback) {
  const run = () => {
    let rev = fallback ? fallback.chapterRev : null;
    let hash = fallback ? fallback.beforeHash : null;
    // Anti-renumber guard (B8): echo the chapter row id the payload loaded
    // with; the server 409s stale_chapter when a mid-novel insert
    // renumbered the list under this tab.
    let chapterId = fallback ? fallback.chapterId : null;
    if (chapterNum === currentCh && currentData) {
      rev = currentData.chapter_rev || rev;
      chapterId = currentData.chapter_id || chapterId;
      const seg = segByIndex(segIndex);
      if (seg) hash = seg.target_hash;
    }
    if (!rev || !hash) {
      return Promise.reject(new Error("segment state unavailable; reload the page"));
    }
    const body = { action, chapter_rev: rev, before_target_hash: hash };
    if (chapterId != null) body.chapter_id = chapterId;
    if (after != null) body.after_text = after;
    return api.updateSegment(novelId, chapterNum, segIndex, body);
  };
  const p = saveChain.then(run, run);
  saveChain = p.catch(() => {});
  return p;
}
// `gen` is the dataGen captured when this write's closure was created. The
// chapter-number guard alone is not enough: navigating away and back before
// a slow PATCH lands leaves chapterNum === currentCh while currentData is a
// FRESH payload, and stamping the stale rev / progress / segments_state onto
// it 409s the next save.
function applyPatchResponse(chapterNum, resp, gen) {
  if (chapterNum !== currentCh || !currentData) return;
  if (gen != null && gen !== dataGen) return;
  const i = currentData.segments.findIndex(s => s.index === resp.segment.index);
  if (i >= 0) currentData.segments[i] = resp.segment;
  currentData.chapter_rev = resp.chapter_rev;
  currentData.progress = resp.progress;
  currentData.next_unconfirmed_index = resp.next_unconfirmed_index;
  currentData.segments_state = resp.segments_state;
  renderProgress(resp.progress);
  updateRow(resp.segment);
  updateActions();
  // Assist-rail hook: glossary chips recompute when the row's text changed.
  document.dispatchEvent(new CustomEvent("editor:segment-updated", {
    detail: { index: resp.segment.index },
  }));
}
function isConflict(err) {
  return err && err.status === 409;
}
// 409 recovery: toast, re-GET, re-anchor the active row by source_hash.
// Optimistic flips are dropped by the reload; failed-save chips survive
// (their retry resolves fresh rev/hash from the reloaded payload).
async function reloadAfterConflict() {
  if (reloading) return;
  reloading = true;
  try {
    showToast("This chapter changed since the page loaded. Reloading segments.", "err");
    const activeSeg = focusSeg != null ? segByIndex(focusSeg) : null;
    const anchorHash = activeSeg ? activeSeg.source_hash : null;
    await loadSegments();
    if (anchorHash && currentData) {
      const seg = currentData.segments.find(s => s.source_hash === anchorHash);
      const row = seg ? rowFor(seg.index) : null;
      if (row) selectRow(row);
    }
  } finally {
    reloading = false;
  }
}

// --- Editing loop ---
function canEdit() {
  return !readOnly && currentData && currentData.chapter_status === "done"
    && (currentData.segments_state === "ok" || currentData.segments_state === "partial");
}
function startEdit(row) {
  if (!row || !canEdit()) return;
  if (editing) {
    if (editing.row === row) return; // already editing this row
    finishEdit({}); // flush the previous cell first
  }
  const idx = Number.parseInt(row.dataset.seg, 10);
  const seg = segByIndex(idx);
  if (!seg) return;
  selectRow(row, { scroll: false });
  const cell = row.querySelector(".seg-tgt");
  if (!cell) return;
  // Plain text only: the cell's content IS the paragraph text.
  cell.textContent = seg.target_text;
  try {
    cell.contentEditable = "plaintext-only";
  } catch (_) {
    cell.contentEditable = "true";
  }
  row.classList.add("editing");
  editing = {
    chapterNum: currentCh,
    segIndex: idx,
    beforeText: seg.target_text,
    chapterRev: currentData.chapter_rev,
    beforeHash: seg.target_hash,
    chapterId: currentData.chapter_id,
    cell,
    row,
  };
  cell.addEventListener("keydown", onEditKeydown);
  cell.focus();
  // Caret at the end (the click that started the edit landed on a
  // non-editable node, so the browser didn't place one).
  const range = document.createRange();
  range.selectNodeContents(cell);
  range.collapse(false);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}
// Ends the active edit. opts: {confirm} folds the save into
// save_and_confirm; {cancel} discards the text. Returns the save promise
// (resolved immediately when nothing changed).
function finishEdit({ confirm = false, cancel = false } = {}) {
  const ed = editing;
  if (!ed) return Promise.resolve(null);
  editing = null;
  ed.cell.removeEventListener("keydown", onEditKeydown);
  ed.cell.contentEditable = "false";
  ed.row.classList.remove("editing");
  const after = (ed.cell.textContent || "").trim();
  const changed = after !== ed.beforeText.trim();
  const stillHere = ed.chapterNum === currentCh;
  const restore = () => { if (stillHere) updateRow(segByIndex(ed.segIndex)); };
  if (cancel || !changed) {
    restore();
    return Promise.resolve(null);
  }
  if (!after) {
    showToast("A paragraph cannot be emptied. Use Revert to AI instead.", "err");
    restore();
    return Promise.resolve(null);
  }
  return commitSave(ed, after, confirm ? "save_and_confirm" : "save");
}
// The _commitParagraphSave pattern: saving state on the live cell, the
// response (or failure chip) lands wherever the row is now.
function commitSave(ed, after, action) {
  const key = `${ed.chapterNum}:${ed.segIndex}`;
  const gen = dataGen; // the payload this write was composed against
  const liveCell = ed.chapterNum === currentCh
    ? rowFor(ed.segIndex)?.querySelector(".seg-tgt")
    : null;
  liveCell?.classList.add("saving");
  return sendPatch(ed.chapterNum, ed.segIndex, action, after, ed)
    .then(resp => {
      failedSaves.delete(key);
      applyPatchResponse(ed.chapterNum, resp, gen);
      if (ed.chapterNum === currentCh) {
        const cell = rowFor(ed.segIndex)?.querySelector(".seg-tgt");
        if (cell) {
          cell.classList.remove("saving", "save-failed");
          cell.classList.add("saved-flash");
          setTimeout(() => cell.classList.remove("saved-flash"), 1200);
        }
      }
      return resp;
    })
    .catch(err => {
      if (isConflict(err) && ed.chapterNum === currentCh) {
        reloadAfterConflict();
        return null;
      }
      // Non-409 failures, and late cross-chapter 409s: a 409 whose
      // captured chapter is no longer on screen must NOT reload the
      // CURRENT chapter (spurious + misleading toast) and must NOT drop
      // the typed text. The chip is keyed to its own chapter, renders
      // when the user navigates back, and its retry resolves fresh
      // guards from the reloaded payload.
      failedSaves.set(key, {
        chapterNum: ed.chapterNum, segIndex: ed.segIndex, after, action,
      });
      if (ed.chapterNum === currentCh) {
        updateRow(segByIndex(ed.segIndex)); // renders the retry chip
        const cell = rowFor(ed.segIndex)?.querySelector(".seg-tgt");
        if (cell) {
          // Keep the user's typed text on screen; the retry chip resends
          // exactly this text.
          cell.textContent = after;
          cell.classList.add("save-failed");
        }
      }
      showToast(`Save failed: ${err.message}`, "err");
      return null;
    });
}
function retryFailedSave(idx, chip) {
  const key = `${currentCh}:${idx}`;
  const pending = failedSaves.get(key);
  if (!pending) return;
  chip.disabled = true;
  chip.textContent = "Retrying";
  commitSave(
    { chapterNum: pending.chapterNum, segIndex: pending.segIndex },
    pending.after,
    pending.action,
  ).then(resp => {
    if (!resp) {
      const c = rowFor(idx)?.querySelector(".seg-save-retry");
      if (c) { c.disabled = false; c.textContent = "Save failed. Retry"; }
    }
  });
}
function onEditKeydown(e) {
  if (!editing) return;
  if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();
    escCancel = true;
    finishEdit({ cancel: true });
    escCancel = false;
    return;
  }
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    const idx = editing.segIndex;
    const changed =
      (editing.cell.textContent || "").trim() !== editing.beforeText.trim();
    if (changed) {
      finishEdit({ confirm: true }).then(() => gotoNextUnconfirmed(idx));
    } else {
      finishEdit({});
      confirmSegment(idx, { optimistic: true, advance: true });
    }
    return;
  }
  if (e.key === "Enter" && e.shiftKey) {
    // A segment is one paragraph: Shift+Enter inserts exactly one line
    // break, never a blank line (which would split the paragraph; the
    // backend save also collapses blank-line runs as a backstop).
    e.preventDefault();
    document.execCommand("insertText", false, "\n");
    return;
  }
  if (e.key === "Enter" && !e.shiftKey) {
    // A segment is one paragraph; plain Enter saves instead of injecting
    // stray newlines into the target.
    e.preventDefault();
    finishEdit({});
    return;
  }
  if (e.altKey && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
    e.preventDefault();
    const delta = e.key === "ArrowDown" ? 1 : -1;
    const fromRow = editing.row;
    finishEdit({});
    const rows = visibleRows();
    const i = rows.indexOf(rowFor(Number.parseInt(fromRow.dataset.seg, 10)) || fromRow);
    const next = rows[i + delta];
    if (next) startEdit(next);
  }
}
// Blur saves (capture phase, like reader-edit): flushes the pending edit
// even when focus leaves for another surface. Esc sets escCancel first.
grid.addEventListener("blur", (e) => {
  if (!editing || e.target !== editing.cell) return;
  if (escCancel) return; // Esc path already handled the exit
  finishEdit({});
}, true);

// --- Confirm / unconfirm / revert ---
function nextUnconfirmedAfter(idx) {
  if (!currentData) return null;
  const segs = currentData.segments;
  const later = segs.find(s => s.index > idx && s.status !== "confirmed");
  if (later) return later.index;
  const any = segs.find(s => s.status !== "confirmed");
  return any ? any.index : null;
}
function gotoNextUnconfirmed(fromIdx) {
  const next = nextUnconfirmedAfter(fromIdx != null ? fromIdx : (focusSeg ?? -1));
  if (next == null) return;
  const row = rowFor(next);
  if (row) selectRow(row);
}
// Optimistic confirm: flip the badge + advance immediately, PATCH in the
// background, roll back + toast on failure. The chapter is captured at
// call time so a late-draining response (or rejection) after chapter nav
// can never paint the OLD chapter's fields onto the new chapter's
// same-index row (applyPatchResponse guards on it; the rollback must
// guard identically).
function confirmSegment(idx, { optimistic = true, advance = false } = {}) {
  if (!canEdit()) return;
  const chapterNum = currentCh;
  const gen = dataGen; // the payload `prev` snapshots, and the response targets
  const seg = segByIndex(idx);
  if (!seg || seg.status === "confirmed") return;
  if (!seg.target_text) {
    showToast("Write this paragraph before confirming it.", "err");
    return;
  }
  const prev = { ...seg };
  if (optimistic) {
    seg.status = "confirmed";
    currentData.progress.confirmed = countConfirmed();
    renderProgress(currentData.progress);
    updateRow(seg);
    updateActions();
    if (advance) gotoNextUnconfirmed(idx);
  }
  sendPatch(chapterNum, idx, "confirm", null, null)
    .then(resp => {
      applyPatchResponse(chapterNum, resp, gen);
      if (!optimistic && advance && chapterNum === currentCh) {
        gotoNextUnconfirmed(idx);
      }
    })
    .catch(err => {
      if (isConflict(err)) {
        if (chapterNum === currentCh) reloadAfterConflict();
        return;
      }
      // Roll back the optimistic flip, but only while still on the
      // chapter the flip happened in AND still on the payload it happened
      // to: a reload (or a navigate away and back) already discarded the
      // flip, and `prev` is a row snapshot from a payload that is gone.
      if (chapterNum === currentCh && gen === dataGen && currentData) {
        const cur = segByIndex(idx);
        if (cur) {
          Object.assign(cur, prev);
          currentData.progress.confirmed = countConfirmed();
          renderProgress(currentData.progress);
          updateRow(cur);
          updateActions();
        }
      }
      showToast(`Confirm failed: ${err.message}`, "err");
    });
}
function unconfirmSegment(idx) {
  if (!canEdit()) return;
  const chapterNum = currentCh;
  const gen = dataGen;
  const seg = segByIndex(idx);
  if (!seg || seg.status !== "confirmed") return;
  sendPatch(chapterNum, idx, "unconfirm", null, null)
    .then(resp => applyPatchResponse(chapterNum, resp, gen))
    .catch(err => {
      if (isConflict(err)) {
        if (chapterNum === currentCh) reloadAfterConflict();
      } else showToast(`Unconfirm failed: ${err.message}`, "err");
    });
}
function revertSegment(idx) {
  if (!canEdit()) return;
  const chapterNum = currentCh;
  const seg = segByIndex(idx);
  // Human rows always; machine rows only when the target diverged from the
  // stored AI rendering (a tm_exact prefill), matching the backend guard.
  if (!seg || (seg.status === "machine" && !seg.machine_differs)) return;
  confirmDialog({
    title: "Revert to AI translation?",
    body: "<p>The current text for this segment will be replaced by the AI's stored translation. This cannot be undone.</p>",
    okText: "Revert",
    danger: true,
  }).then(ok => {
    if (!ok || chapterNum !== currentCh) return;
    // Captured here, not before the dialog: a reload while the dialog was
    // open leaves the write valid (rev resolves at send time), so only the
    // generation in force when the PATCH is created matters.
    const gen = dataGen;
    sendPatch(chapterNum, idx, "revert_machine", null, null)
      .then(resp => applyPatchResponse(chapterNum, resp, gen))
      .catch(err => {
        if (isConflict(err)) {
          if (chapterNum === currentCh) reloadAfterConflict();
        } else showToast(`Revert failed: ${err.message}`, "err");
      });
  });
}

// Confirm-rest: danger-gated, then a full re-render from the server.
confirmRestBtn.addEventListener("click", () => {
  if (!currentData || confirmRestBtn.disabled) return;
  const p = currentData.progress;
  const remaining = p.total - p.confirmed;
  confirmDialog({
    title: "Confirm remaining segments?",
    body: `<p>Mark the remaining <strong>${remaining}</strong> segment${remaining === 1 ? "" : "s"} of this chapter as confirmed, including untouched AI segments.</p>`,
    okText: "Confirm all",
    danger: true,
  }).then(ok => {
    if (!ok) return;
    confirmRestBtn.disabled = true;
    api.confirmAllSegments(novelId, currentCh, {
      chapter_rev: currentData.chapter_rev,
      chapter_id: currentData.chapter_id,
    }).then(() => loadSegments())
      .catch(err => {
        if (isConflict(err)) reloadAfterConflict();
        else {
          showToast(`Confirm all failed: ${err.message}`, "err");
          updateActions();
        }
      });
  });
});

// --- Row interaction via delegation only (rows carry data-seg, no ids) ---
grid.addEventListener("click", (e) => {
  const row = e.target.closest(".seg-row");
  if (!row) return;
  const actBtn = e.target.closest("[data-act]");
  if (actBtn) {
    const idx = Number.parseInt(row.dataset.seg, 10);
    if (actBtn.dataset.act === "revert") revertSegment(idx);
    else if (actBtn.dataset.act === "retry") retryFailedSave(idx, actBtn);
    else if (actBtn.dataset.act === "ai-suggest"
             && typeof openAiSuggestion === "function") {
      // editor-assist.js provides the dialog; guard like reader-quality
      // guards its cross-module calls.
      openAiSuggestion(idx);
    }
    return;
  }
  if (editing && editing.row === row) return; // clicks inside the live edit
  const inTarget = e.target.closest(".seg-tgt");
  if (inTarget && canEdit()) {
    startEdit(row);
  } else {
    if (editing) finishEdit({});
    selectRow(row, { scroll: false });
  }
});

// --- Shortcuts dialog ---
function toggleShortcuts() {
  if (!shortcutsDlg) return;
  if (shortcutsDlg.open) shortcutsDlg.close();
  else shortcutsDlg.showModal();
}
shortcutsBtn.addEventListener("click", toggleShortcuts);
shortcutsClose.addEventListener("click", () => shortcutsDlg.close());

// --- Grid-mode keyboard map ---
function moveActive(delta) {
  const rows = visibleRows();
  if (!rows.length) return;
  const active = grid.querySelector(".seg-row.active");
  let i = active ? rows.indexOf(active) : -1;
  if (i === -1) i = delta > 0 ? -1 : 0;
  const next = rows[Math.min(rows.length - 1, Math.max(0, i + delta))];
  if (next) selectRow(next);
}
function moveActiveUnconfirmed(direction) {
  if (!currentData) return;
  const segs = currentData.segments;
  const from = focusSeg != null ? focusSeg : -1;
  const pool = direction > 0
    ? segs.filter(s => s.index > from && s.status !== "confirmed")
    : segs.filter(s => s.index < from && s.status !== "confirmed").reverse();
  const target = pool[0];
  if (!target) return;
  const row = rowFor(target.index);
  if (row) selectRow(row);
}
document.addEventListener("keydown", (e) => {
  // Same guard as command-palette: never fire while the user is typing.
  if (e.target.matches?.("input, textarea, select, [contenteditable]")) return;
  if (document.querySelector("dialog[open]")) return; // dialogs own keys
  if (e.key === "?" && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    toggleShortcuts();
    return;
  }
  if (e.ctrlKey || e.metaKey) {
    if (e.key === "Enter" && focusSeg != null) {
      e.preventDefault();
      confirmSegment(focusSeg, { optimistic: true, advance: true });
    }
    return;
  }
  if (e.altKey) return;
  switch (e.key) {
    case "j":
    case "ArrowDown":
      e.preventDefault();
      moveActive(1);
      break;
    case "k":
    case "ArrowUp":
      e.preventDefault();
      moveActive(-1);
      break;
    case "Enter": {
      const active = grid.querySelector(".seg-row.active");
      if (active) {
        e.preventDefault();
        startEdit(active);
      }
      break;
    }
    case "u":
      if (focusSeg != null) unconfirmSegment(focusSeg);
      break;
    case "n":
      moveActiveUnconfirmed(1);
      break;
    case "p":
      moveActiveUnconfirmed(-1);
      break;
    case "[":
      prevBtn.click();
      break;
    case "]":
      nextBtn.click();
      break;
    case "f":
      toggleFilter();
      break;
    case "Escape": {
      const active = grid.querySelector(".seg-row.active");
      if (active) {
        active.classList.remove("active");
        focusSeg = null;
        syncUrl();
        document.dispatchEvent(new CustomEvent("editor:active-seg"));
      }
      break;
    }
  }
});

// --- Load + state routing ---
async function loadSegments() {
  const seq = ++loadSeq;
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  if (editing) finishEdit({}); // flush before the grid disappears
  grid.setAttribute("aria-busy", "true");
  grid.innerHTML = "";
  gridHead.hidden = true;
  clearStatus();
  // Chrome that belongs to the OUTGOING chapter goes with the grid: the
  // continue card's Read link and Continue button are baked to it, and its
  // counts describe a chapter no longer on screen. renderState / the error
  // branch repaint both from the payload that arrives.
  resetContinueCard();
  renderProgress({ confirmed: 0, total: 0 });
  let data;
  try {
    data = await api.chapterSegments(novelId, currentCh);
  } catch (e) {
    if (seq !== loadSeq) return;
    currentData = null;
    dataGen++;
    readOnly = true;
    grid.setAttribute("aria-busy", "false");
    renderProgress({ confirmed: 0, total: 0 });
    updateActions();
    showStatus("err", `Could not load this chapter's segments: ${escapeHtml(e.message)}`);
    return;
  }
  if (seq !== loadSeq) return;
  currentData = data;
  dataGen++;
  grid.setAttribute("aria-busy", "false");
  renderState(data);
  // Assist-rail hook: a fresh chapter payload landed (any state).
  document.dispatchEvent(new CustomEvent("editor:segments-loaded"));
}
function renderState(data) {
  renderProgress(data.progress);
  readOnly = true;
  grid.classList.remove("read-only");
  if (data.chapter_status === "pending") {
    updateActions();
    showStatus("info",
      `This chapter is not translated yet. ` +
      `<a href="${readerHref()}">Open it in the reader</a> to translate it, ` +
      `then come back to edit the segments.`);
    return;
  }
  if (data.chapter_status === "translating") {
    updateActions();
    showStatus("info",
      `Translating this chapter now. The segment grid appears automatically when it finishes.`);
    pollTimer = setTimeout(loadSegments, 2500);
    return;
  }
  if (data.chapter_status === "error") {
    updateActions();
    showStatus("err",
      `Translation failed for this chapter. ` +
      `<a href="${readerHref()}">Open it in the reader</a> to see the error and retry.`);
    return;
  }
  if (data.segments_state === "unaligned") {
    updateActions();
    if (data.segments.length) {
      // Human work survived a failed re-alignment: show it read-only.
      showStatus("info",
        `The chapter text changed and automatic paragraph alignment failed, ` +
        `so these segments are shown read only (your edits are preserved). ` +
        `<a href="${readerHref()}">Retranslate the chapter in the reader</a> to rebuild them.`);
      grid.classList.add("read-only");
      renderSegments(data);
      applyFilter();
    } else {
      showStatus("info",
        `Automatic paragraph alignment failed for this chapter. ` +
        `Retranslate it: newer translations enforce 1:1 paragraphs. ` +
        `<a href="${readerHref()}">Open it in the reader</a> to retranslate.`);
    }
    return;
  }
  if (!(data.segments || []).length) {
    updateActions();
    showStatus("info",
      `No segments for this chapter yet. ` +
      `<a href="${readerHref()}">Open it in the reader</a> to check its text, ` +
      `then reload this page.`);
    return;
  }
  if (data.segments_state === "partial") {
    // Chapter-level signal for legacy chapters: the translation predates
    // the 1:1 paragraph contract, so some rows were paired by the length
    // aligner and flagged for review (merged groups, empty slots).
    const flagged = (data.segments || []).filter(s => !s.aligned).length;
    showStatus("info",
      `${flagged} row${flagged === 1 ? "" : "s"} could not be pinned to a ` +
      `single paragraph pair (this translation predates the segment ledger, ` +
      `so rows were matched by length). Rows marked ` +
      `<em>needs review</em> may show merged or missing English; saving a ` +
      `row clears its flag. ` +
      `<button type="button" class="linklike jump-flagged">Jump to next flagged row</button>`);
  } else {
    clearStatus();
  }
  readOnly = false;
  renderSegments(data);
  applyFilter();
  updateActions();
  // Reader -> editor paragraph handoff (Block 5, 2026-08-07): consume the
  // pending ordinal once, resolving it against the just-rendered segments.
  // pendingPara is already null when an explicit ?seg= was present (parsed
  // at module load), so this never fights that anchor.
  if (focusSeg == null && pendingPara != null) {
    const resolved = segIndexForDisplayedOrdinal(pendingPara);
    pendingPara = null;
    if (resolved != null) focusSeg = resolved;
  }
  if (focusSeg != null) {
    const row = rowFor(focusSeg);
    if (row) selectRow(row);
  }
}

// "Jump to next flagged row" in the partial-state banner: cycles through
// the aligned=0 rows from the current selection down, wrapping to the top.
statusEl.addEventListener("click", (e) => {
  if (!e.target.closest(".jump-flagged")) return;
  const rows = Array.from(grid.querySelectorAll('.seg-row[data-aligned="0"]'));
  if (!rows.length) return;
  const active = grid.querySelector(".seg-row.active");
  const from = active ? Number(active.dataset.seg) : -1;
  const next = rows.find(r => Number(r.dataset.seg) > from) || rows[0];
  selectRow(next);
});

// --- Chapter navigation ---
function gotoChapter(n) {
  if (n === currentCh) return;
  if (editing) finishEdit({}); // flush; the save is anchored to its chapter
  currentCh = n;
  focusSeg = null;
  pendingPara = null; // a para handoff is anchored to the chapter it arrived for; navigating away drops it
  syncUrl();
  saveBreadcrumb();
  updateBar();
  loadSegments();
}
prevBtn.addEventListener("click", () => {
  const i = chapterIndex(currentCh);
  if (i > 0) gotoChapter(chaptersCache[i - 1].chapter_num);
});
nextBtn.addEventListener("click", () => {
  const i = chapterIndex(currentCh);
  if (i >= 0 && i < chaptersCache.length - 1) {
    gotoChapter(chaptersCache[i + 1].chapter_num);
  }
});
chSelect.addEventListener("change", () => {
  const n = Number.parseInt(chSelect.value, 10);
  if (Number.isFinite(n)) gotoChapter(n);
});

// --- Boot ---
async function bootEditor() {
  applyFilter();
  let novel, chapters;
  try {
    [novel, chapters] = await Promise.all([
      api.novel(novelId),
      api.chapters(novelId),
    ]);
  } catch (e) {
    grid.setAttribute("aria-busy", "false");
    showStatus("err", `Could not load this novel: ${escapeHtml(e.message)}`);
    return;
  }
  document.title = `Editor · ${novel.title}`;
  crumbNovel.textContent = novel.title;
  chaptersCache = chapters;
  if (!chapters.length) {
    grid.setAttribute("aria-busy", "false");
    // This return skips syncUrl / saveBreadcrumb / updateBar, so the chrome
    // would otherwise keep its HTML defaults: nav arrows enabled but wired
    // to nothing, the Reader link still href="#", the crumb still a
    // placeholder. Point them somewhere honest instead. The chapter select
    // stays empty: there is nothing to pick.
    prevBtn.disabled = true;
    nextBtn.disabled = true;
    readerLink.href = `/reader?novel=${novelId}`;
    crumbCh.textContent = "No chapters";
    showStatus("info", "This novel has no chapters yet.");
    return;
  }
  if (chapterIndex(currentCh) < 0) {
    // The URL (or the breadcrumb) named a chapter this novel does not have.
    // Falling back bypasses gotoChapter, so drop the anchors it would have
    // dropped: ?seg= and ?para= were resolved against the chapter that was
    // asked for, and applying them to a different chapter lands the user on
    // an unrelated paragraph.
    currentCh = chapters[0].chapter_num;
    focusSeg = null;
    pendingPara = null;
  }
  chSelect.innerHTML = chapters.map(c => {
    const title = c.title_en || c.title_zh || "";
    return `<option value="${c.chapter_num}">Ch. ${c.chapter_num}${title ? ` · ${escapeHtml(title)}` : ""}</option>`;
  }).join("");
  syncUrl();
  saveBreadcrumb();
  updateBar();
  await loadSegments();
}
if (Number.isFinite(novelId)) bootEditor();
