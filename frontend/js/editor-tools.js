// editor-tools.js - the CAT editor's chapter tools (Phase 6 relocations).
//
// Loads AFTER editor-core.js and editor-assist.js (ordered plain script tags,
// shared global scope). Everything here moved from the reader's retired edit
// mode: the util-menu diagnostics (QA observations, attempts, last prompt,
// pre-check), the maintenance actions (refresh free draft, insert chapter,
// style note), the concordance search, the learn-from-edits panel, and the
// glossary tooling (select-to-add popover, terms-in-chapter tier,
// missing-locked-terms tier in the assist rail).
//
// Reads editor-core state directly (novelId, currentCh, currentData,
// focusSeg, readOnly) plus core helpers (segByIndex, rowFor, selectRow,
// gotoChapter, loadSegments) and editor-assist's glossary cache
// (glossaryPromise / loadGlossaryOnce / renderGlossaryChips). Reacts to the
// same editor:* CustomEvents as the assist rail.

/* ---- Small helpers ---- */

// One glossary fetch shared with editor-assist (its `glossaryPromise` is a
// shared top-level binding). Invalidate after any glossary mutation so the
// chips and the chapter tiers re-read fresh entries.
function refreshGlossaryCache() {
  glossaryPromise = null;
  return loadGlossaryOnce();
}

// Re-render every glossary-derived surface after entries changed.
function refreshGlossarySurfaces() {
  renderChapterTermTiers();
  const seg = focusSeg != null && currentData ? segByIndex(focusSeg) : null;
  if (seg && typeof renderGlossaryChips === "function") renderGlossaryChips(seg);
}

// Jump to a segment row, crossing chapters when needed. renderState selects
// rowFor(focusSeg) once the payload lands, so setting focusSeg right after
// gotoChapter() (which starts the async load) is enough for the cross-chapter
// case.
function jumpToSegment(ch, idx) {
  if (ch === currentCh) {
    const row = Number.isFinite(idx) ? rowFor(idx) : null;
    if (row) selectRow(row);
    return;
  }
  gotoChapter(ch);
  if (Number.isFinite(idx)) {
    focusSeg = idx;
    syncUrl();
  }
}

// Provenance chip for CAT-confirmed/edited hits (single remaining copy after
// the reader's two Phase 5 copies retired with it).
function provChipHtml(status) {
  return status === "confirmed" || status === "edited"
    ? `<span class="concordance-prov concordance-prov-${status}" title="You ${status} this rendering in the CAT editor">${status}</span>`
    : "";
}

/* ---- QA observations dialog ---- */

const obsDialog = document.getElementById("observations-dialog");
const obsList = document.getElementById("observations-list");

const OBSERVATION_KIND_LABELS = {
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

async function loadAndRenderObservations() {
  if (!obsList) return;
  obsList.innerHTML = '<p class="muted">Loading…</p>';
  let rows;
  try {
    rows = await api.chapterObservations(novelId, currentCh);
  } catch (e) {
    obsList.innerHTML = `<p class="status err">Failed to load: ${escapeHtml(e.message)}</p>`;
    return;
  }
  if (!rows || !rows.length) {
    obsList.innerHTML = '<p class="observations-empty">No observations for this chapter.</p>';
    return;
  }
  obsList.innerHTML = rows.map(obs => {
    const label = OBSERVATION_KIND_LABELS[obs.kind] || obs.kind;
    return `
      <div class="observation-row" data-obs-id="${obs.id}">
        <div>
          <div class="observation-kind">${escapeHtml(label)}</div>
          <div class="observation-excerpt">${escapeHtml(obs.excerpt)}</div>
        </div>
        <button type="button" class="observation-dismiss" data-dismiss="${obs.id}" title="Dismiss this observation">Dismiss</button>
      </div>`;
  }).join("");
}

document.getElementById("editor-observations")?.addEventListener("click", () => {
  if (!obsDialog) return;
  if (!obsDialog.open) obsDialog.showModal();
  loadAndRenderObservations();
});
document.getElementById("observations-close")?.addEventListener("click", () => obsDialog?.close());
// Delegated dismiss (rows re-render after every dismiss).
obsList?.addEventListener("click", async (e) => {
  const target = e.target.closest("[data-dismiss]");
  if (!target) return;
  const obsId = Number.parseInt(target.dataset.dismiss, 10);
  if (!Number.isFinite(obsId)) return;
  target.disabled = true;
  target.textContent = "…";
  try {
    await api.dismissObservation(obsId);
    await loadAndRenderObservations();
  } catch (err) {
    target.disabled = false;
    target.textContent = "Dismiss";
    showToast(`Dismiss failed: ${err.message}`, "err");
  }
});

/* ---- Translation attempts dialog (F22) ---- */

const attemptsDlg = document.getElementById("attempts-dialog");
const attemptsBody = document.getElementById("attempts-body");

document.getElementById("editor-view-attempts")?.addEventListener("click", async () => {
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

/* ---- Last-prompt dialog (F22) ---- */

const lastPromptDlg = document.getElementById("last-prompt-dialog");
const lastPromptBody = document.getElementById("last-prompt-body");
const lastPromptCopy = document.getElementById("last-prompt-copy");

document.getElementById("editor-view-last-prompt")?.addEventListener("click", async () => {
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

/* ---- Pre-translation check ---- */

document.getElementById("editor-precheck")?.addEventListener("click", async () => {
  let warnings;
  try {
    const r = await api.chapterPreCheck(novelId, currentCh);
    warnings = (r && r.warnings) || [];
  } catch (e) {
    showToast(`Pre-check failed: ${e.message}`, "err");
    return;
  }
  const body = warnings.length
    ? warnings.map(w =>
        `<p><strong>[${escapeHtml(w.severity)}]</strong> ${escapeHtml(w.message)}</p>`
      ).join("")
    : "<p>No warnings. This chapter looks ready to translate.</p>";
  void confirmDialog({
    title: `Pre-translation check · Ch. ${currentCh}`,
    body,
    okText: "Close",
    cancelText: "",
  });
});

/* ---- Refresh free draft ---- */

document.getElementById("editor-refresh-free-draft")?.addEventListener("click", async () => {
  try {
    await api.refreshFreeDraft(novelId, currentCh);
    showToast("Free draft regeneration queued. The new draft appears in the reader shortly.", "ok");
  } catch (e) {
    showToast(`Refresh failed: ${e.message}`, "err");
  }
});

/* ---- Insert chapter ---- */

const insertChapterDialog = document.getElementById("insert-chapter-dialog");
const insertAfterNum = document.getElementById("insert-after-num");
const insertTitleInput = document.getElementById("insert-title");
const insertTextArea = document.getElementById("insert-text");
const insertStatus = document.getElementById("insert-status");
const insertSave = document.getElementById("insert-save");
const insertCancel = document.getElementById("insert-cancel");
// Draft stash preserved across Esc/cancel so pasted text isn't lost.
let insertDraft = null;

function stashInsertDraft() {
  const text = insertTextArea ? (insertTextArea.value || "") : "";
  if (text.trim()) {
    insertDraft = {
      after: insertAfterNum ? insertAfterNum.value : "",
      title: insertTitleInput ? insertTitleInput.value : "",
      text,
    };
  }
}

if (insertChapterDialog) {
  document.getElementById("editor-insert-chapter")?.addEventListener("click", () => {
    if (insertDraft) {
      insertAfterNum.value = insertDraft.after;
      insertTitleInput.value = insertDraft.title;
      insertTextArea.value = insertDraft.text;
      insertStatus.textContent = "Draft restored from this session.";
    } else {
      insertAfterNum.value = String(Number.isInteger(currentCh) ? currentCh : 0);
      insertTitleInput.value = "";
      insertTextArea.value = "";
      insertStatus.textContent = "";
    }
    insertChapterDialog.showModal();
  });
  insertCancel?.addEventListener("click", () => {
    stashInsertDraft();
    insertChapterDialog.close();
    if (insertDraft) showToast("Draft kept. Reopen Insert chapter to continue.", "info");
  });
  insertChapterDialog.addEventListener("cancel", () => {
    stashInsertDraft();
    if (insertDraft) showToast("Draft kept. Reopen Insert chapter to continue.", "info");
  });
  insertSave?.addEventListener("click", async () => {
    const after = Number.parseInt(insertAfterNum.value, 10);
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
      insertDraft = null;
      insertStatus.textContent = `Inserted ${r.added_chapters} chapter(s) at ${r.first_new_chapter}. Opening…`;
      // Nudge any other open tab for this novel to refresh its lists.
      try {
        const bc = new BroadcastChannel("novel-changes");
        bc.postMessage({ novel_id: Number(novelId), type: "inserted" });
        bc.close();
      } catch (_) { /* ignore */ }
      setTimeout(() => {
        location.href = `/editor?novel=${novelId}&ch=${r.first_new_chapter || 1}`;
      }, 500);
    } catch (err) {
      insertStatus.textContent = `Insert failed: ${err.message || err}`;
      insertSave.disabled = false;
    }
  });
}

/* ---- Style note ---- */

const styleNoteDialog = document.getElementById("style-note-dialog");
const styleNoteText = document.getElementById("style-note-text");
const styleNoteStatus = document.getElementById("style-note-status");
const styleNoteSave = document.getElementById("style-note-save");
const styleNoteCancel = document.getElementById("style-note-cancel");
let styleNoteOriginal = "";

if (styleNoteDialog) {
  document.getElementById("editor-style-note")?.addEventListener("click", async () => {
    styleNoteText.value = "";
    styleNoteStatus.textContent = "Loading…";
    styleNoteDialog.showModal();
    try {
      const novel = await api.novel(novelId);
      styleNoteOriginal = (novel.style_note || "").trim();
      styleNoteText.value = novel.style_note || "";
      styleNoteStatus.textContent = "";
    } catch (e) {
      styleNoteStatus.textContent = `Failed to load: ${e.message}`;
    }
  });
  function styleNoteDirty() {
    return styleNoteText.value.trim() !== styleNoteOriginal;
  }
  styleNoteCancel?.addEventListener("click", () => {
    if (styleNoteDirty()) {
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
    if (styleNoteDirty()) {
      e.preventDefault();
      confirmDialog({
        title: "Discard changes?",
        body: "<p>Your style note edits have not been saved.</p>",
        okText: "Discard", cancelText: "Keep editing", danger: true,
      }).then(ok => { if (ok) styleNoteDialog.close(); });
    }
  });
  styleNoteSave?.addEventListener("click", async () => {
    styleNoteSave.disabled = true;
    styleNoteStatus.textContent = "Saving…";
    try {
      await api.updateNovel(novelId, {
        style_note: styleNoteText.value.trim() || null,
      });
      styleNoteOriginal = styleNoteText.value.trim();
      styleNoteStatus.textContent = "Saved.";
      setTimeout(() => styleNoteDialog.close(), 500);
    } catch (err) {
      styleNoteStatus.textContent = `Save failed: ${err.message || err}`;
    } finally {
      styleNoteSave.disabled = false;
    }
  });
}

/* ---- Concordance search ---- */

const concordanceDialog = document.getElementById("concordance-dialog");
const concordanceInput = document.getElementById("concordance-input");
const concordanceSearchBtn = document.getElementById("concordance-search");
const concordanceList = document.getElementById("concordance-list");

function openConcordance(prefill) {
  if (!concordanceDialog) return;
  if (prefill != null) concordanceInput.value = prefill;
  concordanceList.innerHTML = "";
  if (!concordanceDialog.open) concordanceDialog.showModal();
  if (concordanceInput.value.trim()) runConcordanceSearch();
  else setTimeout(() => concordanceInput.focus(), 0);
}

async function runConcordanceSearch() {
  const q = (concordanceInput?.value || "").trim();
  if (q.length < 2) {
    concordanceList.innerHTML = '<p class="muted">Enter at least 2 characters.</p>';
    return;
  }
  concordanceList.innerHTML = '<p class="muted">Searching…</p>';
  let hits;
  try { hits = await api.tmConcordance(novelId, q); }
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
          <div class="concordance-chapter">Ch. ${h.chapter_num} · ${escapeHtml(chTitle)}${provChipHtml(h.status)}${sideTag}</div>
          <div class="concordance-source">${escapeHtml(h.source_text)}</div>
          <div class="concordance-target">${escapeHtml(h.target_text)}</div>
        </div>
        <span class="concordance-paragraph">¶${h.paragraph_index + 1}</span>
      </div>`;
  }).join("");
}

document.getElementById("editor-concordance")?.addEventListener("click", () => openConcordance(null));
concordanceSearchBtn?.addEventListener("click", runConcordanceSearch);
concordanceInput?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); runConcordanceSearch(); }
});
document.getElementById("concordance-close")?.addEventListener("click", () => concordanceDialog?.close());
concordanceList?.addEventListener("click", (e) => {
  const row = e.target.closest(".concordance-hit");
  if (!row) return;
  const ch = Number.parseInt(row.dataset.ch, 10);
  const para = Number.parseInt(row.dataset.para, 10);
  if (!Number.isFinite(ch)) return;
  concordanceDialog?.close();
  jumpToSegment(ch, para);
});

/* ---- Learn from my edits ---- */

const learnDialog = document.getElementById("learn-edits-dialog");
const learnBody = document.getElementById("learn-edits-body");
const learnGt = document.getElementById("learn-edits-gt");
const learnStatus = document.getElementById("learn-edits-status");
const learnApply = document.getElementById("learn-edits-apply");
let learnProposal = null;

function renderLearnProposal(p) {
  if (!p || p.captured_edits === 0) {
    return `<p class="muted">No edited segments in this chapter yet. Rewrite a few
      paragraphs in the grid, then come back.</p>`;
  }
  const glossRows = (p.glossary_casing || []).map(g => `
    <label class="learn-row">
      <input type="checkbox" data-kind="gloss" value="${g.id}" />
      <span><b>${escapeHtml(g.term_zh)}</b> ${escapeHtml(g.term_en)}
        <span class="learn-arrow">→</span> ${escapeHtml(g.proposed_en)}</span>
    </label>`).join("");
  const briefRows = (p.brief || []).map(b => `
    <label class="learn-row">
      <input type="checkbox" data-kind="brief" value="${b.id}" />
      <span>${escapeHtml(b.text)}</span>
    </label>`).join("");
  const parts = [`<p class="muted" style="font-size:12px;">From ${p.captured_edits} edited segment(s).</p>`];
  if (glossRows) parts.push(`<div class="learn-sec">Terminology → glossary</div>${glossRows}`);
  if (briefRows) parts.push(`<div class="learn-sec">Voice → brief note</div>${briefRows}`);
  if (!glossRows && !briefRows) {
    parts.push(`<p class="muted">No new glossary or voice patterns found. You can still
      save this chapter as a quality reference below.</p>`);
  }
  return parts.join("");
}

document.getElementById("editor-learn-edits")?.addEventListener("click", async () => {
  if (!learnDialog) return;
  learnStatus.textContent = "";
  if (learnGt) learnGt.checked = false;
  learnBody.innerHTML = `<p class="muted">Analyzing your edits…</p>`;
  if (!learnDialog.open) learnDialog.showModal();
  try {
    learnProposal = await api.learnEditsStage(novelId, currentCh);
    learnBody.innerHTML = renderLearnProposal(learnProposal);
  } catch (e) {
    learnBody.innerHTML = `<p class="status err">${escapeHtml(e.message)}</p>`;
  }
});
learnDialog?.querySelector("[data-act='cancel']")
  ?.addEventListener("click", () => learnDialog.close());

learnApply?.addEventListener("click", async () => {
  if (!learnProposal) { learnDialog.close(); return; }
  const checked = (kind) => Array.from(
    learnBody.querySelectorAll(`input[data-kind="${kind}"]:checked`)
  ).map(el => el.value);
  const selection = {
    brief: checked("brief"),
    glossary_casing: checked("gloss"),
    save_ground_truth: !!(learnGt && learnGt.checked),
  };
  if (!selection.brief.length && !selection.glossary_casing.length && !selection.save_ground_truth) {
    learnStatus.textContent = "Pick at least one item, or the quality-reference box.";
    return;
  }
  learnApply.disabled = true;
  learnStatus.textContent = "Applying…";
  try {
    const r = await api.learnEditsCommit(novelId, currentCh, selection);
    const bits = [];
    if (r.applied_glossary) bits.push(`${r.applied_glossary} glossary fix(es)`);
    if (r.applied_brief) bits.push(`${r.applied_brief} brief note(s)`);
    if (r.ground_truth_saved) bits.push("saved quality reference");
    learnStatus.textContent = bits.length ? `Done: ${bits.join(", ")}.` : "Nothing applied.";
    if (r.applied_glossary) {
      await refreshGlossaryCache();
      refreshGlossarySurfaces();
    }
    setTimeout(() => { if (learnDialog.open) learnDialog.close(); }, 900);
  } catch (e) {
    learnStatus.textContent = `Failed: ${e.message}`;
  } finally {
    learnApply.disabled = false;
  }
});

/* ---- Glossary term form (add / revise) ---- */

const termFormDialog = document.getElementById("term-form-dialog");
const tfTitle = document.getElementById("term-form-title");
const tfZh = document.getElementById("tf-zh");
const tfEn = document.getElementById("tf-en");
const tfCat = document.getElementById("tf-cat");
const tfNotes = document.getElementById("tf-notes");
const tfLocked = document.getElementById("tf-locked");
const tfErr = document.getElementById("tf-err");
const tfDelete = document.getElementById("tf-delete");
const tfSave = document.getElementById("tf-save");
const tfCancel = document.getElementById("tf-cancel");
// null => add mode; a glossary entry => revise mode.
let termFormEntry = null;

function openTermForm(entry, prefill) {
  if (!termFormDialog) return;
  termFormEntry = entry || null;
  tfErr.textContent = "";
  if (entry) {
    tfTitle.textContent = "Revise glossary term";
    tfZh.value = entry.term_zh || "";
    tfZh.readOnly = true;
    tfZh.title = "term_zh is the lookup key. Delete and re-add to change it";
    tfEn.value = entry.term_en || "";
    tfCat.value = entry.category || "other";
    tfNotes.value = entry.notes || "";
    // Checked by default: a revision endorses the rendering (the server's
    // update_entry would implicitly lock on edit anyway); unchecking is the
    // explicit opt-out (revise-form lock-clobber fix, 2026-06-11).
    tfLocked.checked = true;
    tfDelete.hidden = false;
  } else {
    tfTitle.textContent = "Add to glossary";
    tfZh.value = (prefill && prefill.zh) || "";
    tfZh.readOnly = false;
    tfZh.removeAttribute("title");
    tfEn.value = (prefill && prefill.en) || "";
    tfCat.value = "character";
    tfNotes.value = "";
    tfLocked.checked = true;
    tfDelete.hidden = true;
  }
  if (!termFormDialog.open) termFormDialog.showModal();
  setTimeout(() => {
    const el = tfZh.value ? tfEn : tfZh;
    el.focus();
  }, 0);
}

tfCancel?.addEventListener("click", () => termFormDialog?.close());

tfDelete?.addEventListener("click", async () => {
  const entry = termFormEntry;
  if (!entry) return;
  const ok = await confirmDialog({
    title: "Remove glossary term?",
    body: `<p>Remove <strong>${escapeHtml(entry.term_zh)} ↔ ${escapeHtml(entry.term_en)}</strong> from the glossary? Existing chapter text is unchanged.</p>`,
    okText: "Remove",
    danger: true,
  });
  if (!ok) return;
  try {
    await api.deleteGlossary(entry.id);
    termFormDialog.close();
    await refreshGlossaryCache();
    refreshGlossarySurfaces();
    showToast("Removed from glossary", "ok");
  } catch (e) {
    tfErr.textContent = `Delete failed: ${e.message}`;
  }
});

tfSave?.addEventListener("click", async () => {
  const zh = tfZh.value.trim();
  const en = tfEn.value.trim();
  const cat = tfCat.value;
  const notes = tfNotes.value.trim();
  const locked = tfLocked.checked;
  if (!zh || !en) {
    tfErr.textContent = "Chinese and English are both required.";
    return;
  }
  tfSave.disabled = true;
  tfErr.textContent = "";
  try {
    if (termFormEntry) {
      const entry = termFormEntry;
      const oldEn = (entry.term_en || "").trim();
      const patch = {};
      if (en !== oldEn) patch.term_en = en;
      if (cat !== (entry.category || "other")) patch.category = cat;
      if ((notes || null) !== (entry.notes || null)) patch.notes = notes || null;
      // Ship the lock state whenever a field changed or it differs from the
      // row: an unchecked box must reach the server as locked=false or the
      // implicit lock-on-edit overrides the opt-out.
      if (Object.keys(patch).length > 0 || locked !== !!entry.locked) {
        patch.locked = locked;
      }
      if (Object.keys(patch).length === 0) {
        termFormDialog.close();
        return;
      }
      await api.updateGlossary(entry.id, patch);
      let applied = null;
      if (patch.term_en && oldEn) {
        // In-place rewrite across this novel's chapters (the WTR-lab flow the
        // reader's term-edit popover had). The backend reprojects the segment
        // store in the same transaction; reload so the grid shows the rewrite.
        try {
          applied = await api.glossaryApplyInPlace(entry.id, oldEn, en);
        } catch (_) {
          showToast("Glossary updated, but in-place rewrite failed. Try the Glossary page.", "err");
        }
      }
      termFormDialog.close();
      await refreshGlossaryCache();
      refreshGlossarySurfaces();
      if (applied) {
        const n = applied.chapters_updated || 0;
        showToast(`Saved. Renamed in ${n} chapter${n === 1 ? "" : "s"}.`, "ok");
        await loadSegments();
      } else {
        showToast("Saved", "ok");
      }
    } else {
      await api.createGlossary(novelId, {
        term_zh: zh, term_en: en, category: cat, notes: notes || null,
      });
      termFormDialog.close();
      await refreshGlossaryCache();
      refreshGlossarySurfaces();
      showToast("Added to glossary", "ok");
    }
  } catch (e) {
    tfErr.textContent = `Save failed: ${e.message}`;
  } finally {
    tfSave.disabled = false;
  }
});

document.getElementById("assist-add-term")?.addEventListener("click", () => {
  openTermForm(null, null);
});

/* ---- Select-to-add popover on the segment grid ---- */

let selPop = null;

function clearSelPop() {
  if (selPop) { selPop.remove(); selPop = null; }
}

function gridSelection() {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
  const text = sel.toString().trim();
  if (!text || text.length > 60) return null;
  const range = sel.getRangeAt(0);
  const node = range.commonAncestorContainer;
  const el = node.nodeType === 1 ? node : node.parentElement;
  const cell = el && el.closest ? el.closest(".seg-src, .seg-tgt") : null;
  if (!cell || !grid.contains(cell)) return null;
  const rect = range.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return null;
  return { text, rect, inZh: cell.classList.contains("seg-src") };
}

async function showSelPopover() {
  clearSelPop();
  const found = gridSelection();
  if (!found) return;
  const { text, rect, inZh } = found;
  const entries = await loadGlossaryOnce();
  // Selection may have collapsed while the glossary loaded.
  if (!gridSelection()) return;
  const lc = text.toLowerCase();
  const existing = (entries || []).find(g => inZh
    ? (g.term_zh || "").trim() === text
    : (g.term_en || "").trim().toLowerCase() === lc);

  selPop = document.createElement("div");
  selPop.className = "sel-pop";
  selPop.innerHTML = existing
    ? `<span class="sel-pop-hint">${escapeHtml(existing.term_zh)} ↔ ${escapeHtml(existing.term_en)}</span>
       <span class="sep"></span>
       <button data-act="revise">✎ Revise</button>
       <span class="sep"></span>
       <button data-act="concordance">尋 Concordance</button>`
    : `<button data-act="add">＋ Add to glossary</button>
       <span class="sep"></span>
       <button data-act="concordance">尋 Concordance</button>`;
  document.body.appendChild(selPop);
  const desiredTop = window.scrollY + rect.top - selPop.offsetHeight - 10;
  const desiredLeft = window.scrollX + rect.left + (rect.width / 2) - (selPop.offsetWidth / 2);
  const maxLeft = window.scrollX + window.innerWidth - selPop.offsetWidth - 8;
  const maxTop = window.scrollY + window.innerHeight - selPop.offsetHeight - 8;
  selPop.style.top = `${Math.min(Math.max(window.scrollY + 8, desiredTop), maxTop)}px`;
  selPop.style.left = `${Math.min(Math.max(8, desiredLeft), maxLeft)}px`;

  selPop.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const act = btn.dataset.act;
    clearSelPop();
    window.getSelection()?.removeAllRanges();
    if (act === "add") {
      openTermForm(null, inZh ? { zh: text } : { en: text });
    } else if (act === "revise" && existing) {
      openTermForm(existing, null);
    } else if (act === "concordance") {
      openConcordance(text);
    }
  });
}

document.addEventListener("mouseup", (e) => {
  if (selPop && selPop.contains(e.target)) return;
  // Never while an in-cell edit is active: selection inside a contenteditable
  // target is normal typing behavior, not a glossary gesture.
  if (editing) return;
  setTimeout(showSelPopover, 1);
});
document.addEventListener("mousedown", (e) => {
  if (selPop && !selPop.contains(e.target)) clearSelPop();
});

/* ---- Chapter-level glossary tiers (assist rail) ---- */

const missingLockedEl = document.getElementById("assist-missing-locked");
const chapterTermsEl = document.getElementById("assist-chapter-terms");
const chapterTermsCount = document.getElementById("assist-terms-count");

function zhAliases(termZh) {
  return String(termZh || "").split("/").map(s => s.trim()).filter(Boolean);
}

/* Missing-locked tier: SERVER-computed. GET .../consistency's glossary tier
 * is glossary.missing_translator_terms(atomic_only=True): slash-split
 * alternatives, parenthetical stripping, idiom inflections, 1-char-zh guard,
 * and the 2026-06-23 atomic-only narrowing (an all-locked naive client
 * matcher was measured ~96% false-fire; don't reimplement it here). The
 * route's fuzzy `matches` tier is deliberately IGNORED: the assist rail's
 * per-segment TM exact/fuzzy tiers already cover reuse over the segment
 * store. Refetched (debounced) on chapter load and after segment edits. */
let missingLockedSeq = 0;
let missingLockedDebounce = null;

function scheduleMissingLockedTier() {
  if (missingLockedDebounce) clearTimeout(missingLockedDebounce);
  missingLockedDebounce = setTimeout(refreshMissingLockedTier, 600);
}

async function refreshMissingLockedTier() {
  if (!missingLockedEl) return;
  const seq = ++missingLockedSeq;
  const forCh = currentCh;
  if (!currentData || !(currentData.segments || []).length) {
    missingLockedEl.innerHTML =
      `<p class="assist-empty">Loads with the segment grid.</p>`;
    return;
  }
  let res;
  try {
    res = await api.getChapterConsistency(novelId, forCh);
  } catch (_) {
    if (seq !== missingLockedSeq || forCh !== currentCh) return;
    missingLockedEl.innerHTML =
      `<p class="assist-empty">Could not load the locked-term check.</p>`;
    return;
  }
  if (seq !== missingLockedSeq || forCh !== currentCh) return;
  const flags = (res && res.glossary_flags) || [];
  missingLockedEl.innerHTML = flags.length
    ? flags.map(f => `
        <button type="button" class="missing-locked-row"
                data-zh="${escapeHtml(f.term_zh)}" data-para="${f.paragraph_index ?? ""}"
                title="Locked term expected but not found in this chapter's translation. Click to jump to its source.">
          <span class="missing-locked-badge">term</span>
          <span lang="zh">${escapeHtml(f.term_zh)}</span>
          <span class="missing-locked-arrow">→</span>
          <span>${escapeHtml(f.expected_en)}</span>
        </button>`).join("")
    : `<p class="assist-empty">No locked-term misses in this chapter.</p>`;
}

// The terms tier stays client-computed: it is a neutral "which terms fire
// here" listing (click to revise), not a warning, so naive matching is fine.
function renderChapterTermTiers() {
  if (!chapterTermsEl) return;
  scheduleMissingLockedTier();
  const segs = currentData && currentData.segments ? currentData.segments : [];
  if (!segs.length) {
    chapterTermsEl.innerHTML =
      `<p class="assist-empty">Loads with the segment grid.</p>`;
    if (chapterTermsCount) chapterTermsCount.textContent = "";
    return;
  }
  loadGlossaryOnce().then(entries => {
    if (!currentData || currentData.segments !== segs) return; // stale
    const present = []; // {entry, count}
    for (const g of entries || []) {
      const aliases = zhAliases(g.term_zh);
      if (!aliases.length) continue;
      let count = 0;
      for (const s of segs) {
        if (aliases.some(z => (s.source_text || "").includes(z))) count += 1;
      }
      if (count > 0) present.push({ entry: g, count });
    }
    if (chapterTermsCount) {
      chapterTermsCount.textContent = present.length ? `· ${present.length}` : "";
    }
    chapterTermsEl.innerHTML = present.length
      ? present.map(({ entry, count }) => `
          <button type="button" class="chapter-term-card" data-entry="${entry.id}" title="Click to revise this term">
            <span class="ct-zh" lang="zh">${escapeHtml(entry.term_zh)}</span>
            <span class="ct-en">${escapeHtml(entry.term_en)}</span>
            <span class="ct-meta">${escapeHtml(entry.category || "other")}${count > 1 ? ` · ×${count}` : ""}${entry.locked ? " · 🔒" : ""}</span>
          </button>`).join("")
      : `<p class="assist-empty">No glossary terms fire in this chapter. Select any phrase in the grid to add one.</p>`;
  });
}

missingLockedEl?.addEventListener("click", (e) => {
  const row = e.target.closest(".missing-locked-row");
  if (!row || !currentData) return;
  // Locate the offending segment client-side by term_zh (slash aliases);
  // prefer the server's paragraph_index when that row's source matches.
  const aliases = zhAliases(row.dataset.zh);
  const segs = currentData.segments || [];
  const hasAlias = (s) => s && aliases.some(z => (s.source_text || "").includes(z));
  const hinted = Number.parseInt(row.dataset.para, 10);
  let target = Number.isFinite(hinted) ? segs.find(s => s.index === hinted) : null;
  if (!hasAlias(target)) target = segs.find(hasAlias) || null;
  if (target) jumpToSegment(currentCh, target.index);
});

chapterTermsEl?.addEventListener("click", async (e) => {
  const card = e.target.closest(".chapter-term-card");
  if (!card) return;
  const id = Number.parseInt(card.dataset.entry, 10);
  const entries = await loadGlossaryOnce();
  const entry = (entries || []).find(g => g.id === id);
  if (entry) openTermForm(entry, null);
});

document.addEventListener("editor:segments-loaded", renderChapterTermTiers);
document.addEventListener("editor:segment-updated", renderChapterTermTiers);

// Boot paint (segments usually land later and repaint via the event).
renderChapterTermTiers();
