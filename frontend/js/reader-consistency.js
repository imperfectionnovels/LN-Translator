/* ---- Edit-mode consistency rail (on-demand) -------------------------------
 * Surfaces near-duplicate Chinese source paragraphs rendered differently
 * elsewhere in the novel (fuzzy TM) plus locked glossary terms missing from
 * this chapter. Read-only / advisory: fixes reuse the paragraph-edit and
 * glossary-editor flows already in this file. The /consistency fetch fires
 * ONLY while the rail is open, so large novels don't pay fuzzy compute on
 * every chapter open. Shares the right grid column with the terms rail; the
 * two are kept mutually exclusive. */
// applyConsistencyRail() now lives in reader-core.js (the boot calls it before
// this module loads). setConsistencyRailOpen stays here with the rail render.
function setConsistencyRailOpen(open) {
  consistencyRailOpen = !!open;
  localStorage.setItem(CONSISTENCY_RAIL_OPEN_KEY, consistencyRailOpen ? "1" : "0");
  // Mutually exclusive with the terms rail (both dock the same right column).
  if (consistencyRailOpen && typeof setTermsRailOpen === "function") setTermsRailOpen(false);
  applyConsistencyRail();
  if (consistencyRailOpen) renderConsistencyRail(consistencyChapter);
}
consistencyRailToggle?.addEventListener("click", () => setConsistencyRailOpen(!consistencyRailOpen));
consistencyRailClose?.addEventListener("click", () => setConsistencyRailOpen(false));
consistencyBackdrop?.addEventListener("click", () => setConsistencyRailOpen(false));
// Keep the terms toggle exclusive in the other direction too.
termsRailToggle?.addEventListener("click", () => { if (termsRailOpen) setConsistencyRailOpen(false); });

// Fire-and-forget fetch + paint for the open rail. Guards: edit mode, element
// present, rail open, chapter translated, plus a stale-response check so a
// late response from a prior chapter never paints into the current one.
function renderConsistencyRail(ch) {
  if (ch) consistencyChapter = ch;
  if (!consistencyList || readerMode !== "edit" || !consistencyRailOpen) return;
  const cur = consistencyChapter;
  if (!cur || !cur.translated_text) {
    if (consistencyCount) consistencyCount.textContent = "";
    consistencyList.innerHTML = `<div class="consistency-empty">Chapter not translated yet.</div>`;
    return;
  }
  const reqCh = cur.chapter_num;
  if (consistencyCount) consistencyCount.textContent = "";
  consistencyList.innerHTML = `<div class="consistency-empty">Checking…</div>`;
  api.getChapterConsistency(novelId, reqCh).then((res) => {
    // Stale-response guard: only paint if we're still on the requested chapter
    // and the rail is still open.
    if (reqCh !== currentCh || !consistencyRailOpen) return;
    paintConsistencyRail(res);
  }).catch(() => {
    if (reqCh !== currentCh) return;
    consistencyList.innerHTML = `<div class="consistency-empty">Couldn't load the consistency check.</div>`;
  });
}

function paintConsistencyRail(res) {
  if (!consistencyList) return;
  const matches = (res && res.matches) || [];
  const flags = (res && res.glossary_flags) || [];
  if (consistencyCount) {
    const total = matches.length + flags.length;
    consistencyCount.textContent = total ? `· ${total}` : "";
  }
  if (res && res.status === "not_translated") {
    consistencyList.innerHTML = `<div class="consistency-empty">Chapter not translated yet.</div>`;
    return;
  }
  let html = "";
  if (res && res.status === "tm_unavailable") {
    html += `<div class="consistency-note">Couldn't align source and translation, so near-duplicate detection is off for this chapter. Locked-term checks still run.</div>`;
  }
  if (flags.length) {
    html += `<div class="cons-section-label">Glossary terms missing here</div>`;
    html += flags.map((f) => `
      <div class="cons-flag" data-term-id="${f.term_id ?? ""}" data-para="${f.paragraph_index ?? ""}" role="button" tabindex="0" title="Open the glossary editor for this term">
        <span class="cons-badge warn">term</span>
        <span class="cons-zh" lang="zh">${escapeHtml(f.term_zh)}</span>
        <span class="cons-arrow">→</span>
        <span class="cons-expected">${escapeHtml(f.expected_en)}</span>
      </div>`).join("");
  }
  if (matches.length) {
    html += `<div class="cons-section-label">Rendered differently elsewhere</div>`;
    html += matches.map((m) => `
      <div class="cons-item">
        <div class="cons-item-head" data-para="${m.paragraph_index}" role="button" tabindex="0" title="Jump to this paragraph">
          <span class="cons-pmark">¶${m.paragraph_index + 1}</span>
          <span class="cons-src" lang="zh">${escapeHtml(m.source_text)}</span>
        </div>
        <div class="cons-here"><span class="cons-here-label">here</span>${escapeHtml(m.current_rendering)}</div>
        ${(m.others || []).map((o) => `
          <div class="cons-other" data-ch="${o.chapter_num}" role="button" tabindex="0" title="Open chapter ${o.chapter_num}">
            <span class="cons-badge ${o.exact ? "exact" : "fuzzy"}">${o.exact ? "exact" : Math.round(o.similarity * 100) + "%"}</span>
            <span class="cons-other-ch">Ch.${o.chapter_num}</span>
            <span class="cons-other-text">${escapeHtml(o.target_text)}</span>
          </div>`).join("")}
      </div>`).join("");
  }
  if (!html) html = `<div class="consistency-empty">No drift found in this chapter.</div>`;
  consistencyList.innerHTML = html;
}

if (consistencyList) {
  const _consActivate = (target) => {
    const flag = target.closest(".cons-flag");
    if (flag) {
      const para = flag.getAttribute("data-para");
      if (para !== "" && para != null) _scrollToParagraph(parseInt(para, 10));
      const id = Number(flag.getAttribute("data-term-id"));
      const entry = id ? glossaryCache.find((g) => g.id === id) : null;
      if (entry) showTermEditPop(flag, entry, "en");
      return;
    }
    const head = target.closest(".cons-item-head");
    if (head) { _scrollToParagraph(parseInt(head.getAttribute("data-para"), 10)); return; }
    const other = target.closest(".cons-other");
    if (other) {
      const ch = parseInt(other.getAttribute("data-ch"), 10);
      if (ch && ch !== currentCh) loadChapter(ch);
    }
  };
  consistencyList.addEventListener("click", (ev) => _consActivate(ev.target));
  consistencyList.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    if (!ev.target.closest(".cons-flag, .cons-item-head, .cons-other")) return;
    ev.preventDefault();
    _consActivate(ev.target);
  });
}

