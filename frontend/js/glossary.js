/* Glossary page — Han-led plate grid + list view + inline edit.
 * Design follows backend/design/glossary-upgrade proposal §01-§04, §06.
 * Backwards-compatible with the existing API surface in /static/js/api.js. */

const params = new URLSearchParams(location.search);
const novelId = parseInt(params.get("novel"), 10);

const rowsEl = document.getElementById("gloss-rows");
const searchEl = document.getElementById("search");
const sortByEl = document.getElementById("sort-by");
const filterModeEl = document.getElementById("filter-mode");
const catRailEl = document.getElementById("cat-rail");
const toastEl = document.getElementById("toast");
const addBtn = document.getElementById("add-entry-btn");
const headTitle = document.getElementById("head-title");
const headSub = document.getElementById("head-sub");
const healthRibbon = document.getElementById("health-ribbon");
const healthRibbonMsg = document.getElementById("health-ribbon-msg");
const healthRibbonStats = document.getElementById("health-ribbon-stats");
const healthOpenBtn = document.getElementById("health-open");
const healthDismissBtn = document.getElementById("health-dismiss");
const viewToggleEls = document.querySelectorAll(".view-toggle button[data-view]");

const LS_VIEW_KEY = `gloss.view.${novelId}`;
const LS_DISMISS_KEY = `gloss.health.dismiss.${novelId}`;
let view = localStorage.getItem(LS_VIEW_KEY) === "list" ? "list" : "cards";
viewToggleEls.forEach(b => b.classList.toggle("on", b.dataset.view === view));

let filterMode = "all";   // all | locked | auto | manual
let activeCat  = "all";
let entries    = [];
let editingId  = null;    // currently-editing entry id (plate or list row)
let addOpen    = false;   // inline add card visible at top of grid
let healthCache = null;   // last fetched health report

// Bulk-select state persists across renders so a re-fetch doesn't drop
// in-progress selections.
const selected = new Set();

const CATS = [
  { id: "all",       label: "All",        glyph: "全" },
  { id: "character", label: "Characters", glyph: "人" },
  { id: "place",     label: "Places",     glyph: "山" },
  { id: "technique", label: "Techniques", glyph: "術" },
  { id: "item",      label: "Items",      glyph: "物" },
  { id: "idiom",     label: "Idioms",     glyph: "言" },
  { id: "other",     label: "Other",      glyph: "餘" },
];
const CAT_LABEL = Object.fromEntries(CATS.map(c => [c.id, c.label]));
const CAT_GLYPH = Object.fromEntries(CATS.map(c => [c.id, c.glyph]));

function escapeAttr(s) { return String(s || "").replace(/"/g, "&quot;"); }

let toastTimer = null;
function showToast(msg, kind = "info") {
  toastEl.className = `status ${kind}`;
  toastEl.textContent = msg;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.textContent = ""; toastEl.className = "status"; }, 4000);
}

/* ============================================================
   Export menu. Direct hrefs to streaming endpoints.
   ============================================================ */
{
  const csv = document.getElementById("export-csv");
  const md = document.getElementById("export-md");
  if (csv) csv.href = `/api/novels/${novelId}/glossary/export?format=csv`;
  if (md)  md.href  = `/api/novels/${novelId}/glossary/export?format=md`;
}

/* Confirm dialog lives in frontend/js/utils.js (C7). */

/* ============================================================
   Apply-choice dialog (Initiative 4). Same behavior as before.
   ============================================================ */
const applyChoiceDlg = document.getElementById("apply-choice-dialog");
const applyChoiceBody = document.getElementById("apply-choice-body");
const applyChoiceMeta = document.getElementById("apply-choice-meta");

function openApplyChoiceDialog(entry, oldTermEn) {
  if (!applyChoiceDlg) return;
  applyChoiceBody.innerHTML =
    `<p>Glossary term updated:</p>
     <p><strong>${escapeHtml(entry.term_zh)}</strong>: ${escapeHtml(oldTermEn)} → <strong>${escapeHtml(entry.term_en)}</strong></p>
     <p class="muted">Applying to existing translations updates exact matching English text only. Chapters where the old term was translated inconsistently won't all match. For full consistency, choose Retranslate.</p>`;
  applyChoiceMeta.classList.add("hidden");
  applyChoiceMeta.innerHTML = "";

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
        showToast(`Saved “${entry.term_zh}”. Existing chapters unchanged.`, "ok");
        return;
      }
      if (choice === "apply") {
        try {
          const res = await api.glossaryApplyInPlace(entry.id, oldTermEn, entry.term_en);
          showToast(
            `Applied to ${res.chapters_updated} chapter${res.chapters_updated === 1 ? "" : "s"} · ` +
            `${res.rows_updated_translated} draft / ${res.rows_updated_refined} refined rows.`,
            "ok"
          );
        } catch (e) {
          showToast(`Apply failed: ${e.message}`, "err");
        }
        return;
      }
      if (choice === "retranslate") {
        let affected;
        try { affected = await api.affectedChapters(entry.id); }
        catch (e) { showToast(`Couldn't list affected chapters: ${e.message}`, "err"); return; }
        if (!affected || !affected.length) {
          showToast(`No chapters contain “${entry.term_zh}”.`, "info");
          return;
        }
        const ok = await confirmDialog({
          title: `Re-translate ${affected.length} chapter${affected.length === 1 ? "" : "s"}?`,
          body: `<p>Affected chapters use <strong>${escapeHtml(entry.term_zh)}</strong>.</p>
                 <p class="muted">Existing polished text for these chapters will be cleared when they're re-translated.</p>`,
          meta: `<strong>Chapters:</strong> ${escapeHtml(affected.map(c => c.chapter_num).join(", "))}`,
          okText: "Re-translate",
        });
        if (!ok) return;
        try {
          const res = await api.retranslateAffected(entry.id);
          const queued = res.queued_count || 0;
          showToast(`Queued ${queued} chapter${queued === 1 ? "" : "s"} for re-translation.`, "ok");
        } catch (e) {
          showToast(`Re-translate failed: ${e.message}`, "err");
        }
      }
    };
    btn.addEventListener("click", fn);
    handlers.push({ btn, fn });
  });
  applyChoiceDlg.addEventListener("cancel", onCancelEvt);
  applyChoiceDlg.showModal();
}

/* ============================================================
   Filtering and sorting. Shared between cards and list views.
   ============================================================ */
function visibleEntries() {
  const q = (searchEl.value || "").trim().toLowerCase();
  let list = entries.slice();
  if (activeCat !== "all") list = list.filter(e => e.category === activeCat);
  if (q) list = list.filter(e =>
    (e.term_zh || "").toLowerCase().includes(q) ||
    (e.term_en || "").toLowerCase().includes(q) ||
    (e.notes || "").toLowerCase().includes(q) ||
    (e.usage_note || "").toLowerCase().includes(q)
  );
  if (filterMode === "locked")      list = list.filter(e => e.locked);
  else if (filterMode === "auto")   list = list.filter(e => e.auto_detected && !e.locked);
  else if (filterMode === "manual") list = list.filter(e => !e.auto_detected && !e.locked);

  const by = sortByEl.value;
  list.sort((a, b) => {
    if (by === "alpha-zh") return (a.term_zh || "").localeCompare(b.term_zh || "");
    if (by === "recent")   return b.id - a.id;
    return (a.term_en || "").localeCompare(b.term_en || "");
  });
  return list;
}

/* ============================================================
   Category rail (top tab strip).
   ============================================================ */
function renderCatRail() {
  const counts = {};
  for (const e of entries) counts[e.category] = (counts[e.category] || 0) + 1;
  counts.all = entries.length;
  catRailEl.innerHTML = CATS
    // Hide "Other" tab when empty. Keeps the rail focused on real categories.
    .filter(c => c.id !== "other" || (counts.other || 0) > 0)
    .map(c => `
      <button class="cat-tab ${c.id === activeCat ? "on" : ""}" data-cat="${c.id}" role="tab" aria-selected="${c.id === activeCat}">
        <span class="glyph ${c.id}">${c.glyph}</span>${escapeHtml(c.label)}<span class="count">${counts[c.id] || 0}</span>
      </button>
    `).join("");
  catRailEl.querySelectorAll(".cat-tab").forEach(el => {
    el.addEventListener("click", () => {
      activeCat = el.dataset.cat;
      renderCatRail();
      render();
    });
  });
}

/* ============================================================
   Source-segment counts (small numerals inside the seg buttons).
   ============================================================ */
function updateSourceCounts() {
  const c = { locked: 0, auto: 0, manual: 0 };
  for (const e of entries) {
    if (e.locked) c.locked++;
    else if (e.auto_detected) c.auto++;
    else c.manual++;
  }
  filterModeEl.querySelectorAll(".seg-count").forEach(span => {
    const k = span.dataset.count;
    span.textContent = c[k] != null ? c[k] : "";
  });
}

/* ============================================================
   Health ribbon.
   ============================================================ */
async function loadHealth() {
  try {
    healthCache = await api.glossaryHealth(novelId);
    renderHealthRibbon();
  } catch (e) {
    healthRibbon.classList.add("hidden");
  }
}

function renderHealthRibbon() {
  if (!healthCache) { healthRibbon.classList.add("hidden"); return; }
  const dupEn  = (healthCache.duplicate_en || []).length;
  const dupZh  = (healthCache.duplicate_zh || []).length;
  const unused = (healthCache.unused || []).length;
  const total  = dupEn + dupZh + unused;
  // Dismissal key: clear when the underlying counts change so user gets a
  // fresh signal after fixing or breaking something. A plain count works
  // as a stable hash of the report.
  const stamp = `${dupEn}-${dupZh}-${unused}`;
  if (total === 0 || localStorage.getItem(LS_DISMISS_KEY) === stamp) {
    healthRibbon.classList.add("hidden");
    return;
  }
  const bits = [];
  if (dupEn)  bits.push(`<strong>${dupEn}</strong> duplicate English term${dupEn === 1 ? "" : "s"}`);
  if (dupZh)  bits.push(`<strong>${dupZh}</strong> duplicate Chinese`);
  if (unused) bits.push(`<strong>${unused}</strong> unused`);
  healthRibbonMsg.innerHTML = `<strong>Glossary health · ${total} thing${total === 1 ? "" : "s"} to look at.</strong> ${bits.join(" · ")}.`;
  healthRibbonStats.innerHTML =
    (dupEn  ? `<span><span class="n">${dupEn}</span> dupes en</span>` : "") +
    (dupZh  ? `<span><span class="n">${dupZh}</span> dupes zh</span>` : "") +
    (unused ? `<span><span class="n">${unused}</span> unused</span>`   : "");
  healthRibbon.dataset.stamp = stamp;
  healthRibbon.classList.remove("hidden");
}

healthDismissBtn?.addEventListener("click", () => {
  const stamp = healthRibbon.dataset.stamp || "0";
  localStorage.setItem(LS_DISMISS_KEY, stamp);
  healthRibbon.classList.add("hidden");
});

/* ============================================================
   Health dialog (full report).
   ============================================================ */
{
  const dlg = document.getElementById("health-dialog");
  const body = document.getElementById("health-dialog-body");
  const closeBtn = document.getElementById("health-close");
  function renderHealth(report) {
    const dupEn = report.duplicate_en || [];
    const dupZh = report.duplicate_zh || [];
    const unused = report.unused || [];
    if (!dupEn.length && !dupZh.length && !unused.length) {
      body.innerHTML = `<p class="ok">No issues found across ${report.total_entries} entries.</p>`;
      return;
    }
    const parts = [];
    if (dupEn.length) {
      parts.push(`
        <section>
          <h4>Duplicate English (${dupEn.length} group${dupEn.length === 1 ? "" : "s"})</h4>
          <p class="muted">Same English term used for multiple Chinese terms. Readers can't tell which one a given mention came from.</p>
          ${dupEn.map(g => `
            <div class="health-group">
              <div class="health-label">${escapeHtml(g.term_en)}</div>
              <ul>${g.entries.map(e => `<li>${escapeHtml(e.term_zh)} · ${escapeHtml(e.category)}${e.locked ? " · locked" : ""}</li>`).join("")}</ul>
            </div>`).join("")}
        </section>
      `);
    }
    if (dupZh.length) {
      parts.push(`
        <section>
          <h4>Duplicate Chinese (${dupZh.length} group${dupZh.length === 1 ? "" : "s"})</h4>
          ${dupZh.map(g => `
            <div class="health-group">
              <div class="health-label">${escapeHtml(g.term_zh)}</div>
              <ul>${g.entries.map(e => `<li>${escapeHtml(e.term_en)}</li>`).join("")}</ul>
            </div>`).join("")}
        </section>
      `);
    }
    if (unused.length) {
      parts.push(`
        <section>
          <h4>Unused (${unused.length})</h4>
          <p class="muted">Term doesn't appear in any chapter. Stale auto-extracted term or pre-seeded term whose chapter isn't uploaded yet.</p>
          <ul class="health-unused">${unused.map(e => `<li>${escapeHtml(e.term_zh)} → ${escapeHtml(e.term_en)}${e.locked ? " <span class='muted'>(locked)</span>" : ""}</li>`).join("")}</ul>
        </section>
      `);
    }
    body.innerHTML = parts.join("");
  }
  healthOpenBtn?.addEventListener("click", async () => {
    body.innerHTML = `<p class="muted">Loading…</p>`;
    dlg.showModal();
    try {
      const report = healthCache || await api.glossaryHealth(novelId);
      healthCache = report;
      renderHealth(report);
    } catch (e) {
      body.innerHTML = `<p class="status err">${escapeHtml(e.message)}</p>`;
    }
  });
  closeBtn?.addEventListener("click", () => dlg.close());
}

/* ============================================================
   Plate rendering. The new Han-led card.
   ============================================================ */
function renderPlate(e) {
  const isSel = selected.has(e.id);
  const isEditing = editingId === e.id;
  const cat = e.category || "other";
  const badges = [];
  if (e.locked) badges.push(`<span class="badge locked"><span class="han">鎖</span>locked</span>`);
  if (e.auto_detected && !e.locked) badges.push(`<span class="badge auto"><span class="han">自</span>auto</span>`);
  const usage = e.usage_note && e.usage_note.trim()
    ? `<div class="usage" data-id="${e.id}" title="Click to edit usage_note">${escapeHtml(e.usage_note)}</div>`
    : `<div class="usage empty" data-id="${e.id}" title="Add a usage note — injected into every translation prompt">No usage note · <em>click to add</em></div>`;
  return `
    <div class="plate ${isSel ? "selected" : ""} ${isEditing ? "editing" : ""}" data-id="${e.id}">
      <div class="sel" data-act="toggle-select" title="Select for bulk actions"></div>
      <div class="head">
        <div class="han-block">${escapeHtml(e.term_zh)}</div>
        <div class="badges">${badges.join("")}</div>
      </div>
      <div class="body">
        <div class="en">${escapeHtml(e.term_en)}</div>
        <div class="cat-line">
          <span class="cat-glyph ${cat}">${escapeHtml(CAT_GLYPH[cat] || "餘")}</span>
          <span>${escapeHtml(CAT_LABEL[cat] || cat)}</span>
        </div>
        ${usage}
      </div>
      ${isEditing ? renderPlateEditZone(e) : ""}
      <div class="foot">
        <span class="meta-text"></span>
        <span class="spacer"></span>
        <span class="row-acts">
          <span class="ico ${e.locked ? "cin on" : ""}" data-act="toggle-lock" title="${e.locked ? "Unlock this term" : "Lock this term (won't be auto-overwritten)"}">鎖</span>
          <span class="ico jade" data-act="retranslate" title="Re-translate every chapter that uses this term">↻</span>
          <span class="ico" data-act="edit" title="Edit term">✎</span>
          <span class="ico" data-act="menu" title="More actions">⋯</span>
        </span>
      </div>
    </div>
  `;
}

function renderPlateEditZone(e) {
  const cats = ["character", "place", "technique", "item", "idiom", "other"];
  return `
    <div class="edit-zone">
      <div class="field">
        <label>English</label>
        <input data-edit="term_en" value="${escapeAttr(e.term_en)}">
      </div>
      <div class="field-row">
        <div class="field">
          <label>Category</label>
          <select data-edit="category">
            ${cats.map(c => `<option value="${c}" ${c === e.category ? "selected" : ""}>${c}</option>`).join("")}
          </select>
        </div>
        <div class="field" style="justify-content:end;">
          <label><span class="han" style="font-family:var(--font-family-han);font-weight:700;background:var(--cinnabar);color:#f4ecd6;padding:1px 5px;border-radius:2px;font-size:10px;margin-right:6px;">鎖</span>Lock</label>
          <label style="display:flex;align-items:center;gap:6px;font-family:var(--font-family-sans);font-size:12px;color:var(--fg);text-transform:none;letter-spacing:0;padding-top:4px;">
            <input type="checkbox" data-edit="locked" ${e.locked ? "checked" : ""}>
            <span>Locked</span>
          </label>
        </div>
      </div>
      <div class="field usage">
        <label><span class="han" style="font-family:var(--font-family-han);font-weight:700;background:var(--jade);color:#f0efeb;padding:1px 5px;border-radius:2px;font-size:10px;margin-right:4px;">提</span>Usage note · injected to every prompt</label>
        <textarea data-edit="usage_note" placeholder='e.g. "Always capital M — protagonist epithet, not generic."'>${escapeHtml(e.usage_note || "")}</textarea>
      </div>
      <div class="field">
        <label>Notes (yours only. Not sent to translator)</label>
        <input data-edit="notes" value="${escapeAttr(e.notes || "")}" placeholder="Private notes">
      </div>
      <div class="toolbar">
        <span class="hint">Saves on change · close with <span class="kbd">esc</span></span>
        <span class="spacer"></span>
        <button class="btn-tiny" data-act="close-edit">Close</button>
      </div>
    </div>
  `;
}

function renderAddCard() {
  const cats = ["character", "place", "technique", "item", "idiom", "other"];
  return `
    <div class="plate add-card" data-add-card>
      <div class="head">
        <span class="lbl"><strong>New term</strong></span>
      </div>
      <div class="body">
        <div class="field zh">
          <label>Chinese</label>
          <input data-add="term_zh" placeholder="中文" autocomplete="off" required>
        </div>
        <div class="field-row">
          <div class="field">
            <label>English</label>
            <input data-add="term_en" placeholder="English" autocomplete="off" required>
          </div>
          <div class="field">
            <label>Category</label>
            <select data-add="category">
              ${cats.map(c => `<option value="${c}" ${c === "character" ? "selected" : ""}>${c}</option>`).join("")}
            </select>
          </div>
        </div>
        <div class="field usage">
          <label>Usage note · injected into every prompt</label>
          <textarea data-add="usage_note" placeholder='e.g. "Pinyin + Sect. Not the literal reading."'></textarea>
        </div>
        <div class="field">
          <label>Notes (yours only)</label>
          <input data-add="notes" placeholder="Private notes — not shown to the translator">
        </div>
      </div>
      <div class="foot">
        <button class="btn-primary" data-act="add-lock">Add &amp; lock</button>
        <button class="btn-tiny" data-act="add">Add</button>
        <span class="spacer"></span>
        <button class="btn-tiny" data-act="cancel-add">Cancel</button>
      </div>
    </div>
  `;
}

/* ============================================================
   List-row rendering.
   ============================================================ */
function renderListRow(e) {
  const cat = e.category || "other";
  const isSel = selected.has(e.id);
  const isEditing = editingId === e.id;
  const badges = [];
  if (e.locked) badges.push(`<span class="badge locked"><span class="han">鎖</span>locked</span>`);
  if (e.auto_detected && !e.locked) badges.push(`<span class="badge auto"><span class="han">自</span>auto</span>`);
  const usage = isEditing
    ? `<div class="use-cell">usage_note · editing below</div>`
    : (e.usage_note && e.usage_note.trim()
        ? `<div class="use-cell" data-act="edit-usage" title="${escapeAttr(e.usage_note)}">${escapeHtml(e.usage_note)}</div>`
        : `<div class="use-cell empty" data-act="edit-usage">no usage note · <span class="add-cta">click to add</span></div>`);
  const enCell = isEditing
    ? `<div class="en-cell"><input data-edit="term_en" value="${escapeAttr(e.term_en)}"></div>`
    : `<div class="en-cell">${escapeHtml(e.term_en)}</div>`;
  const catCell = isEditing
    ? `<div class="cat-cell"><select data-edit="category">${["character","place","technique","item","idiom","other"].map(c => `<option value="${c}" ${c === cat ? "selected" : ""}>${c}</option>`).join("")}</select></div>`
    : `<div class="cat-cell"><span class="cat-glyph ${cat}">${escapeHtml(CAT_GLYPH[cat] || "餘")}</span>${escapeHtml(CAT_LABEL[cat] || cat)}</div>`;
  const expansion = isEditing ? `
    <div class="edit-expansion">
      <span class="label"><span class="han">提</span>Usage note · injected to every prompt</span>
      <textarea data-edit="usage_note" placeholder='e.g. "Pinyin + Sect. Not the literal reading."'>${escapeHtml(e.usage_note || "")}</textarea>
      <div class="edit-toolbar">
        <span class="hint">Save with <span class="kbd">⌘</span><span class="kbd">⏎</span> · close with <span class="kbd">esc</span></span>
        <span class="spacer"></span>
        <button class="btn-tiny" data-act="close-edit">Close</button>
        <button class="btn-primary" data-act="save-edit">Save</button>
      </div>
    </div>` : "";
  return `
    <div class="list-row ${isSel ? "selected" : ""} ${isEditing ? "editing" : ""}" data-id="${e.id}">
      <div class="sel-cell" data-act="toggle-select" role="checkbox" aria-checked="${isSel}"></div>
      <div class="han-cell"><div class="zh">${escapeHtml(e.term_zh)}</div></div>
      ${enCell}
      ${catCell}
      ${usage}
      <div class="badge-cell">${badges.join("")}</div>
      <div class="freq-cell"></div>
      <div class="act-cell" data-act="menu" title="More">⋯</div>
      ${expansion}
    </div>
  `;
}

/* ============================================================
   Render. Top-level branching on view.
   ============================================================ */
function render() {
  rowsEl.setAttribute("aria-busy", "false");
  rowsEl.classList.toggle("list-view", view === "list");
  updateSourceCounts();

  const list = visibleEntries();

  // The add-card belongs at the head of the grid (cards view) or above
  // the list table (list view).
  const addHtml = addOpen ? renderAddCard() : "";

  if (list.length === 0) {
    if (entries.length === 0) {
      rowsEl.innerHTML = `
        ${addHtml}
        <div class="gloss-empty">
          <div class="han-eyebrow"><span class="han">詞</span><span>Glossary</span></div>
          <div class="title">Build the glossary as you go.</div>
          <p>Terms appear here as the translator extracts them, or you can add one manually. Locked terms become directives the translator follows in every chapter.</p>
          <div class="cta">
            <button class="btn-primary" id="empty-add">＋ New term</button>
            <a class="btn-tiny" href="/glossary/global">⇄ Open global glossary</a>
          </div>
        </div>`;
      const btn = rowsEl.querySelector("#empty-add");
      if (btn) btn.addEventListener("click", () => openAddCard());
    } else {
      rowsEl.innerHTML = `
        ${addHtml}
        <div class="gloss-empty">
          <div class="title">No matches</div>
          <p>Try a different search, or clear the category filter.</p>
        </div>`;
    }
    wireAddCard();
    renderBulkBar();
    return;
  }

  if (view === "cards") {
    rowsEl.innerHTML = addHtml + list.map(renderPlate).join("");
  } else {
    const head = `
      <div class="list-row head" role="row">
        <div></div>
        <div>Chinese</div>
        <div>English</div>
        <div>Category</div>
        <div>Usage note · injected to prompt</div>
        <div>Source</div>
        <div class="end">Hits</div>
        <div></div>
      </div>`;
    rowsEl.innerHTML = `${addHtml}<div class="list-table">${head}${list.map(renderListRow).join("")}</div>`;
  }

  wireAddCard();
  wireRows();
  renderBulkBar();
}

/* ============================================================
   Wiring. Delegated handlers per render pass.
   ============================================================ */
function wireRows() {
  // Card-view interactions
  rowsEl.querySelectorAll(".plate[data-id]").forEach(card => {
    const id = parseInt(card.dataset.id, 10);
    card.querySelector("[data-act='toggle-select']")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      toggleSelect(id);
    });
    card.querySelector("[data-act='toggle-lock']")?.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const entry = entries.find(x => x.id === id);
      if (!entry) return;
      await saveField(id, "locked", !entry.locked);
    });
    card.querySelector("[data-act='retranslate']")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      retranslateOne(id);
    });
    card.querySelector("[data-act='edit']")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      openEdit(id);
    });
    card.querySelector("[data-act='menu']")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      openMoreMenu(id, ev.currentTarget);
    });
    card.querySelector(".usage[data-id]")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      openEdit(id, "usage_note");
    });
    card.querySelector("[data-act='close-edit']")?.addEventListener("click", () => closeEdit());
    // Wire all edit inputs in this card (when in edit mode)
    card.querySelectorAll("[data-edit]").forEach(input => wireEditInput(input, id));
  });

  // List-view interactions
  rowsEl.querySelectorAll(".list-row[data-id]").forEach(row => {
    const id = parseInt(row.dataset.id, 10);
    row.querySelector("[data-act='toggle-select']")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      toggleSelect(id);
    });
    row.querySelector("[data-act='edit-usage']")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      openEdit(id, "usage_note");
    });
    row.querySelector("[data-act='menu']")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      openMoreMenu(id, ev.currentTarget);
    });
    row.querySelector("[data-act='close-edit']")?.addEventListener("click", () => closeEdit());
    row.querySelector("[data-act='save-edit']")?.addEventListener("click", () => closeEdit());
    row.querySelectorAll("[data-edit]").forEach(input => wireEditInput(input, id));
  });
}

function wireEditInput(input, id) {
  const field = input.dataset.edit;
  // Change-on-blur for textareas/inputs, immediate for checkboxes/selects.
  const evtName = (input.type === "checkbox" || input.tagName === "SELECT") ? "change" : "blur";
  input.addEventListener(evtName, () => {
    const value = input.type === "checkbox" ? input.checked : input.value;
    saveField(id, field, value);
  });
  // ⌘↵ inside textarea / input commits and closes the edit zone.
  input.addEventListener("keydown", (ev) => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
      ev.preventDefault();
      const value = input.type === "checkbox" ? input.checked : input.value;
      saveField(id, field, value).then(() => closeEdit());
    } else if (ev.key === "Escape") {
      closeEdit();
    }
  });
}

async function saveField(id, field, value) {
  const prevEntry = entries.find(x => x.id === id);
  if (!prevEntry) return;
  const oldTermEn = prevEntry.term_en;
  if (prevEntry[field] === value) return; // nothing to do
  try {
    const updated = await api.updateGlossary(id, { [field]: value });
    const idx = entries.findIndex(x => x.id === id);
    if (idx >= 0) entries[idx] = updated;
    renderCatRail();
    render();
    if (
      field === "term_en"
      && oldTermEn != null
      && oldTermEn !== updated.term_en
      && oldTermEn.trim()
      && updated.term_en.trim()
    ) {
      openApplyChoiceDialog(updated, oldTermEn);
    } else {
      showToast(`Saved “${updated.term_zh}”.`, "ok");
    }
  } catch (e) {
    showToast(`Save failed: ${e.message}`, "err");
  }
}

/* ============================================================
   Edit state.
   ============================================================ */
function openEdit(id, focusField) {
  editingId = id;
  render();
  // Defer focus until after re-render.
  requestAnimationFrame(() => {
    const sel = focusField
      ? rowsEl.querySelector(`.editing [data-edit="${focusField}"]`)
      : rowsEl.querySelector(`.editing [data-edit]`);
    if (sel) sel.focus();
  });
}
function closeEdit() {
  if (editingId == null) return;
  editingId = null;
  render();
}

/* ============================================================
   Selection.
   ============================================================ */
function toggleSelect(id) {
  if (selected.has(id)) selected.delete(id);
  else selected.add(id);
  // Optimistic: toggle the class on the affected row only, no full re-render.
  rowsEl.querySelectorAll(`[data-id="${id}"]`).forEach(el => {
    el.classList.toggle("selected", selected.has(id));
    const sel = el.querySelector(".sel, .sel-cell");
    if (sel) sel.setAttribute("aria-checked", selected.has(id));
  });
  renderBulkBar();
}

function renderBulkBar() {
  const bar = document.getElementById("gloss-bulk-bar");
  const count = document.getElementById("gloss-bulk-count");
  if (!bar) return;
  for (const id of selected) {
    if (!entries.some(e => e.id === id)) selected.delete(id);
  }
  if (selected.size === 0) { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  count.textContent = String(selected.size);
}

/* ============================================================
   "More" menu. Opens the confirm dialog as a small action picker.
   data-more buttons inside the dialog body trigger the row actions.
   ============================================================ */
async function openMoreMenu(id) {
  const entry = entries.find(x => x.id === id);
  if (!entry) return;
  const dlg = document.getElementById("confirm-dialog");
  dlg.dataset.entryId = String(id);
  await confirmDialog({
    title: `${entry.term_zh} → ${entry.term_en}`,
    body: `
      <p class="muted" style="margin-bottom:10px;">Choose an action.</p>
      <div style="display:flex; gap:8px; flex-wrap:wrap;">
        <button class="btn-tiny" data-more="edit" type="button">✎ Edit</button>
        <button class="btn-tiny" data-more="retranslate" type="button">↻ Retranslate affected</button>
        <button class="btn-tiny" data-more="promote" type="button">↑ Promote to global</button>
        <button class="btn-tiny" data-more="delete" type="button" style="color:var(--signal-error);border-color:var(--signal-error);">Delete</button>
      </div>`,
    okText: "",
    cancelText: "Close",
  });
  delete dlg.dataset.entryId;
}

// data-more buttons live inside the confirm dialog body; route them to the
// matching action and close the dialog. The dialog itself stashes the
// entry id when opened.
document.body.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-more]");
  if (!btn) return;
  const root = btn.closest("dialog");
  if (!root) return;
  const id = parseInt(root.dataset.entryId || "0", 10);
  if (!id) return;
  const action = btn.dataset.more;
  root.close();
  if (action === "edit") return openEdit(id);
  if (action === "retranslate") return retranslateOne(id);
  if (action === "promote") return promoteOne(id);
  if (action === "delete") return deleteOne(id);
});

/* ============================================================
   Per-row actions: retranslate, promote, delete.
   ============================================================ */
async function retranslateOne(id) {
  const entry = entries.find(x => x.id === id);
  if (!entry) return;
  let affected;
  try { affected = await api.affectedChapters(id); }
  catch (e) { showToast(`Couldn't list affected chapters: ${e.message}`, "err"); return; }
  if (!affected || !affected.length) {
    showToast(`No chapters contain “${entry.term_zh}”.`, "info");
    return;
  }
  const ok = await confirmDialog({
    title: `Re-translate ${affected.length} chapter${affected.length === 1 ? "" : "s"}?`,
    body: `<p>Affected chapters use <strong>${escapeHtml(entry.term_zh)}</strong>.</p>
           <p class="muted">Any existing polished text for these chapters will be cleared when they are re-translated.</p>`,
    meta: `<strong>Chapters:</strong> ${escapeHtml(affected.map(c => c.chapter_num).join(", "))}`,
    okText: "Re-translate",
  });
  if (!ok) return;
  try {
    const res = await api.retranslateAffected(id);
    const queued = res.queued_count || 0;
    showToast(`Queued ${queued} chapter${queued === 1 ? "" : "s"} for re-translation.`, "ok");
  } catch (e) {
    showToast(`Re-translate failed: ${e.message}`, "err");
  }
}

async function promoteOne(id) {
  const entry = entries.find(x => x.id === id);
  if (!entry) return;
  const ok = await confirmDialog({
    title: "Promote to global glossary?",
    body: `<p>Move <strong>${escapeHtml(entry.term_zh)}</strong> → <strong>${escapeHtml(entry.term_en)}</strong> into the cross-novel global glossary.</p>
           <p class="muted">Every novel's translator will see this rendering. A per-novel entry on the same term in another novel still wins for that novel.</p>`,
    okText: "Promote",
  });
  if (!ok) return;
  try {
    await api.promoteToGlobal(id);
    entries = entries.filter(x => x.id !== id);
    renderCatRail();
    render();
    showToast(`Promoted “${entry.term_zh}” to global.`, "ok");
  } catch (e) {
    if (e.message && e.message.startsWith("409")) {
      showToast(`A global entry for “${entry.term_zh}” already exists.`, "err");
    } else {
      showToast(`Promote failed: ${e.message}`, "err");
    }
  }
}

async function deleteOne(id) {
  const entry = entries.find(x => x.id === id);
  if (!entry) return;
  const ok = await confirmDialog({
    title: "Delete glossary entry?",
    body: `<p>Delete <strong>${escapeHtml(entry.term_zh)}</strong> → <strong>${escapeHtml(entry.term_en)}</strong>?</p>
           <p class="muted">This won't change already-translated chapters, but future chapters won't use this term.</p>`,
    okText: "Delete",
  });
  if (!ok) return;
  try {
    await api.deleteGlossary(id);
    entries = entries.filter(x => x.id !== id);
    selected.delete(id);
    renderCatRail();
    render();
    showToast(`Deleted “${entry.term_zh}”.`, "ok");
  } catch (e) {
    showToast(`Delete failed: ${e.message}`, "err");
  }
}

/* ============================================================
   Bulk actions.
   ============================================================ */
document.getElementById("gloss-bulk-clear")?.addEventListener("click", () => {
  selected.clear();
  rowsEl.querySelectorAll(".selected").forEach(c => c.classList.remove("selected"));
  renderBulkBar();
});

async function bulkLockAction(locked) {
  if (selected.size === 0) return;
  const ids = [...selected];
  try {
    await api.bulkLockGlossary(novelId, ids, locked);
    entries = entries.map(e => ids.includes(e.id) ? { ...e, locked } : e);
    selected.clear();
    renderCatRail();
    render();
    showToast(`${locked ? "Locked" : "Unlocked"} ${ids.length} entr${ids.length === 1 ? "y" : "ies"}.`, "ok");
  } catch (e) {
    showToast(`Bulk ${locked ? "lock" : "unlock"} failed: ${e.message}`, "err");
  }
}
document.getElementById("gloss-bulk-lock")?.addEventListener("click", () => bulkLockAction(true));
document.getElementById("gloss-bulk-unlock")?.addEventListener("click", () => bulkLockAction(false));

document.getElementById("gloss-bulk-retranslate")?.addEventListener("click", async () => {
  if (selected.size === 0) return;
  const ids = [...selected];
  const n = ids.length;
  const ok = await confirmDialog({
    title: `Retranslate chapters affected by ${n} term${n === 1 ? "" : "s"}?`,
    body: `<p>This will re-queue every translated chapter that contains any of the selected terms. Chapters using more than one of the selected terms are queued exactly once.</p>
           <p class="muted">Chapters currently being translated are skipped. The in-flight worker's success commit would clobber a mid-flight queue flip.</p>`,
    okText: "Retranslate",
  });
  if (!ok) return;
  try {
    const result = await api.bulkRetranslateAffected(ids);
    const q = result.queued_count || 0;
    const skip = (result.skipped_in_flight || []).length;
    const skipStr = skip ? ` (${skip} skipped, in flight)` : "";
    showToast(
      q === 0
        ? `No chapters affected by those terms.${skipStr}`
        : `Queued ${q} chapter${q === 1 ? "" : "s"} for re-translation.${skipStr}`,
      q === 0 ? "info" : "ok",
    );
  } catch (e) {
    showToast(`Bulk retranslate failed: ${e.message}`, "err");
  }
});

document.getElementById("gloss-bulk-delete")?.addEventListener("click", async () => {
  if (selected.size === 0) return;
  const ids = [...selected];
  const n = ids.length;
  const ok = await confirmDialog({
    title: `Delete ${n} glossary entr${n === 1 ? "y" : "ies"}?`,
    body: `<p>This won't change already-translated chapters, but future chapters won't use these terms.</p>`,
    okText: "Delete",
  });
  if (!ok) return;
  try {
    await api.bulkDeleteGlossary(novelId, ids);
    entries = entries.filter(e => !ids.includes(e.id));
    selected.clear();
    renderCatRail();
    render();
    showToast(`Deleted ${n} entr${n === 1 ? "y" : "ies"}.`, "ok");
  } catch (e) {
    showToast(`Bulk delete failed: ${e.message}`, "err");
  }
});

/* ============================================================
   Add card. Inline.
   ============================================================ */
function openAddCard() {
  addOpen = true;
  render();
  requestAnimationFrame(() => {
    const el = rowsEl.querySelector('[data-add="term_zh"]');
    if (el) el.focus();
  });
}
function closeAddCard() {
  addOpen = false;
  render();
}

function wireAddCard() {
  const card = rowsEl.querySelector("[data-add-card]");
  if (!card) return;
  card.querySelector("[data-act='cancel-add']")?.addEventListener("click", () => closeAddCard());
  card.querySelector("[data-act='add']")?.addEventListener("click", () => submitAdd(false));
  card.querySelector("[data-act='add-lock']")?.addEventListener("click", () => submitAdd(true));
  card.querySelectorAll("input, textarea").forEach(el => {
    el.addEventListener("keydown", (ev) => {
      if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
        ev.preventDefault();
        submitAdd(true);
      } else if (ev.key === "Escape") {
        closeAddCard();
      }
    });
  });
}

async function submitAdd(lockIt) {
  const card = rowsEl.querySelector("[data-add-card]");
  if (!card) return;
  const zh = card.querySelector('[data-add="term_zh"]').value.trim();
  const en = card.querySelector('[data-add="term_en"]').value.trim();
  const cat = card.querySelector('[data-add="category"]').value;
  const usage = card.querySelector('[data-add="usage_note"]').value.trim();
  const notes = card.querySelector('[data-add="notes"]').value.trim();
  if (!zh || !en) { showToast("Chinese and English are required.", "err"); return; }
  try {
    const created = await api.createGlossary(novelId, {
      term_zh: zh, term_en: en, category: cat,
      notes: notes || null,
      usage_note: usage || null,
    });
    // If "Add & lock", PATCH locked=1 right after.
    let final = created;
    if (lockIt && !created.locked) {
      try { final = await api.updateGlossary(created.id, { locked: true }); }
      catch (_) { /* keep created */ }
    }
    const idx = entries.findIndex(x => x.id === final.id);
    if (idx >= 0) entries[idx] = final; else entries.push(final);
    closeAddCard();
    renderCatRail();
    render();
    const row = rowsEl.querySelector(`[data-id="${final.id}"]`);
    if (row) { row.classList.add("flash"); setTimeout(() => row.classList.remove("flash"), 1500); }
    showToast(`Added “${final.term_zh}”.`, "ok");
  } catch (e) {
    showToast(e.message, "err");
  }
}

addBtn.addEventListener("click", () => {
  if (addOpen) closeAddCard(); else openAddCard();
});

/* ============================================================
   Search + sort + filter + view toggle wiring.
   ============================================================ */
let searchTimer = null;
searchEl.addEventListener("input", () => {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(render, 180);
});
sortByEl.addEventListener("change", render);

filterModeEl.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-filter]");
  if (!btn) return;
  filterModeEl.querySelectorAll("button").forEach(b => {
    const active = b === btn;
    b.classList.toggle("on", active);
    b.setAttribute("aria-pressed", active ? "true" : "false");
  });
  filterMode = btn.dataset.filter;
  render();
});

viewToggleEls.forEach(b => {
  b.addEventListener("click", () => switchView(b.dataset.view));
});
function switchView(v) {
  if (v !== "cards" && v !== "list") return;
  view = v;
  localStorage.setItem(LS_VIEW_KEY, view);
  viewToggleEls.forEach(b => b.classList.toggle("on", b.dataset.view === view));
  render();
}

/* ============================================================
   Keyboard shortcuts (page-level).
   ============================================================ */
document.addEventListener("keydown", (ev) => {
  const tag = (ev.target.tagName || "").toUpperCase();
  const isTyping = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || ev.target.isContentEditable;

  // ⌘K / ctrl+K. Focus search. Allowed even while typing in another field.
  if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") {
    ev.preventDefault();
    searchEl.focus();
    searchEl.select();
    return;
  }

  if (isTyping) return;

  if (ev.key === "/") { ev.preventDefault(); searchEl.focus(); return; }
  if (ev.key.toLowerCase() === "n") { ev.preventDefault(); openAddCard(); return; }
  if (ev.key.toLowerCase() === "v") { ev.preventDefault(); switchView(view === "cards" ? "list" : "cards"); return; }
  if (ev.key === "Escape") {
    if (editingId != null) { closeEdit(); return; }
    if (addOpen) { closeAddCard(); return; }
    if (selected.size > 0) {
      selected.clear();
      rowsEl.querySelectorAll(".selected").forEach(c => c.classList.remove("selected"));
      renderBulkBar();
      return;
    }
  }
});

/* ============================================================
   Load + initial render.
   ============================================================ */
async function load() {
  try {
    const novel = await api.novel(novelId);
    headTitle.textContent = `Glossary · ${novel.title}`;
    document.getElementById("novel-title").textContent = `Glossary: ${novel.title}`;
    const crumbNovel = document.getElementById("crumb-novel");
    if (crumbNovel) {
      crumbNovel.textContent = novel.title;
      crumbNovel.href = `/reader?novel=${novelId}&ch=1`;
    }
    entries = await api.glossary(novelId);
    headSub.textContent = `${entries.length} terms · ${entries.filter(e => e.locked).length} locked.`;
    renderCatRail();
    render();
    // Health check is best-effort. Don't block the page on it.
    loadHealth();
  } catch (e) {
    rowsEl.innerHTML = `<p class="status err">${escapeHtml(e.message)}</p>`;
  }
}

load();
