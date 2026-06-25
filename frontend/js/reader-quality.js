/* reader-quality.js - per-chapter quality badge (cockpit, edit-mode only).
 *
 * The in-reader half of "see quality": a compact violation-count chip in the
 * chapter bar that, on click, reveals this chapter's worst rule category,
 * observer hits and fixup churn, with jumps into the existing QA panel and
 * consistency rail. Data: GET /api/novels/{id}/chapters/{n}/quality
 * (services/quality_dashboard). Loaded last so it can call into the other
 * reader modules (_openObservationsDialog, setConsistencyRailOpen).
 *
 * The learn-from-edits panel (Phase 3) also lands in this file.
 */

const _qualityBadge = document.getElementById("quality-badge");
const _qualityBadgeScore = document.getElementById("quality-badge-score");
let _qualityPop = null;
let _qualityData = null;

function _qualityBand(rate) {
  if (rate <= 0.05) return "good";
  if (rate <= 0.15) return "warn";
  return "bad";
}

function _ensureQualityPop() {
  if (_qualityPop) return _qualityPop;
  _qualityPop = document.createElement("div");
  _qualityPop.className = "quality-pop";
  _qualityPop.hidden = true;
  // Delegated actions: jump into the QA panel / consistency rail.
  _qualityPop.addEventListener("click", (ev) => {
    const act = ev.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    if (act === "obs" && typeof _openObservationsDialog === "function") _openObservationsDialog();
    if (act === "cons" && typeof setConsistencyRailOpen === "function") setConsistencyRailOpen(true);
    _closeQualityPop();
  });
  document.body.appendChild(_qualityPop);
  return _qualityPop;
}

function _paintQualityPop() {
  const d = _qualityData;
  if (!d) return;
  const pop = _ensureQualityPop();
  const rules = Object.entries(d.fixup_rules || {})
    .map(([n, c]) => `<div class="qp-row"><span class="qp-k">${escapeHtml(n)}</span><span class="qp-v">${c}</span></div>`)
    .join("");
  pop.innerHTML = `
    <div class="qp-head">Chapter quality</div>
    <div class="qp-big qp-${_qualityBand(d.rate)}">${d.violations}
      <span class="qp-big-sub">violations / ${d.opportunities} opportunities</span></div>
    ${d.worst_category
      ? `<div class="qp-row"><span class="qp-k">Worst category</span><span class="qp-v">${escapeHtml(d.worst_category)}</span></div>`
      : ""}
    <div class="qp-row"><span class="qp-k">Observer hits</span>
      <span class="qp-v"><button type="button" class="qp-link" data-act="obs">${d.observer_hits} →</button></span></div>
    <div class="qp-row"><span class="qp-k">Fixup churn</span><span class="qp-v">${d.fixup_total}</span></div>
    ${rules}
    <div class="qp-actions"><button type="button" class="qp-link" data-act="cons">Open consistency rail →</button></div>`;
  // Anchor under the badge, kept on-screen.
  const r = _qualityBadge.getBoundingClientRect();
  pop.style.top = `${r.bottom + 6}px`;
  pop.style.left = `${Math.max(8, Math.min(r.right - 240, window.innerWidth - 256))}px`;
}

function _openQualityPop() {
  _paintQualityPop();
  if (_qualityPop) _qualityPop.hidden = false;
  _qualityBadge?.setAttribute("aria-expanded", "true");
}
function _closeQualityPop() {
  if (_qualityPop) _qualityPop.hidden = true;
  _qualityBadge?.setAttribute("aria-expanded", "false");
}

_qualityBadge?.addEventListener("click", (ev) => {
  ev.stopPropagation();
  if (_qualityPop && !_qualityPop.hidden) { _closeQualityPop(); return; }
  _openQualityPop();
});
document.addEventListener("click", (ev) => {
  if (_qualityPop && !_qualityPop.hidden
      && !_qualityPop.contains(ev.target) && ev.target !== _qualityBadge) {
    _closeQualityPop();
  }
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && _qualityPop && !_qualityPop.hidden) _closeQualityPop();
});

// Fetch + paint the badge for a chapter. Edit-mode-only and done-only; a slow
// response from a previous chapter never paints over the current one.
async function renderQualityBadge(ch) {
  if (!_qualityBadge) return;
  if (!ch || ch.status !== "done") {
    _qualityBadge.hidden = true;
    _closeQualityPop();
    return;
  }
  try {
    const d = await api.chapterQuality(novelId, ch.chapter_num);
    if (currentCh !== ch.chapter_num) return;  // stale response guard
    if (!d || !d.scored) { _qualityBadge.hidden = true; return; }
    _qualityData = d;
    _qualityBadgeScore.textContent = d.violations;
    _qualityBadge.dataset.band = _qualityBand(d.rate);
    _qualityBadge.title =
      `Quality: ${d.violations} rule violations`
      + (d.worst_category ? ` (worst: ${d.worst_category})` : "")
      + ` · ${d.observer_hits} observer hits · ${d.fixup_total} fixups`;
    _qualityBadge.hidden = false;
    if (_qualityPop && !_qualityPop.hidden) _paintQualityPop();
  } catch (_) {
    _qualityBadge.hidden = true;
  }
}

// Boot fallback: if a chapter was already rendered before this module executed
// (rare, since the boot render follows awaited network loads), paint its badge.
if (typeof lastChapter !== "undefined" && lastChapter) renderQualityBadge(lastChapter);


/* ---- Learn-from-edits panel ---------------------------------------------
 * Turns this chapter's captured paragraph edits into confirm-per-row glossary
 * casing fixes + per-novel brief notes (and an optional quality reference).
 * Stage then commit: nothing is written until the user picks Apply. */
const _learnBtn = document.getElementById("learn-edits-btn");
const _learnDialog = document.getElementById("learn-edits-dialog");
const _learnBody = document.getElementById("learn-edits-body");
const _learnGt = document.getElementById("learn-edits-gt");
const _learnStatus = document.getElementById("learn-edits-status");
const _learnApply = document.getElementById("learn-edits-apply");
let _learnProposal = null;

function _renderLearnProposal(p) {
  if (!p || p.captured_edits === 0) {
    return `<p class="muted">No captured edits for this chapter yet. Switch on
      Edit paragraphs, rewrite a few, then come back.</p>`;
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
  const parts = [`<p class="muted" style="font-size:12px;">From ${p.captured_edits} captured edit(s).</p>`];
  if (glossRows) parts.push(`<div class="learn-sec">Terminology → glossary</div>${glossRows}`);
  if (briefRows) parts.push(`<div class="learn-sec">Voice → brief note</div>${briefRows}`);
  if (!glossRows && !briefRows) {
    parts.push(`<p class="muted">No new glossary or voice patterns found. You can still
      save this chapter as a quality reference below.</p>`);
  }
  return parts.join("");
}

async function _openLearnEdits() {
  if (!_learnDialog) return;
  _learnStatus.textContent = "";
  if (_learnGt) _learnGt.checked = false;
  _learnBody.innerHTML = `<p class="muted">Analyzing your edits…</p>`;
  if (!_learnDialog.open) _learnDialog.showModal();
  try {
    _learnProposal = await api.learnEditsStage(novelId, currentCh);
    _learnBody.innerHTML = _renderLearnProposal(_learnProposal);
  } catch (e) {
    _learnBody.innerHTML = `<p class="status err">${escapeHtml(e.message)}</p>`;
  }
}

_learnBtn?.addEventListener("click", _openLearnEdits);
_learnDialog?.querySelector("[data-act='cancel']")
  ?.addEventListener("click", () => _learnDialog.close());

_learnApply?.addEventListener("click", async () => {
  if (!_learnProposal) { _learnDialog.close(); return; }
  const checked = (kind) => Array.from(
    _learnBody.querySelectorAll(`input[data-kind="${kind}"]:checked`)
  ).map(el => el.value);
  const selection = {
    brief: checked("brief"),
    glossary_casing: checked("gloss"),
    save_ground_truth: !!(_learnGt && _learnGt.checked),
  };
  if (!selection.brief.length && !selection.glossary_casing.length && !selection.save_ground_truth) {
    _learnStatus.textContent = "Pick at least one item, or the quality-reference box.";
    return;
  }
  _learnApply.disabled = true;
  _learnStatus.textContent = "Applying…";
  try {
    const r = await api.learnEditsCommit(novelId, currentCh, selection);
    const bits = [];
    if (r.applied_glossary) bits.push(`${r.applied_glossary} glossary fix(es)`);
    if (r.applied_brief) bits.push(`${r.applied_brief} brief note(s)`);
    if (r.ground_truth_saved) bits.push("saved quality reference");
    _learnStatus.textContent = bits.length ? `Done: ${bits.join(", ")}.` : "Nothing applied.";
    if (r.applied_glossary && typeof loadGlossary === "function") {
      await loadGlossary();
      if (typeof invalidateTermPattern === "function") invalidateTermPattern();
    }
    setTimeout(() => { if (_learnDialog.open) _learnDialog.close(); }, 900);
  } catch (e) {
    _learnStatus.textContent = `Failed: ${e.message}`;
  } finally {
    _learnApply.disabled = false;
  }
});
