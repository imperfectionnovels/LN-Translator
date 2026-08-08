/* ---- Reading-surface glossary tools (restored 2026-08-06) ----
 *
 * The CAT pivot (Phase 6) moved ALL glossary tooling to the editor and left
 * the reader with no in-place way to fix a term spotted mid-chapter. This
 * module restores the selection flow in a reading-surface form: select a
 * phrase in either pane (or the aligned bilingual grid), then Add it to the
 * novel glossary or Revise the existing entry without leaving the chapter.
 *
 * Term highlighting returned 2026-08-07: the marks themselves are built by the
 * shared term-marks.js and rendered by reader-chapter.js, and THIS module owns
 * the click, which opens the same Revise mini dialog the selection popover
 * uses. One save-and-propagate path, whether the user reached it by selecting
 * a phrase or by clicking an existing mark.
 *
 * Still deliberately lean: no contenteditable, no delete (the glossary page
 * owns destructive ops), no concordance (editor tools). The mini form covers
 * term_zh / term_en / category / locked / notes; the Revise form deep-links to
 * the full entry on the glossary page.
 *
 * Load order: after reader-chapter.js (uses its loadChapter + renderers'
 * shared state), before reader-quality.js. Classic scripts, shared scope:
 * novelId / currentCh / glossaryCache live in reader-core.js; escapeHtml /
 * copyText / confirmDialog live in utils.js. */

const glossaryMiniDialog = document.getElementById("glossary-mini-dialog");

// Slash-split aliases: "筑基 / 築基" matches either surface form. Mirrors
// editor-tools.js::zhAliases (and the backend's slash-alternative handling)
// so a selection of one alias finds the entry instead of minting a duplicate.
function _glossAliases(s) {
  return String(s || "").split("/").map(x => x.trim()).filter(Boolean);
}

// Find the glossary entry a selection refers to. Chinese side: exact alias
// match. English side: case-insensitive alias match.
function _glossFindEntry(text, inZh) {
  const t = String(text || "").trim();
  if (!t) return null;
  const lc = t.toLowerCase();
  return (glossaryCache || []).find(g => inZh
    ? _glossAliases(g.term_zh).includes(t)
    : _glossAliases(g.term_en).some(a => a.toLowerCase() === lc)
  ) || null;
}

/* ---- Selection popover ---- */

let _selPopEl = null;

function _clearSelPop() {
  if (_selPopEl) { _selPopEl.remove(); _selPopEl = null; }
}

function _showSelPopover() {
  _clearSelPop();
  if (document.querySelector("dialog[open]")) return;
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) return;
  const text = sel.toString().trim();
  if (!text || text.length > 60) return;
  const range = sel.getRangeAt(0);
  // Only react inside the reading panes: #body-zh / #body-en, or the aligned
  // grid's .src / .tgt cells.
  const node = range.commonAncestorContainer;
  const el = node.nodeType === 1 ? node : node.parentElement;
  const alignedEl = document.getElementById("aligned-body");
  const inAligned = !!(alignedEl && alignedEl.contains(node));
  if (!bodyEn.contains(node) && !bodyZh.contains(node) && !inAligned) return;
  const inZh = inAligned ? !!(el && el.closest(".src")) : bodyZh.contains(node);
  const rect = range.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return;

  const existing = _glossFindEntry(text, inZh);

  _selPopEl = document.createElement("div");
  _selPopEl.className = "sel-pop";
  if (existing) {
    _selPopEl.innerHTML = `
      <span class="sel-pop-hint">${escapeHtml(existing.term_zh)} ↔ ${escapeHtml(existing.term_en)}</span>
      <span class="sep"></span>
      <button data-act="revise">✎ Revise</button>
      <span class="sep"></span>
      <button data-act="copy">＂ Copy</button>
    `;
  } else {
    _selPopEl.innerHTML = `
      <button data-act="add">＋ Add to glossary</button>
      <span class="sep"></span>
      <button data-act="copy">＂ Copy</button>
    `;
  }
  document.body.appendChild(_selPopEl);
  // Clamp to viewport on all four sides so selecting near an edge doesn't
  // render the popover off-screen.
  const desiredTop = window.scrollY + rect.top - _selPopEl.offsetHeight - 10;
  const desiredLeft = window.scrollX + rect.left + (rect.width / 2) - (_selPopEl.offsetWidth / 2);
  const maxLeft = window.scrollX + window.innerWidth - _selPopEl.offsetWidth - 8;
  const maxTop = window.scrollY + window.innerHeight - _selPopEl.offsetHeight - 8;
  _selPopEl.style.top = `${Math.min(Math.max(window.scrollY + 8, desiredTop), maxTop)}px`;
  _selPopEl.style.left = `${Math.min(Math.max(8, desiredLeft), maxLeft)}px`;

  _selPopEl.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    if (btn.dataset.act === "copy") {
      const ok = await copyText(text);
      showFloatToast(ok ? "Copied" : "Copy failed", rect);
      _clearSelPop();
      window.getSelection()?.removeAllRanges();
      return;
    }
    _clearSelPop();
    if (btn.dataset.act === "add") _openGlossForm({ text, inZh });
    else if (btn.dataset.act === "revise") _openGlossForm({ entry: existing });
  });
}

document.addEventListener("mouseup", (e) => {
  if (_selPopEl && _selPopEl.contains(e.target)) return;
  // Defer so the browser finishes updating the selection first.
  setTimeout(_showSelPopover, 0);
});
document.addEventListener("scroll", _clearSelPop, { passive: true });

/* ---- Mini add/revise form ---- */

const _GLOSS_CATS = ["character", "place", "technique", "item", "idiom", "other"];

// One form for both flows. Add: { text, inZh } prefills the selected side.
// Revise: { entry } pins term_zh and edits the rest. The Locked checkbox
// defaults CHECKED in both flows: a manual add locks server-side, and a
// PATCH implicitly locks on change, so checked is the post-save state
// (see the 2026-06-11 revise-lock-clobber fix); unchecking is the explicit
// "let auto-detection keep updating this" opt-out.
function _openGlossForm(opts) {
  if (!glossaryMiniDialog) return;
  const entry = opts.entry || null;
  const zh = entry ? entry.term_zh : (opts.inZh ? opts.text : "");
  const en = entry ? entry.term_en : (opts.inZh ? "" : opts.text);
  const cat = (entry && entry.category) || "other";
  const notes = (entry && entry.notes) || "";
  const zhField = entry
    ? `<div class="form-row"><label>Chinese</label>
         <div class="gm-zh" lang="zh">${escapeHtml(zh)}${entry.locked ? " 🔒" : ""}</div>
       </div>`
    : `<div class="form-row"><label for="gm-zh">Chinese</label>
         <input id="gm-zh" value="${escapeHtml(zh)}" lang="zh" autocomplete="off"></div>`;
  glossaryMiniDialog.innerHTML = `
    <h3>${entry ? "Revise glossary entry" : "Add to glossary"}</h3>
    ${zhField}
    <div class="form-row"><label for="gm-en">English</label>
      <input id="gm-en" value="${escapeHtml(en)}" autocomplete="off"></div>
    <div class="form-row"><label for="gm-cat">Category</label>
      <select id="gm-cat">
        ${_GLOSS_CATS.map(c => `<option value="${c}"${c === cat ? " selected" : ""}>${c}</option>`).join("")}
      </select></div>
    <div class="form-row"><label for="gm-notes">Notes</label>
      <input id="gm-notes" value="${escapeHtml(notes)}" autocomplete="off"
             placeholder="e.g. 'lowercase' to exempt from Title-Case"></div>
    <div class="form-row gm-lock-row">
      <label class="gm-lock"><input id="gm-lock" type="checkbox" checked>
        Locked <span class="muted">(auto-detection never overwrites)</span></label>
    </div>
    <div class="gm-err muted" id="gm-err"></div>
    <div class="actions">
      ${entry ? `<a class="btn-ghost" href="/glossary?novel=${novelId}&focus=${entry.id}">Full entry ↗</a>` : ""}
      <span style="flex:1;"></span>
      <button type="button" class="btn-ghost" id="gm-cancel">Cancel</button>
      <button type="button" class="btn-primary" id="gm-save">${entry ? "Save" : "Add"}</button>
    </div>
  `;
  glossaryMiniDialog.showModal();
  glossaryMiniDialog.querySelector("#gm-en").focus();
  glossaryMiniDialog.querySelector("#gm-cancel").addEventListener("click", () => glossaryMiniDialog.close());
  glossaryMiniDialog.querySelector("#gm-save").addEventListener("click", () => _saveGlossForm(entry));
  // Property assignment (not addEventListener): each open replaces the
  // previous handler, so stale entry closures never accumulate.
  glossaryMiniDialog.onkeydown = (e) => {
    if (e.key === "Enter" && e.target.tagName === "INPUT") {
      e.preventDefault();
      _saveGlossForm(entry);
    }
  };
}

async function _saveGlossForm(entry) {
  if (!glossaryMiniDialog) return;
  const errEl = glossaryMiniDialog.querySelector("#gm-err");
  const en = glossaryMiniDialog.querySelector("#gm-en").value.trim();
  const cat = glossaryMiniDialog.querySelector("#gm-cat").value;
  const notes = glossaryMiniDialog.querySelector("#gm-notes").value.trim();
  const locked = glossaryMiniDialog.querySelector("#gm-lock").checked;
  try {
    if (entry) {
      if (!en) { errEl.textContent = "English rendering is required."; return; }
      const oldEn = entry.term_en;
      await api.updateGlossary(entry.id, {
        term_en: en, category: cat, notes: notes || null, locked,
      });
      // Mirror the CAT editor's behavior: a changed rendering rewrites the
      // stored chapters in place (word-boundary, case-sensitive) so the fix
      // is visible in the chapter being read right now. The glossary page's
      // three-way dialog remains the surface for retranslate-instead.
      if (en !== oldEn) {
        try { await api.glossaryApplyInPlace(entry.id, oldEn, en); }
        catch (_) { /* entry saved; the apply is best-effort */ }
      }
    } else {
      const zh = glossaryMiniDialog.querySelector("#gm-zh").value.trim();
      if (!zh || !en) { errEl.textContent = "Chinese and English are required."; return; }
      const created = await api.createGlossary(novelId, {
        term_zh: zh, term_en: en, category: cat, notes: notes || null,
      });
      // Manual adds lock server-side; an unchecked box is the explicit
      // opt-out, applied as a follow-up PATCH (same pattern as glossary.js).
      if (!locked && created && created.locked) {
        try { await api.updateGlossary(created.id, { locked: false }); }
        catch (_) { /* keep created */ }
      }
    }
    glossaryMiniDialog.close();
    try { glossaryCache = await api.glossary(novelId); } catch (_) { /* stale cache is fine */ }
    // Re-render the chapter so an applied-in-place revision shows immediately.
    if (entry && typeof loadChapter === "function") loadChapter(currentCh);
  } catch (err) {
    errEl.textContent = (err && err.message) || "Save failed.";
  }
}

/* ---- Term click: open Revise ----
 * A rendered .term span carries data-entry-id, so the click resolves the entry
 * with one lookup instead of scanning the glossary by text. Opens the SAME
 * mini dialog the selection popover's Revise action uses, which is what keeps
 * the reader on a single save-and-propagate path. */
function _openTermFromSpan(e) {
  const span = e.target.closest(".term");
  if (!span) return;
  // Drag wins: while a live selection exists the user is selecting text, not
  // clicking a term. The selection popover handles that gesture instead.
  const sel = window.getSelection();
  if (sel && !sel.isCollapsed && sel.toString().trim()) return;
  const id = Number(span.dataset.entryId);
  if (!Number.isFinite(id)) return;
  const entry = (glossaryCache || []).find(g => g.id === id);
  // A span left over from a deleted entry simply does nothing.
  if (!entry) return;
  e.preventDefault();
  _clearSelPop();
  _openGlossForm({ entry });
}

bodyEn.addEventListener("click", _openTermFromSpan);
bodyZh.addEventListener("click", _openTermFromSpan);
// The aligned grid host is static markup, so one bind survives every re-render.
document.getElementById("aligned-body")?.addEventListener("click", _openTermFromSpan);

// Util-menu Glossary link (novel-scoped, set once).
(() => {
  const link = document.getElementById("open-glossary");
  if (link) link.href = `/glossary?novel=${novelId}`;
})();
