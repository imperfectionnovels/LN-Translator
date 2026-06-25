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

// Cancel the in-flight translation from the loading screen. activeLoader
// carries the chapter currently being processed.
loaderCancel?.addEventListener("click", () => {
  const num = activeLoader ? activeLoader.chapterNum : currentCh;
  loaderCancel.textContent = "Cancelling…";
  cancelOneFromQueue(num, loaderCancel);
});

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
  function _styleNoteDirty() {
    const orig = ((novelMeta && novelMeta.style_note) || "").trim();
    return styleNoteText.value.trim() !== orig;
  }
  styleNoteCancel.addEventListener("click", () => {
    if (_styleNoteDirty()) {
      confirmDialog({
        title: "Discard changes?",
        body: "<p>Your style note edits have not been saved.</p>",
        okText: "Discard", cancelText: "Keep editing", danger: true,
      }).then(ok => { if (ok) styleNoteDialog.close(); });
    } else {
      styleNoteDialog.close();
    }
  });
  styleNoteDialog.addEventListener("cancel", (e) => {
    if (_styleNoteDirty()) {
      e.preventDefault();
      confirmDialog({
        title: "Discard changes?",
        body: "<p>Your style note edits have not been saved.</p>",
        okText: "Discard", cancelText: "Keep editing", danger: true,
      }).then(ok => { if (ok) styleNoteDialog.close(); });
    }
  });
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

// Insert a chapter that was missed during import into the MIDDLE of the novel.
// Edit-mode-only util-menu action; mirrors the style-note dialog wiring. All
// derefs are null-guarded so a cached reader.html without the markup just
// no-ops rather than throwing (which would kill the rest of reader.js).
const insertChapterBtn = document.getElementById("insert-chapter");
const insertChapterDialog = document.getElementById("insert-chapter-dialog");
const insertAfterNum = document.getElementById("insert-after-num");
const insertTitleInput = document.getElementById("insert-title");
const insertTextArea = document.getElementById("insert-text");
const insertStatus = document.getElementById("insert-status");
const insertSave = document.getElementById("insert-save");
const insertCancel = document.getElementById("insert-cancel");
function _stashInsertDraft() {
  const text = insertTextArea ? (insertTextArea.value || "") : "";
  if (text.trim()) {
    _insertDraft = {
      after: insertAfterNum ? insertAfterNum.value : "",
      title: insertTitleInput ? insertTitleInput.value : "",
      text,
    };
  }
}
if (insertChapterBtn && insertChapterDialog) {
  insertChapterBtn.addEventListener("click", () => {
    if (_insertDraft) {
      // Restore the stashed draft so the user can continue where they left off.
      insertAfterNum.value = _insertDraft.after;
      insertTitleInput.value = _insertDraft.title;
      insertTextArea.value = _insertDraft.text;
      insertStatus.textContent = "Draft restored from this session.";
    } else {
      // Default to "after the chapter you're reading"; the common case is
      // noticing a gap right where you are.
      insertAfterNum.value = String(Number.isInteger(currentCh) ? currentCh : 0);
      insertTitleInput.value = "";
      insertTextArea.value = "";
      insertStatus.textContent = "";
    }
    insertChapterDialog.showModal();
  });
  insertCancel.addEventListener("click", () => {
    _stashInsertDraft();
    insertChapterDialog.close();
    if (_insertDraft) showToast("Draft kept. Reopen Insert chapter to continue.", "info");
  });
  insertChapterDialog.addEventListener("cancel", () => {
    _stashInsertDraft();
    if (_insertDraft) showToast("Draft kept. Reopen Insert chapter to continue.", "info");
    // The dialog still closes; no preventDefault.
  });
  insertSave.addEventListener("click", async () => {
    const after = parseInt(insertAfterNum.value, 10);
    const text = (insertTextArea.value || "").trim();
    if (!Number.isInteger(after) || after < 0) {
      insertStatus.textContent = "Enter a valid 'after chapter' number (0 or more).";
      return;
    }
    if (!text) {
      insertStatus.textContent = "Paste the chapter text first.";
      return;
    }
    insertSave.disabled = true;
    insertStatus.textContent = "Inserting…";
    try {
      const r = await api.insertChapter(novelId, after, text, insertTitleInput.value.trim() || null);
      _insertDraft = null; // Clear the draft on successful insert.
      insertStatus.textContent = `Inserted ${r.added_chapters} chapter(s) at ${r.first_new_chapter}. Opening…`;
      // Nudge any other open tab for this novel to refresh its TOC.
      try {
        const bc = new BroadcastChannel("novel-changes");
        bc.postMessage({ novel_id: Number(novelId), type: "inserted" });
        bc.close();
      } catch (_) { /* ignore */ }
      setTimeout(() => {
        location.href = `/reader?novel=${novelId}&ch=${r.first_new_chapter || 1}`;
      }, 500);
    } catch (err) {
      insertStatus.textContent = `Insert failed: ${err.message || err}`;
      insertSave.disabled = false;
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
  const ok = await copyText(text);
  showFloatToast(ok ? "Chapter copied" : "Copy failed", copyChapterBtn.getBoundingClientRect());
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

// The active translation surface: the aligned grid (edit mode, when
// renderChapterBody set data-aligned="on") or the legacy #body-en. The edit
// machinery (contenteditable paragraphs, paragraph-index capture, scroll-to-
// paragraph) routes through these so it works in both layouts.
function _activeParaContainer() {
  if (stage.dataset.aligned === "on") {
    return document.getElementById("aligned-body") || bodyEn;
  }
  return bodyEn;
}
function _activeTgtParas() {
  const c = _activeParaContainer();
  // Aligned: the editable units are the <p>s inside .tgt cells only (never the
  // .src Chinese cells). Legacy: the body's own <p>s. Both keep document order,
  // so the Nth <p> maps to the Nth markdown chunk identically in either layout.
  return c === bodyEn
    ? Array.from(c.querySelectorAll("p"))
    : Array.from(c.querySelectorAll(".prow > .tgt p"));
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
  // The aligned grid stamps each target <p> with its true stored-text index at
  // render time (alignment can put several paragraphs in one row, so DOM order
  // is not the chunk index). Honor that stamp; the legacy single-column body
  // has none, so fall back to DOM position there.
  const stamped = p.dataset.paragraphIndex;
  const idx = (stamped !== undefined && stamped !== "")
    ? parseInt(stamped, 10)
    : _activeTgtParas().indexOf(p);
  if (!Number.isFinite(idx) || idx < 0 || idx >= chunks.length) return;
  p.dataset.chapterNum = String(currentCh);
  p.dataset.paragraphIndex = String(idx);
  p.dataset.beforeMd = chunks[idx];
  p.dataset.variant = variant;
}

function applyEditMode() {
  editBtn?.classList.toggle("on", editMode);
  editBtn?.setAttribute("aria-pressed", editMode ? "true" : "false");
  // Clear the edit-mode flag from both possible surfaces so a stale class can't
  // survive a layout switch, then set it on the active one.
  bodyEn.classList.remove("edit-mode");
  const alignedEl = document.getElementById("aligned-body");
  if (alignedEl) alignedEl.classList.remove("edit-mode");
  if (editMode) {
    _activeParaContainer().classList.add("edit-mode");
    _activeTgtParas().forEach(p => {
      p.setAttribute("contenteditable", "true");
      _stripGlossSpans(p);
      _captureParagraphMeta(p);
    });
  } else {
    // Re-render the whole chapter body so glossary highlights come back
    // and any pre-edit markdown is reflected. The contenteditable / dataset
    // attributes vanish naturally when the body (or aligned grid) is rebuilt.
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

// Bind the edit listeners to #pane-en (the common ancestor of #body-en and the
// aligned #aligned-body) so per-paragraph editing works in both layouts. The
// closest("p"...) guards keep them scoped to editable paragraphs only.
const _editHost = document.getElementById("pane-en") || bodyEn;

// Re-capture metadata on focus so a paragraph that becomes editable after a
// re-render (or that was previously the saved-flash target) carries the
// current chunk text and index. focusin bubbles; blur (capture phase) is
// what we use for the save trigger below.
_editHost.addEventListener("focusin", (e) => {
  if (!editMode) return;
  const p = e.target.closest("p[contenteditable='true']");
  if (!p) return;
  // Refresh metadata only if missing — once captured at edit-mode entry
  // it's authoritative for the lifetime of the edit. Re-capturing on every
  // focusin would overwrite a freshly-edited p's before_md with the post-
  // edit text, breaking the next save's verification.
  if (!p.dataset.beforeMd) _captureParagraphMeta(p);
});

// Core save logic extracted so the blur handler and the retry chip can share
// it. `ctx` carries all values captured at focus time so the save is always
// anchored to the original chapter, even if the user navigated away.
async function _commitParagraphSave(p, ctx) {
  const { beforeMd, after, variant, chapterNumAtFocus, paragraphIndex } = ctx;
  const column = variant === "refined" ? "refined_text" : "translated_text";
  const key = `${chapterNumAtFocus}:${paragraphIndex}`;
  p.setAttribute("contenteditable", "false");
  p.classList.add("saving");
  try {
    await api.editParagraph(
      novelId, chapterNumAtFocus, paragraphIndex, beforeMd, after, variant,
    );
    // Update local cache the same way the backend did: split, replace index,
    // rejoin. Only when we're still on the chapter we edited; otherwise
    // lastChapter is for a different chapter and we shouldn't touch it.
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
    // Clear any failed-edit state for this paragraph.
    _failedEdits.delete(key);
    const existingChip = p.nextElementSibling;
    if (existingChip && existingChip.classList.contains("p-save-retry")) {
      existingChip.remove();
    }
    p.classList.remove("saving", "save-failed");
    p.removeAttribute("title");
    p.classList.add("saved-flash");
    setTimeout(() => p.classList.remove("saved-flash"), 1200);
  } catch (err) {
    p.classList.remove("saving");
    p.classList.add("save-failed");
    p.title = `Save failed: ${err.message}`;
    // Stash the ctx so the retry chip and the nav guard can find it.
    _failedEdits.set(key, ctx);
    // Insert a visible retry chip directly after the paragraph (idempotent).
    let chip = p.nextElementSibling;
    if (!chip || !chip.classList.contains("p-save-retry")) {
      chip = document.createElement("button");
      chip.type = "button";
      chip.className = "p-save-retry";
      p.insertAdjacentElement("afterend", chip);
    }
    chip.textContent = "Save failed. Retry";
    chip.onclick = () => _retryFailedEdit(key, chip, p);
    throw err;
  } finally {
    // Re-enable editing only when we're still in edit mode AND still on the
    // chapter this paragraph belongs to. If the user navigated, the <p> is
    // detached from DOM and this is a harmless no-op on a stale node.
    if (editMode && currentCh === chapterNumAtFocus) {
      p.setAttribute("contenteditable", "true");
    }
  }
}

async function _retryFailedEdit(key, chip, p) {
  const ctx = _failedEdits.get(key);
  if (!ctx) return;
  chip.disabled = true;
  chip.textContent = "Retrying…";
  try {
    await _commitParagraphSave(p, ctx);
    // Success: chip and map entry were already cleaned up inside _commitParagraphSave.
  } catch (err) {
    chip.disabled = false;
    chip.textContent = "Save failed. Retry";
    showToast("Save failed again: " + err.message, "err");
  }
}

_editHost.addEventListener("blur", async (e) => {
  // Intentionally NOT gated on `editMode`. When the user exits edit mode or
  // navigates away mid-edit, the focused paragraph loses focus and we still
  // want to flush the pending edit. The presence of data-before-md is the
  // authoritative "this <p> was being edited" signal.
  const p = e.target.closest("p");
  if (!p || !p.dataset.beforeMd) return;
  const beforeMd = p.dataset.beforeMd;
  const after = p.textContent.trim();
  // Use the chapter/index captured AT FOCUS, not the current globals. The
  // user may have navigated chapters while typing.
  const chapterNumAtFocus = parseInt(p.dataset.chapterNum || "", 10);
  const paragraphIndex = parseInt(p.dataset.paragraphIndex || "", 10);
  if (!Number.isFinite(chapterNumAtFocus) || !Number.isFinite(paragraphIndex)) return;
  if (!beforeMd || beforeMd.trim() === after) return;
  // Variant was captured at focus time (paragraph metadata). It tells us
  // which column the backend should mutate AND which column to splice in
  // the local cache. Defaults to 'draft' so a paragraph that somehow lost
  // its dataset still works.
  const variant = p.dataset.variant || "draft";
  const ctx = { beforeMd, after, variant, chapterNumAtFocus, paragraphIndex };
  try {
    await _commitParagraphSave(p, ctx);
  } catch (_err) {
    // Failure is already handled inside _commitParagraphSave (chip + map entry).
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
const pageTurnSelect = document.getElementById("page-turn-select");
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
  if (pageTurnSelect) pageTurnSelect.value = _pageTurnPref();
  _syncTypeReadouts();
  typeDlg.showModal();
}
document.getElementById("type-settings-btn")?.addEventListener("click", openTypeSettings);
document.getElementById("type-settings-close")?.addEventListener("click", () => typeDlg?.close());
document.getElementById("type-settings-reset")?.addEventListener("click", () => {
  localStorage.removeItem("readerFsBody");
  localStorage.removeItem("readerFsLh");
  localStorage.removeItem("readerFocusMode");
  localStorage.removeItem(PAGE_TURN_KEY);
  document.documentElement.style.removeProperty("--fs-body");
  document.documentElement.style.removeProperty("--fs-body-lh");
  document.documentElement.removeAttribute("data-focus-mode");
  if (fsBodySlider) fsBodySlider.value = String(DEFAULT_FS_BODY);
  if (fsLhSlider) fsLhSlider.value = String(DEFAULT_FS_LH);
  if (focusModeToggle) focusModeToggle.checked = false;
  if (pageTurnSelect) pageTurnSelect.value = "shift";
  _syncTypeReadouts();
});
pageTurnSelect?.addEventListener("change", () => {
  localStorage.setItem(PAGE_TURN_KEY, pageTurnSelect.value);
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
    // Resume position: when the URL didn't pin a chapter, land on the last
    // chapter the reader finished rendering instead of always defaulting to
    // chapter 1. Prefer the durable DB position (survives a WebView2 storage
    // wipe); fall back to the localStorage breadcrumb for users whose DB
    // column hasn't been backfilled yet (it backfills on the next open).
    if (!hadExplicitCh) {
      // Use a positive-integer test, NOT Number.isFinite: the API serializes an
      // unset DB column as JSON null, and Number(null) === 0 (finite), which
      // would wrongly skip the localStorage fallback and then fail the cache
      // lookup, dropping the reader on chapter 1. `null → 0` and `undefined →
      // NaN` must both fall through to the breadcrumb.
      let savedCh = Number(novelMeta?.last_read_chapter_num);
      if (!Number.isInteger(savedCh) || savedCh <= 0) {
        try {
          const raw = localStorage.getItem(`lastRead:${novelId}`);
          if (raw) savedCh = Number(JSON.parse(raw)?.ch);
        } catch (_) { /* corrupt breadcrumb — fall through to the default */ }
      }
      if (Number.isInteger(savedCh) && savedCh > 0
          && chaptersCache.some(c => c.chapter_num === savedCh)) {
        currentCh = savedCh;
      }
    }
    // Guard against a non-existent target: default `1` on a partial import
    // that starts at 296, or a deletion gap. chaptersCache is ordered, so
    // [0] is the first chapter.
    if (!chaptersCache.some(c => c.chapter_num === currentCh)) {
      currentCh = chaptersCache[0].chapter_num;
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
    // Skip the background poll when the tab is hidden (no point hitting the DB
    // for a view nobody is looking at) OR when nothing on the server can move
    // the TOC without a user action (_hasLiveWork). The visibilitychange and
    // BroadcastChannel handlers below still refresh on demand, so any
    // staleness self-heals the moment the user returns or another tab appends.
    if (document.visibilityState === "visible" && _hasLiveWork()) {
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

// True when something on the server could still change the chapter list or a
// TOC glyph without user action: a chapter translating or queued, a refinement
// or free-draft in flight, or an import in progress. When false, the periodic
// background poll skips its tick — the TOC cannot change on its own, and the
// viewed chapter keeps its own per-chapter poll regardless.
function _hasLiveWork() {
  if (novelMeta && novelMeta.import_status === "in_progress") return true;
  return chaptersCache.some(c =>
    c.status === "translating" ||
    c.translate_queued ||
    c.refinement_status === "pending" || c.refinement_status === "in_progress" ||
    c.free_draft_status === "pending" || c.free_draft_status === "in_progress"
  );
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
  mt_texture: "MT texture",
  double_possessive: "Double possessive",
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

// Scroll units for the active layout, used as the bookmark/concordance index
// space. Legacy/read: #body-en's direct <p> children. Edit-mode aligned grid:
// the .prow rows (one per paragraph pair). Capture and jump both read this, so
// they stay consistent within a layout; cross-layout jumps can drift by a row
// or two on heading/quote-heavy chapters, which only shifts the smooth-scroll
// landing slightly.
function _activeScrollUnits() {
  if (stage.dataset.aligned === "on") {
    const a = document.getElementById("aligned-body");
    return a ? Array.from(a.querySelectorAll(".prow")) : [];
  }
  const body = document.getElementById("body-en");
  return body ? Array.from(body.children).filter(el => el.tagName === "P") : [];
}

function _currentTopParagraphIndex() {
  // Find the first scroll unit whose bounding rect's bottom is below the
  // chapter-bar (so partially-visible top paragraphs don't get skipped).
  const paras = _activeScrollUnits();
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
  const paras = _activeScrollUnits();
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
  const ok = await copyText(lastPromptBody?.textContent || "");
  lastPromptCopy.textContent = ok ? "Copied" : "Copy failed";
  setTimeout(() => { lastPromptCopy.textContent = "Copy"; }, 1500);
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
