// Returns [{ entry, count }] for every glossary term occurring in `ch`,
// ordered by first appearance in the English body (the side being edited),
// with terms that only surface on the Chinese side appended after. Counts sum
// occurrences across both panes. Reuses buildTermPattern() + termInfo().
function collectChapterTerms(ch) {
  if (!ch) return [];
  const pattern = buildTermPattern();
  if (!pattern) return [];
  const found = new Map(); // entry.id -> { entry, count, firstIdx, firstSide }
  const scan = (text, side, re) => {
    if (!re || !text) return;
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(text))) {
      const g = termInfo(m[0], side);
      // Guard against a zero-width match wedging the loop (defensive; our
      // patterns never match empty, but advancing keeps this total).
      if (m.index === re.lastIndex) re.lastIndex++;
      if (!g) continue;
      const cur = found.get(g.id);
      if (cur) {
        cur.count++;
      } else {
        found.set(g.id, { entry: g, count: 1, firstIdx: m.index, firstSide: side });
      }
    }
  };
  // English first so the ordering matches reading order in the edited pane.
  scan(_displayedEnglish(ch), "en", pattern.en);
  scan(ch.original_text || "", "zh", pattern.zh);
  return Array.from(found.values()).sort((a, b) => {
    if (a.firstSide !== b.firstSide) return a.firstSide === "en" ? -1 : 1;
    return a.firstIdx - b.firstIdx;
  });
}

function renderTermsRail(ch) {
  if (!termsList) return;
  // Compute only in edit mode; the rail is hidden otherwise, and skipping the
  // scan keeps read-mode renders cheap.
  const items = (readerMode === "edit") ? collectChapterTerms(ch) : [];
  if (termsCount) termsCount.textContent = items.length ? `· ${items.length}` : "";
  if (!items.length) {
    termsList.innerHTML =
      `<div class="terms-empty">No glossary terms appear in this chapter. Select any phrase in the body and choose <em>Add to glossary</em> to create one.</div>`;
    return;
  }
  termsList.innerHTML = items.map(({ entry, count }) => {
    const cat = entry.category || "other";
    const lock = entry.locked ? `<span class="tc-lock" title="Locked">🔒</span>` : "";
    const cnt = count > 1 ? `<span class="tc-count">(${count})</span>` : "";
    return `
      <div class="term-card" data-id="${entry.id}" role="button" tabindex="0" title="Click to find it in the chapter · ✎ to edit">
        <div class="tc-main">
          <div class="tc-zh">${escapeHtml(entry.term_zh)}</div>
          <div class="tc-en">${escapeHtml(entry.term_en)}</div>
        </div>
        <button type="button" class="tc-edit" data-act="edit" title="Edit this term" aria-label="Edit ${escapeHtml(entry.term_en)}">✎</button>
        <div class="tc-foot">
          <span class="tc-cat">${escapeHtml(cat)}</span>
          ${cnt}
          ${lock}
        </div>
      </div>`;
  }).join("");
}

// Scrolls the visible body to the first highlighted occurrence of `entry` and
// flashes it. Matches by entry id via termInfo so casing differences between
// the stored term and the prose don't matter. Prefers the aligned edit grid,
// then the EN pane, then the ZH pane.
function _jumpToTermInBody(entry, card) {
  const aligned = document.getElementById("aligned-body");
  const enHost = (aligned && !aligned.hidden && aligned.innerHTML) ? aligned : bodyEn;
  let target = null;
  const pick = (host, side) => {
    if (target || !host) return;
    const spans = host.querySelectorAll(".term");
    for (const span of spans) {
      const shown = (span.textContent || "").trim();
      const g = termInfo(shown, side)
        || termInfo(span.getAttribute("data-term") || "", side === "en" ? "zh" : "en");
      if (g && g.id === entry.id) { target = span; break; }
    }
  };
  pick(enHost, "en");
  if (!target) pick((aligned && !aligned.hidden && aligned.innerHTML) ? aligned : bodyZh, "zh");
  if (target) {
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.add("term-flash");
    setTimeout(() => target.classList.remove("term-flash"), 1200);
  } else if (card) {
    showFloatToast("Not highlighted in the current view", card.getBoundingClientRect());
  }
}

if (termsList) {
  termsList.addEventListener("click", (ev) => {
    const card = ev.target.closest(".term-card");
    if (!card) return;
    const id = Number(card.getAttribute("data-id"));
    const entry = glossaryCache.find(g => g.id === id);
    if (!entry) return;
    if (ev.target.closest("[data-act='edit']")) {
      ev.preventDefault();
      showTermEditPop(card, entry, "en");
    } else {
      _jumpToTermInBody(entry, card);
    }
  });
  // Keyboard parity: Enter / Space on a focused card opens its editor.
  termsList.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const card = ev.target.closest(".term-card");
    if (!card) return;
    ev.preventDefault();
    const entry = glossaryCache.find(g => g.id === Number(card.getAttribute("data-id")));
    if (entry) showTermEditPop(card, entry, "en");
  });
}

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
      // Leave the selection intact (don't removeAllRanges) so the reference
      // text stays copyable with Ctrl+C / right-click → Copy after the
      // click-to-fill. The fill is additive; copy must still work here.
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
  // The Locked checkbox renders CHECKED regardless of the row's current
  // state: a revision is an endorsement of the new rendering, and the
  // server's update_entry() would implicitly lock on any edit anyway.
  // Unchecking is the explicit opt-out. It used to default to the row's
  // current state, which for auto-detected entries silently sent
  // locked=false and let the next translation's merge_new_terms revert
  // the user's rendering (the upsert overwrites WHERE locked = 0).
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
        <input type="checkbox" id="gf-locked" checked>
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
    const alignedEl = document.getElementById("aligned-body");
    if (bodyEn.contains(node) || bodyZh.contains(node) || (alignedEl && alignedEl.contains(node))) active = true;
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
    if (res.in_flight_translate) {
      statusEl.className = "status info";
      statusEl.textContent = `Cancelled chapter ${chapterNum}'s translation.`;
    } else if (res.cancelled_translate) {
      statusEl.className = "status info";
      statusEl.textContent = `Chapter ${chapterNum} removed from the translation queue.`;
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
  // Re-arm the cancel button each time the loader appears (a prior cancel
  // leaves it disabled). Refinement isn't user-cancellable, so only offer
  // cancel for the translate stage.
  if (loaderCancel) {
    const cancelable = stage === "translate";
    loaderCancel.hidden = !cancelable;
    loaderCancel.disabled = false;
    loaderCancel.textContent = "Cancel translation";
  }
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
  // Navigation guard: if there are unsaved paragraph edits on the current
  // chapter and the user is navigating away, confirm before discarding them.
  // Must run before any DOM rewrite so the failed paragraphs are still visible.
  if (currentCh !== num && _failedEdits.size > 0) {
    const ok = await confirmDialog({
      title: "Unsaved edit",
      body: "<p>A paragraph edit failed to save and will be lost if you leave this chapter.</p>",
      okText: "Leave anyway", cancelText: "Stay", danger: true,
    });
    if (!ok) return;
    _failedEdits.clear();
  }

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
      _pendingTurnDir = num > currentCh ? "next" : "prev";
    }
    currentCh = num;
    persistReadingPosition(num);
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
    // Refresh the library-strip snippet for a cache-hit done chapter; the full
    // render path (below) does this at line ~2387, but the prefetch shortcut
    // bypasses it, so without this the breadcrumb keeps the lastLine:null that
    // persistReadingPosition just wrote.
    if (cachedCh.status === "done") {
      persistLastRead(cachedCh);
      _prefetchNext(num);
    }
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
    _pendingTurnDir = num > currentCh ? "next" : "prev";
  }
  currentCh = num;
  persistReadingPosition(num);
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
    // Staleness guard: a slow fetch can resolve after the user has navigated
    // to a different chapter. Bail so a stale response can't overwrite the
    // current chapter's body, banners, or poll loop.
    if (num !== currentCh) return;
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
      // Refresh the per-chapter terms rail here too: this branch early-returns
      // before renderChapterBody (which is the rail's usual refresh point), so
      // without this the rail would stay stuck on the PREVIOUS chapter's terms
      // when navigating into a not-yet-translated chapter. collectChapterTerms
      // falls back to the raw ZH source when there's no translation yet.
      renderTermsRail(ch);
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
          `<p><button type="button" class="btn-ghost" id="cancel-queued">Cancel</button></p>` +
          zhDetails;
        document.getElementById("cancel-queued")?.addEventListener("click", (e) => {
          cancelOneFromQueue(num, e.currentTarget);
        });
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
        void confirmDialog({ title: "Retry failed", body: `<p>${escapeHtml(e.message)}</p>`, okText: "OK", cancelText: "" });
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
    // Staleness guard: the user may have navigated away before this async
    // fetch resolved; don't paint a stale chapter's preview / candidate list.
    if (num !== currentCh) return;
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
  // Page-turn: play the stashed direction now that the real body paints (the
  // stash was set in loadChapter's chapter-change guard). Same-chapter poll
  // re-renders don't set it, so they don't animate.
  if (_pendingTurnDir) { _playPageTurn(_pendingTurnDir); _pendingTurnDir = null; }
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
  // Feature B: paragraph-aligned grid. Pair source + translation paragraphs
  // into shared rows wherever the ZH pane is visible (edit mode AND bilingual
  // read mode): the translator merges paragraphs and drops boilerplate, so
  // independent panes drift visibly off by the chapter tail. Keep the legacy
  // bodies populated (above) as the fallback + classic-mode surface;
  // _buildAlignedRows returns null (so we stay on the legacy panes) in single
  // column or when the counts diverge too far to align.
  const alignedEl = document.getElementById("aligned-body");
  let aligned = false;
  if ((readerMode === "edit" || dualMode) && alignedEl) {
    const rows = _buildAlignedRows(ch.original_text || "", enSource, pattern);
    if (rows) {
      alignedEl.innerHTML = rows;
      alignedEl.hidden = false;
      aligned = true;
    }
  }
  if (!aligned && alignedEl) { alignedEl.hidden = true; alignedEl.innerHTML = ""; }
  stage.dataset.aligned = aligned ? "on" : "off";
  // F14 (2026-05-25): pre-render next chapter so Next click feels
  // instant. Only fires when the current chapter is done; pending /
  // translating chapters skip (no point cacheing what isn't ready).
  if (ch.status === "done") _prefetchNext(ch.chapter_num);
  applyGlossaryMergeBanner(ch);
  applyQualityBanner(ch);
  applyRefinementBanner(ch);
  // Refresh the per-chapter glossary cards rail. Runs on every body render, so
  // it tracks chapter nav, poll re-renders, mode flips, and post-edit
  // re-renders (showTermEditPop ends here) with no extra wiring.
  renderTermsRail(ch);
  // On-demand consistency rail: caches the chapter and refetches only when the
  // rail is open (no-op otherwise), so chapter open stays cheap.
  renderConsistencyRail(ch);
  // Per-chapter quality badge (cockpit). Guarded: reader-quality.js loads after
  // this module, but by the time a chapter actually renders (after awaited
  // network loads) it has executed. Edit-mode-only gating lives in the fn.
  if (typeof renderQualityBadge === "function") renderQualityBadge(ch);
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
        void confirmDialog({ title: "Retranslate refused", body: `<p>${escapeHtml(body.detail || resp.statusText)}</p>`, okText: "OK", cancelText: "" });
        return;
      }
      // Worker is queued. loadChapter re-fetches the row; the existing poll
      // loop picks up state changes and re-renders the banner / clears it on
      // success.
      loadChapter(ch.chapter_num);
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "↻ Retry recovery";
      void confirmDialog({ title: "Recovery request failed", body: `<p>${escapeHtml(String(err))}</p>`, okText: "OK", cancelText: "" });
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

let _lastPersistedCh = null;
let _posSaveTimer = null;
// Record which chapter the reader is currently on. Writes BOTH the durable DB
// position (survives a WebView2 storage wipe) and a localStorage breadcrumb,
// for ANY opened chapter regardless of translation status. The user is "on"
// this chapter even while it's still pending/translating, so reopening must
// resume here — gating this on status='done' (the old behavior) meant a novel
// whose chapters hadn't finished translating never recorded a position and
// every reload fell back to chapter 1. Deduped on _lastPersistedCh so a
// same-chapter poll re-render is a no-op (and doesn't clobber the lastLine
// snippet persistLastRead sets), and debounced so rapid prev/next doesn't spam
// the endpoint. Best-effort: a failed DB write never disrupts reading — the
// breadcrumb remains the fallback.
function persistReadingPosition(num) {
  if (!Number.isInteger(num) || num <= 0) return;
  if (num === _lastPersistedCh) return;
  _lastPersistedCh = num;
  // Breadcrumb resets `lastLine`: it belongs to the chapter we're landing on,
  // and persistLastRead fills it in once that chapter renders as 'done'.
  try {
    localStorage.setItem(`lastRead:${novelId}`, JSON.stringify({
      ch: num, ts: Date.now(), lastLine: null,
    }));
  } catch (_) { /* storage disabled/full — the DB copy below still persists */ }
  if (_posSaveTimer) clearTimeout(_posSaveTimer);
  _posSaveTimer = setTimeout(() => {
    api.setReadingPosition(novelId, num).catch(() => {});
  }, 800);
}
// Refresh the breadcrumb's prose snippet for the library hero strip. Only a
// finished chapter has displayable English, so this stays done-gated. The DB
// position is already recorded by persistReadingPosition on chapter open, so
// this no longer drives resume — it only keeps the `lastLine` snippet current.
function persistLastRead(ch) {
  if (ch.status !== "done") return;
  const displayed = _displayedEnglish(ch);
  const paras = String(displayed).split(/\n\s*\n/).map(p => p.trim()).filter(Boolean);
  const lastLine = paras.length ? paras[paras.length - 1].slice(0, 240) : null;
  try {
    localStorage.setItem(`lastRead:${novelId}`, JSON.stringify({
      ch: ch.chapter_num, ts: Date.now(), lastLine,
    }));
  } catch (_) { /* storage disabled/full — snippet is a nice-to-have */ }
}

