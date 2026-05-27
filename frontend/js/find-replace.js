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
const toastEl = document.getElementById("toast");

let currentToken = null;

function showToast(msg, kind = "info") {
  toastEl.className = "status " + kind;
  toastEl.textContent = msg;
  setTimeout(() => { toastEl.textContent = ""; toastEl.className = "status"; }, 6000);
}

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
let _undoSnapshotId = null;

function _hideUndoBar() {
  undoBar.hidden = true;
  if (_undoExpiryTimer) { clearTimeout(_undoExpiryTimer); _undoExpiryTimer = null; }
  if (_undoTickTimer) { clearInterval(_undoTickTimer); _undoTickTimer = null; }
  _undoSnapshotId = null;
}

async function _showUndoBarForNovel(novelId, summary) {
  // Latest snapshot for this novel is what we just created.
  let snapshots;
  try { snapshots = await api.frSnapshots(novelId); }
  catch { return; }
  if (!snapshots.length) return;
  const latest = snapshots[0]; // server returns newest first
  // 10-minute window from committed_at (parsed leniently).
  const committedAt = Date.parse(latest.committed_at) || Date.now();
  const expiresAt = committedAt + 10 * 60_000;
  if (expiresAt <= Date.now() || latest.restored_at) return;
  _undoSnapshotId = latest.id;
  undoMsg.textContent = summary;
  undoBar.hidden = false;
  _undoExpiryTimer = setTimeout(_hideUndoBar, expiresAt - Date.now());
  const tick = () => {
    const remaining = Math.max(0, Math.floor((expiresAt - Date.now()) / 60_000));
    undoTimer.textContent = remaining > 0 ? `· ${remaining} min left` : "· expiring";
  };
  tick();
  _undoTickTimer = setInterval(tick, 30_000);
}

undoBtn.addEventListener("click", async () => {
  if (!_undoSnapshotId) return;
  undoBtn.disabled = true;
  undoBtn.textContent = "Reverting…";
  try {
    await api.restoreFrSnapshot(_undoSnapshotId);
    showToast("Reverted. The chapters are back to their pre-apply text.", "ok");
    _hideUndoBar();
  } catch (e) {
    showToast(`Undo failed: ${e.message}`, "err");
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
  commitBtn.disabled = true;
  commitBtn.textContent = "Applying…";
  try {
    const result = await api.findReplaceCommit(currentToken);
    const summary = `Applied to ${result.chapters_updated} chapter${result.chapters_updated === 1 ? "" : "s"} · `
      + `${result.rows_updated_translated} draft rows, ${result.rows_updated_refined} refined rows updated.`;
    showToast(summary, "ok");
    // F1: only single-novel scope gets the undo bar — global scope writes
    // multiple snapshots and there isn't one batch to revert.
    _hideUndoBar();
    if (scopeKindEl.value === "novel" && novelSelectEl.value) {
      _showUndoBarForNovel(Number(novelSelectEl.value), summary);
    }
    currentToken = null;
    previewSection.hidden = true;
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
