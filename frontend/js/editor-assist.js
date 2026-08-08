// editor-assist.js - the CAT editor's assist rail (Phase 4).
//
// Loads AFTER editor-core.js (ordered plain script tags, reader-*.js
// convention): reads the shared top-level state (novelId, currentCh,
// currentData, focusSeg, editing, readOnly) and core helpers (segByIndex,
// rowFor, startEdit, canEdit, revertSegment) directly, and reacts to the
// CustomEvents core dispatches ("editor:segments-loaded",
// "editor:active-seg", "editor:segment-updated").
//
// Three panels for the active segment:
//   - TM matches: exact (same source, other chapters; confirmed first) and
//     fuzzy (near-duplicate sources, 0.80 threshold) from
//     GET .../segments/{i}/assist. Debounced ~150ms on active-row change,
//     prefetches the next row, stale-guarded by a sequence counter.
//   - Glossary chips: computed CLIENT-side from the cached api.glossary list
//     (terms whose Chinese appears in the source paragraph; warn chip when
//     the English is missing from the target). Zero extra backend.
//   - The "AI suggests" dialog for kept rows: shows the stored machine_text
//     (served by the assist endpoint so the list payload stays lean);
//     Apply routes through core's revertSegment (confirmDialog-gated).
//
// Keyboard: 'a' toggles the rail (persisted in localStorage editorAssistOpen,
// default open); Alt+1..3 applies the nth TM match into the active editing
// cell (replaces the text, stays editing).

const assistRail = document.getElementById("assist-rail");
const assistToggleBtn = document.getElementById("assist-toggle");
const assistCloseBtn = document.getElementById("assist-close");
const assistTmEl = document.getElementById("assist-tm");
const assistGlossaryEl = document.getElementById("assist-glossary");
const aiDialog = document.getElementById("ai-suggestion-dialog");
const aiDialogNote = document.getElementById("ai-suggestion-note");
const aiDialogText = document.getElementById("ai-suggestion-text");
const aiDialogApply = document.getElementById("ai-suggestion-apply");
const aiDialogClose = document.getElementById("ai-suggestion-close");

const ASSIST_OPEN_KEY = "editorAssistOpen";
let assistOpen = localStorage.getItem(ASSIST_OPEN_KEY) !== "0"; // default open
let assistSeq = 0; // stale-response guard
let assistDebounce = null;
let assistMatches = []; // flattened target texts, exact first (Alt+1..3)
const assistCache = new Map(); // "ch:idx" -> assist payload
let glossaryPromise = null; // one api.glossary fetch per page load

function applyAssistVisibility() {
  assistRail.hidden = !assistOpen;
  assistToggleBtn.setAttribute("aria-pressed", assistOpen ? "true" : "false");
  document.body.classList.toggle("assist-open", assistOpen);
}
function setAssistOpen(on) {
  assistOpen = on;
  try {
    localStorage.setItem(ASSIST_OPEN_KEY, on ? "1" : "0");
  } catch (_) { /* private mode: the pick just won't persist */ }
  applyAssistVisibility();
  if (on) scheduleAssist();
}
assistToggleBtn.addEventListener("click", () => setAssistOpen(!assistOpen));
assistCloseBtn.addEventListener("click", () => setAssistOpen(false));

function fetchAssist(ch, idx) {
  const key = `${ch}:${idx}`;
  if (assistCache.has(key)) return Promise.resolve(assistCache.get(key));
  return api.segmentAssist(novelId, ch, idx).then(d => {
    assistCache.set(key, d);
    return d;
  });
}
function loadGlossaryOnce() {
  if (!glossaryPromise) {
    glossaryPromise = api.glossary(novelId).catch(() => []);
  }
  return glossaryPromise;
}

function scheduleAssist() {
  if (!assistOpen) return;
  if (assistDebounce) clearTimeout(assistDebounce);
  assistDebounce = setTimeout(loadAssist, 150);
}

function assistIdle() {
  assistMatches = [];
  assistTmEl.innerHTML =
    `<p class="assist-empty">Select a segment to see matching translations from the rest of this novel.</p>`;
  assistGlossaryEl.innerHTML =
    `<p class="assist-empty">Glossary terms for the selected segment appear here.</p>`;
}

async function loadAssist() {
  const seq = ++assistSeq;
  const seg = focusSeg != null && currentData ? segByIndex(focusSeg) : null;
  if (!seg) {
    assistIdle();
    return;
  }
  const ch = currentCh;
  const idx = seg.index;
  renderGlossaryChips(seg);
  let data;
  try {
    data = await fetchAssist(ch, idx);
  } catch (e) {
    if (seq !== assistSeq) return;
    assistMatches = [];
    assistTmEl.innerHTML =
      `<p class="assist-empty">Could not load matches: ${escapeHtml(e.message)}</p>`;
    return;
  }
  if (seq !== assistSeq || ch !== currentCh || idx !== focusSeg) return;
  renderTmMatches(data);
  // Prefetch the next row so j / Ctrl+Enter flows feel instant.
  const next = currentData
    ? currentData.segments.find(s => s.index > idx)
    : null;
  if (next) fetchAssist(ch, next.index).catch(() => { /* prefetch only */ });
}

function matchCard(n, label, source, target, meta) {
  const src = source
    ? `<div class="assist-match-src" lang="zh">${escapeHtml(source)}</div>`
    : "";
  const applyBtn = !readOnly
    ? `<button type="button" class="assist-apply" data-assist-apply="${n}" title="Apply this translation to the active segment (Alt+${n + 1} while editing)">Apply</button>`
    : "";
  return `
    <div class="assist-match">
      <div class="assist-match-head">
        <span class="assist-match-label">${label}</span>
        <span class="assist-match-meta">${escapeHtml(meta)}</span>
        ${applyBtn}
      </div>
      ${src}
      <div class="assist-match-tgt">${escapeHtml(target)}</div>
    </div>`;
}

function renderTmMatches(data) {
  assistMatches = [];
  const cards = [];
  (data.tm_exact || []).forEach(m => {
    const n = assistMatches.length;
    assistMatches.push(m.target_text);
    cards.push(matchCard(
      n, `<span class="assist-chip assist-chip-exact">exact</span>`,
      null, m.target_text,
      `Ch. ${m.chapter_num} · ${m.status}`,
    ));
  });
  (data.tm_fuzzy || []).forEach(m => {
    const n = assistMatches.length;
    assistMatches.push(m.target_text);
    cards.push(matchCard(
      n, `<span class="assist-chip assist-chip-fuzzy">${Math.round(m.score * 100)}%</span>`,
      m.source_text, m.target_text,
      `Ch. ${m.chapter_num}`,
    ));
  });
  assistTmEl.innerHTML = cards.length
    ? cards.join("")
    : `<p class="assist-empty">No matches for this paragraph yet. Exact and near matches from segments you confirm in other chapters appear here.</p>`;
}

function renderGlossaryChips(seg) {
  loadGlossaryOnce().then(entries => {
    // Re-resolve: the active segment may have moved while the list loaded.
    const cur = focusSeg != null && currentData ? segByIndex(focusSeg) : null;
    if (!cur || cur.index !== seg.index) return;
    const chips = [];
    for (const g of entries || []) {
      const zhs = String(g.term_zh || "").split("/")
        .map(s => s.trim()).filter(Boolean);
      const hit = zhs.find(z => cur.source_text.includes(z));
      if (!hit) continue;
      const en = String(g.term_en || "");
      const present = en && (cur.target_text || "")
        .toLowerCase().includes(en.toLowerCase());
      chips.push(
        `<button type="button" class="assist-term${present ? "" : " assist-term-missing"}"
              data-entry-id="${g.id}"
              title="${present
                ? "Rendered in this paragraph"
                : "Expected English is missing from this paragraph"}">
          <span lang="zh">${escapeHtml(hit)}</span> → ${escapeHtml(en)}
        </button>`,
      );
    }
    assistGlossaryEl.innerHTML = chips.length
      ? chips.join("")
      : `<p class="assist-empty">No glossary terms in this paragraph.</p>`;
  });
}

// Apply a TM match: replace the active editing cell's text and stay editing;
// from grid mode, open the edit first (canEdit-gated).
function applyAssistMatch(i) {
  const text = assistMatches[i];
  if (text == null) return;
  if (!editing) {
    const row = focusSeg != null ? rowFor(focusSeg) : null;
    if (!row || !canEdit()) return;
    startEdit(row);
  }
  if (!editing) return;
  editing.cell.textContent = text;
  editing.cell.focus();
  const range = document.createRange();
  range.selectNodeContents(editing.cell);
  range.collapse(false);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}
assistTmEl.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-assist-apply]");
  if (!btn) return;
  applyAssistMatch(Number.parseInt(btn.dataset.assistApply, 10));
});

// Glossary chips (Block 6, 2026-08-07): click opens the revise form.
// editor-tools.js (which defines openTermForm) loads AFTER this file, but
// the click itself happens long after boot, so the guard only ever matters
// on a race at first paint.
assistGlossaryEl.addEventListener("click", async (e) => {
  const chip = e.target.closest(".assist-term");
  if (!chip) return;
  if (typeof openTermForm !== "function") return;
  const id = Number.parseInt(chip.dataset.entryId, 10);
  if (!Number.isFinite(id)) return;
  const entries = await loadGlossaryOnce();
  const entry = (entries || []).find(g => g.id === id);
  if (entry) openTermForm(entry, null);
});

// --- "AI suggests" dialog (kept chip on human rows) ---
let aiDialogIdx = null;
function openAiSuggestion(idx) {
  aiDialogIdx = idx;
  aiDialogApply.disabled = true;
  // Contextual note: kept human rows vs TM-prefilled machine rows.
  const seg = segByIndex(idx);
  aiDialogNote.textContent = seg && seg.status !== "machine"
    ? "Your text was kept through the last retranslate. The AI's current rendering of this paragraph:"
    : "This segment was pre-filled from translation memory. The AI's own rendering of this paragraph:";
  aiDialogText.textContent = "Loading the stored AI translation...";
  aiDialog.showModal();
  fetchAssist(currentCh, idx).then(d => {
    if (aiDialogIdx !== idx || !aiDialog.open) return;
    if (d.machine_text) {
      aiDialogText.textContent = d.machine_text;
      aiDialogApply.disabled = readOnly;
    } else {
      aiDialogText.textContent =
        "No AI translation is stored for this segment.";
    }
  }).catch(e => {
    if (aiDialogIdx !== idx || !aiDialog.open) return;
    aiDialogText.textContent = `Could not load the AI translation: ${e.message}`;
  });
}
aiDialogApply.addEventListener("click", () => {
  const idx = aiDialogIdx;
  aiDialog.close();
  // Core's revert action: confirmDialog-gated revert_machine PATCH.
  if (idx != null) revertSegment(idx);
});
aiDialogClose.addEventListener("click", () => aiDialog.close());

// --- Keyboard: 'a' toggles the rail; Alt+1..3 applies the nth match ---
document.addEventListener("keydown", (e) => {
  if (e.altKey && !e.ctrlKey && !e.metaKey
      && (e.key === "1" || e.key === "2" || e.key === "3")) {
    // Only while editing: the match lands in the live cell (spec'd shape).
    if (!editing) return;
    e.preventDefault();
    applyAssistMatch(Number.parseInt(e.key, 10) - 1);
    return;
  }
  if (e.key === "a" && !e.ctrlKey && !e.metaKey && !e.altKey) {
    // Same guards as core's grid-mode map: never while typing or in a dialog.
    if (e.target.matches?.("input, textarea, select, [contenteditable]")) return;
    if (document.querySelector("dialog[open]")) return;
    e.preventDefault();
    setAssistOpen(!assistOpen);
  }
});

// --- Core hooks ---
document.addEventListener("editor:active-seg", scheduleAssist);
document.addEventListener("editor:segments-loaded", () => {
  // A fresh payload may follow a retranslate or an out-of-band edit; drop
  // the per-chapter cache so matches and machine_text reflect the new rows.
  assistCache.clear();
  scheduleAssist();
});
document.addEventListener("editor:segment-updated", (e) => {
  if (!assistOpen) return;
  // A save changed the target text: the row's assist payload (machine_text
  // stays, but exact matches elsewhere are unaffected) is still valid; only
  // the glossary warn-chips depend on the target, so recompute those.
  if (e.detail && e.detail.index === focusSeg) {
    const seg = segByIndex(focusSeg);
    if (seg) renderGlossaryChips(seg);
  }
});

// --- Boot ---
applyAssistVisibility();
assistIdle();
