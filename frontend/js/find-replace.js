/* Project-wide find/replace page (Initiative 4).
 *
 * Flow: pick scope + flags → Preview → Apply.
 * The preview returns a token; the commit POST sends that token. If the
 * DB has drifted since the preview, the server refuses and the user
 * has to re-preview against the new state.
 */

const form = document.getElementById("fr-form");
const findEl = document.getElementById("fr-find");
const replaceEl = document.getElementById("fr-replace");
const scopeKindEl = document.getElementById("fr-scope-kind");
const novelSelectEl = document.getElementById("fr-novel");
const targetEl = document.getElementById("fr-target");
const regexEl = document.getElementById("fr-regex");
const caseEl = document.getElementById("fr-case");
const wordBoundaryEl = document.getElementById("fr-word-boundary");
const previewBtn = document.getElementById("fr-preview-btn");
const clearBtn = document.getElementById("fr-clear-btn");
const commitBtn = document.getElementById("fr-commit-btn");
const cancelBtn = document.getElementById("fr-cancel-btn");
const previewSection = document.getElementById("fr-preview");
const previewSummary = document.getElementById("fr-preview-summary");
const previewTruncated = document.getElementById("fr-preview-truncated");
const previewRows = document.getElementById("fr-preview-rows");
let currentToken = null;

// showToast is window.showToast from utils.js (audit 6.6).

/* ---- I4: client-side regex validation ---- */
const regexErrorEl = document.createElement("div");
regexErrorEl.className = "fr-regex-error";
regexErrorEl.setAttribute("aria-live", "polite");
findEl.parentElement.appendChild(regexErrorEl);

function validateRegex() {
  if (!regexEl.checked || !findEl.value) {
    findEl.classList.remove("invalid");
    regexErrorEl.textContent = "";
    previewBtn.disabled = false;
    return true;
  }
  try {
    new RegExp(findEl.value);
    findEl.classList.remove("invalid");
    regexErrorEl.textContent = "";
    previewBtn.disabled = false;
    return true;
  } catch (e) {
    findEl.classList.add("invalid");
    regexErrorEl.textContent = `Invalid regex: ${e.message}`;
    previewBtn.disabled = true;
    return false;
  }
}

findEl.addEventListener("input", validateRegex);
regexEl.addEventListener("change", validateRegex);

/* ---- Novel selector ---- */
async function loadNovels() {
  try {
    const novels = await api.novels();
    novelSelectEl.innerHTML =
      '<option value="">Pick a novel…</option>' +
      novels.map(n => `<option value="${n.id}">${escapeHtml(n.title)}</option>`).join("");
  } catch (e) {
    novelSelectEl.innerHTML = `<option value="">Load failed: ${escapeHtml(e.message)}</option>`;
  }
}
loadNovels();

scopeKindEl.addEventListener("change", () => {
  novelSelectEl.disabled = scopeKindEl.value !== "novel";
  if (novelSelectEl.disabled) novelSelectEl.value = "";
  loadHistory();
});

novelSelectEl.addEventListener("change", () => {
  loadHistory();
});

/* ---- Build the request body ---- */
function buildRequest() {
  const targetVal = targetEl.value;
  const target_cols =
    targetVal === "both" ? ["translated_text", "refined_text"]
    : [targetVal];
  const scopeKind = scopeKindEl.value;
  const scope_ids = scopeKind === "novel" && novelSelectEl.value
    ? [Number(novelSelectEl.value)]
    : [];
  if (scopeKind === "novel" && scope_ids.length === 0) {
    throw new Error("Pick a novel for novel-scoped find/replace.");
  }
  return {
    find: findEl.value,
    replacement: replaceEl.value,
    scope_kind: scopeKind,
    scope_ids,
    target_cols,
    use_regex: regexEl.checked,
    case_sensitive: caseEl.checked,
    word_boundary: wordBoundaryEl.checked,
  };
}

/* ---- Preview ---- */
form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  let body;
  try { body = buildRequest(); }
  catch (e) { showToast(e.message, "err"); return; }
  previewBtn.disabled = true;
  previewBtn.textContent = "Previewing…";
  try {
    const result = await api.findPreview(body);
    currentToken = result.token;
    renderPreview(result);
  } catch (e) {
    showToast(`Preview failed: ${e.message}`, "err");
    currentToken = null;
    previewSection.hidden = true;
  } finally {
    previewBtn.disabled = false;
    previewBtn.textContent = "Preview matches";
  }
});

function renderPreview(result) {
  previewSection.hidden = false;
  const totals = `${result.total_chapters} chapter${result.total_chapters === 1 ? "" : "s"} matched · `
    + `${result.total_hits_translated} hit${result.total_hits_translated === 1 ? "" : "s"} in draft`
    + `, ${result.total_hits_refined} in refined`;
  previewSummary.textContent = totals;
  previewTruncated.innerHTML = result.truncated
    ? `<div class="truncated">Result truncated. Only the first chapters are shown. Narrow the scope or use a more specific search string.</div>`
    : "";
  if (!result.rows.length) {
    previewRows.innerHTML = `<p class="muted">No matches.</p>`;
    commitBtn.disabled = true;
    return;
  }
  commitBtn.disabled = false;
  previewRows.innerHTML = result.rows.map(r => {
    const titleBits = [];
    if (r.novel_title) titleBits.push(escapeHtml(r.novel_title));
    titleBits.push(`Ch. ${r.chapter_num}`);
    if (r.chapter_title_en) titleBits.push(escapeHtml(r.chapter_title_en));
    const samples = r.sample_lines.map(s => `<div class="sample">${escapeHtml(s)}</div>`).join("");
    return `
      <div class="row">
        <span class="chapter">${titleBits.join(" · ")}</span>
        <span class="counts">${r.hits_translated} hit${r.hits_translated === 1 ? "" : "s"} (draft) · ${r.hits_refined} (refined)</span>
        ${samples}
      </div>`;
  }).join("");
}

/* ---- Commit ---- */
const undoBar = document.getElementById("fr-undo-bar");
const undoBtn = document.getElementById("fr-undo-btn");
const undoMsg = document.getElementById("fr-undo-msg");
const undoTimer = document.getElementById("fr-undo-timer");
let _undoExpiryTimer = null;
let _undoTickTimer = null;
let _undoSnapshotIds = [];

function _hideUndoBar() {
  undoBar.hidden = true;
  if (_undoExpiryTimer) { clearTimeout(_undoExpiryTimer); _undoExpiryTimer = null; }
  if (_undoTickTimer) { clearInterval(_undoTickTimer); _undoTickTimer = null; }
  _undoSnapshotIds = [];
}

/* Shows the undo bar for any scope.
 * snapshotIds: array of snapshot ids from the commit response.
 * summaryText: the human-readable summary string to display.
 */
function _showUndoBar(snapshotIds, summaryText) {
  if (!snapshotIds || !snapshotIds.length) return;
  _hideUndoBar();
  _undoSnapshotIds = snapshotIds.slice();
  const now = Date.now();
  const expiresAt = now + 10 * 60_000;
  undoMsg.textContent = summaryText;
  undoBar.hidden = false;
  _undoExpiryTimer = setTimeout(_hideUndoBar, 10 * 60_000);
  const tick = () => {
    const remaining = Math.max(0, Math.floor((expiresAt - Date.now()) / 60_000));
    undoTimer.textContent = remaining > 0 ? `· ${remaining} min left` : "· expiring";
  };
  tick();
  _undoTickTimer = setInterval(tick, 30_000);
}

undoBtn.addEventListener("click", async () => {
  if (!_undoSnapshotIds.length) return;
  undoBtn.disabled = true;
  undoBtn.textContent = "Reverting…";
  const ids = _undoSnapshotIds.slice();
  try {
    let restored = 0;
    const failures = [];
    for (const id of ids) {
      try { await api.restoreFrSnapshot(id); restored++; }
      catch (e) { failures.push(e.message); }
    }
    if (failures.length) {
      showToast(`Reverted ${restored} snapshot(s). Error on ${failures.length}: ${failures[0]}`, "err");
    } else {
      showToast(`Reverted ${restored} snapshot(s).`, "ok");
    }
    _hideUndoBar();
    loadHistory();
  } finally {
    undoBtn.disabled = false;
    undoBtn.textContent = "↺ Undo this batch";
  }
});

commitBtn.addEventListener("click", async () => {
  if (!currentToken) {
    showToast("No active preview. Run Preview first.", "err");
    return;
  }

  // Fix A: require confirmation when scope is not a single novel.
  const isGlobalScope = scopeKindEl.value !== "novel";
  if (isGlobalScope) {
    const summaryText = previewSummary ? previewSummary.textContent : "all matched chapters";
    const ok = await confirmDialog({
      title: "Apply across every novel?",
      body: `<p>This will modify ${escapeHtml(summaryText)}.</p>` +
            `<p class="muted">One restore snapshot is written per novel. You can undo for 10 minutes afterwards.</p>`,
      okText: "Apply to all novels",
      danger: true,
    });
    if (!ok) return;
  }

  commitBtn.disabled = true;
  commitBtn.textContent = "Applying…";
  try {
    const result = await api.findReplaceCommit(currentToken);
    const summary = `Applied to ${result.chapters_updated} chapter${result.chapters_updated === 1 ? "" : "s"} · `
      + `${result.rows_updated_translated} draft rows, ${result.rows_updated_refined} refined rows updated.`;
    showToast(summary, "ok");
    _hideUndoBar();
    // Fix A: undo bar now works for all scopes via snapshot_ids from the response.
    const ids = Array.isArray(result.snapshot_ids) ? result.snapshot_ids : [];
    _showUndoBar(ids, summary);
    currentToken = null;
    previewSection.hidden = true;
    loadHistory();
  } catch (e) {
    if (e.message && e.message.startsWith("410")) {
      showToast("Preview expired. Re-run Preview before applying.", "err");
      currentToken = null;
      previewSection.hidden = true;
    } else if (e.message && e.message.startsWith("409")) {
      showToast("Some chapters changed since the preview. Re-run Preview against the new state.", "err");
      currentToken = null;
      previewSection.hidden = true;
    } else {
      showToast(`Apply failed: ${e.message}`, "err");
    }
  } finally {
    commitBtn.disabled = false;
    commitBtn.textContent = "Apply changes";
  }
});

cancelBtn.addEventListener("click", () => {
  currentToken = null;
  previewSection.hidden = true;
});

clearBtn.addEventListener("click", () => {
  findEl.value = "";
  replaceEl.value = "";
  scopeKindEl.value = "all";
  novelSelectEl.value = "";
  novelSelectEl.disabled = true;
  targetEl.value = "both";
  regexEl.checked = false;
  caseEl.checked = true;
  wordBoundaryEl.checked = false;
  currentToken = null;
  previewSection.hidden = true;
});

/* ---- Fix B: commit history panel ---- */

function fmtWhen(iso) {
  if (!iso) return "";
  const s = iso.includes("T") ? iso : iso.replace(" ", "T") + "Z";
  return new Date(s).toLocaleString();
}

async function loadHistory() {
  const historySection = document.getElementById("fr-history");
  const historyRows = document.getElementById("fr-history-rows");
  if (!historySection || !historyRows) return;

  const novelId = novelSelectEl.value ? Number(novelSelectEl.value) : null;
  if (!novelId) {
    historySection.hidden = true;
    return;
  }

  let snapshots;
  try {
    snapshots = await api.frSnapshots(novelId);
  } catch (e) {
    historySection.hidden = true;
    return;
  }

  historySection.hidden = false;

  if (!snapshots.length) {
    historyRows.innerHTML = `<p class="muted">No replacements recorded for this novel.</p>`;
    return;
  }

  // Render newest first (server already returns newest first).
  historyRows.innerHTML = snapshots.map(s => {
    const find = escapeHtml(JSON.stringify(s.find_pattern));
    const repl = escapeHtml(JSON.stringify(s.replace_pattern));
    const when = fmtWhen(s.committed_at);
    const target = escapeHtml(s.target || "");
    const chapters = s.chapters_changed;
    const meta = `${chapters} chapter${chapters === 1 ? "" : "s"} · ${target} · ${escapeHtml(when)}`;
    const actionCell = s.restored_at
      ? `<span class="muted fr-history-restored">Restored</span>`
      : `<button type="button" class="btn-ghost fr-history-restore-btn" data-restore="${s.id}">Restore</button>`;
    return `<div class="fr-history-row">
      <span class="fr-history-pair">${find} &rarr; ${repl}</span>
      <span class="muted fr-history-meta">${meta}</span>
      ${actionCell}
    </div>`;
  }).join("");
}

/* Event delegation for Restore buttons in the history panel. */
const historyRowsEl = document.getElementById("fr-history-rows");
if (historyRowsEl) {
  historyRowsEl.addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".fr-history-restore-btn");
    if (!btn) return;
    const id = Number(btn.dataset.restore);
    if (!id) return;

    // Find the row data for the confirm dialog.
    const rowEl = btn.closest(".fr-history-row");
    const pairEl = rowEl ? rowEl.querySelector(".fr-history-pair") : null;
    const metaEl = rowEl ? rowEl.querySelector(".fr-history-meta") : null;
    const pairText = pairEl ? pairEl.innerHTML : escapeHtml(String(id));
    const metaText = metaEl ? metaEl.textContent : "";

    const ok = await confirmDialog({
      title: "Restore this replacement?",
      body: `<p>Pattern: ${pairText}</p><p class="muted">${escapeHtml(metaText)}</p>`,
      okText: "Restore",
      danger: true,
    });
    if (!ok) return;

    btn.disabled = true;
    try {
      await api.restoreFrSnapshot(id);
      showToast("Reverted.", "ok");
      loadHistory();
    } catch (e) {
      showToast(`Restore failed: ${escapeHtml(e.message)}`, "err");
      btn.disabled = false;
    }
  });
}
