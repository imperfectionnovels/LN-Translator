/* Global glossary page (Initiative 3).
 *
 * Simpler than the per-novel glossary page: no auto-vs-locked filter (every
 * global entry is locked), no retranslate-affected (cross-novel; a future
 * version can layer init 4's find/replace engine on top), no glossary-merge
 * banner. Editing an entry surfaces a usage warning showing N novels / M
 * chapters affected — keeps the user honest about the blast radius.
 */

const rowsEl = document.getElementById("gloss-rows");
const toastEl = document.getElementById("toast");
const addBtn = document.getElementById("add-entry-btn");
const addForm = document.getElementById("add-entry-form");
const newZh = document.getElementById("new-zh");
const newEn = document.getElementById("new-en");
const newCat = document.getElementById("new-category");
const newNotes = document.getElementById("new-notes");
const newUsageNote = document.getElementById("new-usage-note");
const cancelAdd = document.getElementById("cancel-add");

let entries = [];

/* Confirm dialog lives in frontend/js/utils.js (C7). */

function showToast(msg, kind = "info") {
  toastEl.className = "status " + kind;
  toastEl.textContent = msg;
  setTimeout(() => { toastEl.textContent = ""; toastEl.className = "status"; }, 4500);
}

/* Initiative 4 — global-glossary apply-choice dialog. Same shape as the
 * per-novel page but with only 2 buttons (no retranslate-affected route
 * for global entries — that's an Initiative 5/EPUB-era follow-up). */
const applyChoiceDlg = document.getElementById("apply-choice-dialog");
const applyChoiceBody = document.getElementById("apply-choice-body");
const applyChoiceMeta = document.getElementById("apply-choice-meta");

async function openGlobalApplyChoiceDialog(entry, oldTermEn) {
  if (!applyChoiceDlg) return;
  // Surface the blast radius up front — global edits hit every novel.
  let usage = [];
  try { usage = await api.globalGlossaryUsage(entry.id); } catch (_) {}
  const totalChapters = usage.reduce((s, u) => s + u.chapter_count, 0);
  applyChoiceBody.innerHTML =
    `<p>Global term updated:</p>
     <p><strong>${escapeHtml(entry.term_zh)}</strong>: ${escapeHtml(oldTermEn)} → <strong>${escapeHtml(entry.term_en)}</strong></p>
     <p class="muted">Applying updates exact matching English text only. Chapters where the old term was translated inconsistently won't all match.</p>`;
  if (usage.length) {
    applyChoiceMeta.innerHTML =
      `<strong>Impact:</strong> ${usage.length} novel${usage.length === 1 ? "" : "s"} · ${totalChapters} source chapter${totalChapters === 1 ? "" : "s"} reference this term.`;
    applyChoiceMeta.classList.remove("hidden");
  } else {
    applyChoiceMeta.classList.add("hidden");
    applyChoiceMeta.innerHTML = "";
  }

  const handlers = [];
  function cleanup() {
    handlers.forEach(({ btn, fn }) => btn.removeEventListener("click", fn));
    applyChoiceDlg.removeEventListener("cancel", onCancelEvt);
  }
  const onCancelEvt = () => cleanup();
  applyChoiceDlg.querySelectorAll("[data-choice]").forEach(btn => {
    const fn = async () => {
      const choice = btn.dataset.choice;
      cleanup();
      applyChoiceDlg.close();
      if (choice === "none") {
        showToast(`Saved "${entry.term_zh}". Existing chapters unchanged.`, "ok");
        return;
      }
      if (choice === "apply") {
        try {
          const res = await api.globalGlossaryApplyInPlace(entry.id, oldTermEn, entry.term_en);
          showToast(
            `Applied across ${res.chapters_updated} chapter${res.chapters_updated === 1 ? "" : "s"} · ` +
            `${res.rows_updated_translated} draft / ${res.rows_updated_refined} refined rows.`,
            "ok"
          );
        } catch (e) {
          showToast(`Apply failed: ${e.message}`, "err");
        }
      }
    };
    btn.addEventListener("click", fn);
    handlers.push({ btn, fn });
  });
  applyChoiceDlg.addEventListener("cancel", onCancelEvt);
  applyChoiceDlg.showModal();
}

/* ---- Render ---- */
function render() {
  rowsEl.setAttribute("aria-busy", "false");
  if (!entries.length) {
    rowsEl.innerHTML = '<p class="muted">No global glossary entries yet. Add one with the button above, or promote a per-novel entry from any novel\'s glossary page.</p>';
    return;
  }
  // Group by category, same order as the per-novel page.
  const order = ["character", "place", "technique", "item", "other", "idiom"];
  const byCat = {};
  for (const e of entries) (byCat[e.category] = byCat[e.category] || []).push(e);
  const parts = [];
  for (const cat of order) {
    const list = byCat[cat] || [];
    if (!list.length) continue;
    parts.push(`<h3 class="cat-head">${cat} <span class="muted">(${list.length})</span></h3>`);
    for (const e of list) parts.push(_cardHtml(e));
  }
  rowsEl.innerHTML = parts.join("");
  _wireCards();
}

function _cardHtml(e) {
  return `
    <div class="gloss-card" data-id="${e.id}">
      <div class="card-head">
        <strong class="term-zh">${escapeHtml(e.term_zh)}</strong>
        <span class="muted">→</span>
        <input type="text" class="term-en" data-field="term_en" value="${escapeHtml(e.term_en)}">
      </div>
      <div class="card-meta">
        <select data-field="category">
          ${["character","place","technique","item","other","idiom"]
            .map(c => `<option value="${c}"${c === e.category ? " selected" : ""}>${c}</option>`).join("")}
        </select>
        <input type="text" data-field="notes" placeholder="Notes" value="${escapeHtml(e.notes || "")}">
        <input type="text" data-field="usage_note" placeholder="Usage note (injected into every prompt)" value="${escapeHtml(e.usage_note || "")}">
      </div>
      <div class="card-actions">
        <button type="button" class="btn-ghost" data-act="usage">Usage</button>
        <button type="button" class="btn-ghost danger-confirm" data-act="delete">Delete</button>
      </div>
    </div>`;
}

function _wireCards() {
  rowsEl.querySelectorAll(".gloss-card").forEach(card => {
    const id = parseInt(card.dataset.id, 10);
    // Auto-save on any field change. The PATCH route accepts partial bodies.
    card.querySelectorAll("input[data-field], select[data-field]").forEach(input => {
      input.addEventListener("change", async () => {
        const field = input.dataset.field;
        const value = input.value;
        // Initiative 4 — snapshot old term_en so we can offer the in-place
        // substitution dialog after a term change.
        const prevEntry = entries.find(x => x.id === id);
        const oldTermEn = prevEntry ? prevEntry.term_en : null;
        try {
          const updated = await api.updateGlobalGlossary(id, { [field]: value });
          const idx = entries.findIndex(x => x.id === id);
          if (idx >= 0) entries[idx] = updated;
          if (
            field === "term_en"
            && oldTermEn != null
            && oldTermEn !== updated.term_en
            && oldTermEn.trim()
            && updated.term_en.trim()
          ) {
            openGlobalApplyChoiceDialog(updated, oldTermEn);
          } else {
            showToast(`Saved "${updated.term_zh}". Cache invalidates on next translation.`, "ok");
          }
        } catch (e) {
          showToast(`Save failed: ${e.message}`, "err");
        }
      });
    });
    card.querySelector("[data-act='delete']").addEventListener("click", async () => {
      const entry = entries.find(x => x.id === id);
      if (!entry) return;
      // Show blast radius before delete so the user knows what they're
      // undoing across novels.
      let usage = [];
      try { usage = await api.globalGlossaryUsage(id); } catch (_) { /* best effort */ }
      const totalChapters = usage.reduce((s, u) => s + u.chapter_count, 0);
      const meta = usage.length
        ? `<strong>Affects:</strong> ${usage.length} novel${usage.length === 1 ? "" : "s"} · ${totalChapters} chapter${totalChapters === 1 ? "" : "s"} reference this term in their source.`
        : "";
      const ok = await confirmDialog({
        title: "Delete global glossary entry?",
        body: `<p>Delete <strong>${escapeHtml(entry.term_zh)}</strong> → <strong>${escapeHtml(entry.term_en)}</strong>?</p>
               <p class="muted">Existing translations stay as-is. Future translations across all novels will no longer use this rendering.</p>`,
        meta,
        okText: "Delete",
        danger: true,
      });
      if (!ok) return;
      try {
        await api.deleteGlobalGlossary(id);
        entries = entries.filter(x => x.id !== id);
        render();
        showToast(`Deleted "${entry.term_zh}".`, "ok");
      } catch (e) {
        showToast(`Delete failed: ${e.message}`, "err");
      }
    });
    card.querySelector("[data-act='usage']").addEventListener("click", async () => {
      const entry = entries.find(x => x.id === id);
      if (!entry) return;
      let usage;
      try { usage = await api.globalGlossaryUsage(id); } catch (e) {
        showToast(`Usage check failed: ${e.message}`, "err"); return;
      }
      const totalChapters = usage.reduce((s, u) => s + u.chapter_count, 0);
      const rows = usage.length
        ? `<ul>${usage.map(u => `<li>${escapeHtml(u.novel_title)} <span class="muted">· ${u.chapter_count} chapter${u.chapter_count === 1 ? "" : "s"}</span></li>`).join("")}</ul>`
        : `<p class="muted">No chapters reference this term yet.</p>`;
      await confirmDialog({
        title: `Usage of "${entry.term_zh}"`,
        body: `<p>This term appears in <strong>${totalChapters}</strong> chapter${totalChapters === 1 ? "" : "s"} across <strong>${usage.length}</strong> novel${usage.length === 1 ? "" : "s"}.</p>${rows}`,
        okText: "OK",
        cancelText: "",
      });
    });
  });
}

/* ---- Add new ---- */
addBtn.addEventListener("click", () => {
  addForm.hidden = !addForm.hidden;
  if (!addForm.hidden) newZh.focus();
});
cancelAdd.addEventListener("click", () => { addForm.hidden = true; });
addForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = {
    term_zh: newZh.value.trim(),
    term_en: newEn.value.trim(),
    category: newCat.value,
    notes: newNotes.value.trim() || null,
    usage_note: newUsageNote.value.trim() || null,
  };
  try {
    const created = await api.createGlobalGlossary(body);
    entries.unshift(created);
    render();
    addForm.hidden = true;
    newZh.value = ""; newEn.value = ""; newNotes.value = ""; newUsageNote.value = "";
    showToast(`Added "${created.term_zh}" to the global glossary.`, "ok");
  } catch (err) {
    // 409 = collision with an existing global entry. Offer to scroll to it.
    if (err.message && err.message.startsWith("409")) {
      showToast(`A global entry for "${body.term_zh}" already exists. Edit it directly above.`, "err");
    } else {
      showToast(`Add failed: ${err.message}`, "err");
    }
  }
});

/* ---- Boot ---- */
async function load() {
  try {
    entries = await api.globalGlossary();
    render();
  } catch (e) {
    rowsEl.setAttribute("aria-busy", "false");
    rowsEl.innerHTML = `<p class="status err">Load failed: ${escapeHtml(e.message)}</p>`;
  }
}
load();
