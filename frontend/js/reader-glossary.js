/* ---- Glossary highlighting ---- */
// R8: glossary highlight regex was rebuilt on every renderChapterBody —
// each chapter nav, each toggle, each search. On a 2000-term glossary
// that's two ~2000-alternative unions compiled per render. We key the
// cache on the glossaryCache reference identity; loadGlossary / create /
// update all replace the array (or invalidate explicitly) so a stale
// cache can't outlive the data it was built from.
let _termPatternCache = null;
let _termPatternCacheKey = null;
function invalidateTermPattern() {
  _termPatternCache = null;
  _termPatternCacheKey = null;
}
function buildTermPattern() {
  if (!glossaryCache.length) return null;
  if (_termPatternCacheKey === glossaryCache) return _termPatternCache;
  // Sort longest first so "Master Yan" beats "Yan".
  const escapeRe = s => s.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&");

  const enTerms = glossaryCache
    .filter(g => g.term_en && g.term_en.trim().length >= 2)
    .map(g => g.term_en.trim())
    .sort((a, b) => b.length - a.length);
  // Case-insensitive: a glossary term may be stored lowercase but appear
  // title-cased at a sentence start (and vice versa). The displayed text still
  // uses the prose's own casing — only matching is case-folded.
  const enRe = enTerms.length
    ? new RegExp("\\b(" + enTerms.map(escapeRe).join("|") + ")\\b", "gi")
    : null;

  const zhTerms = glossaryCache
    .filter(g => g.term_zh && g.term_zh.trim())
    .map(g => g.term_zh.trim())
    .sort((a, b) => b.length - a.length);
  const zhRe = zhTerms.length
    ? new RegExp("(" + zhTerms.map(escapeRe).join("|") + ")", "g")
    : null;

  const result = (!enRe && !zhRe) ? null : { en: enRe, zh: zhRe };
  _termPatternCache = result;
  _termPatternCacheKey = glossaryCache;
  return result;
}

function termInfo(text, side /* "en" | "zh" */) {
  const k = side === "en" ? "term_en" : "term_zh";
  const needle = text.trim();
  // EN matching is case-insensitive (mirrors the case-insensitive term regex);
  // ZH stays exact since Chinese has no case.
  if (side === "en") {
    const lc = needle.toLowerCase();
    return glossaryCache.find(g => (g[k] || "").trim().toLowerCase() === lc);
  }
  return glossaryCache.find(g => (g[k] || "").trim() === needle);
}
function termClass(g) {
  if (!g) return "term";
  if (g.category === "technique" || g.category === "item") return "term alt";
  return "term";
}

// Shared paragraph splitter for both reading panes and the edit-mode aligned
// grid. Blank lines delimit paragraphs. When a raw has internal newlines but no
// blank-line separator (some scraped CN raws), each non-empty line becomes its
// own paragraph, so the source pane does not collapse into one giant <p> while
// the EN side (marked with breaks:true) shows paragraph-level breaks. Returns
// trimmed, non-empty paragraph strings.
function _splitParas(text) {
  const raw = String(text || "");
  if (raw.includes("\n") && !/\n\s*\n/.test(raw)) {
    return raw.split(/\n+/).map(p => p.trim()).filter(Boolean);
  }
  return raw.split(/\n\s*\n/).map(p => p.trim()).filter(Boolean);
}

// Render ONE already-split paragraph to a single <p>, overlaying glossary-term
// highlight spans. `re` is the precompiled term regex for this side (or null).
function _renderOneParaWithTerms(para, side, re) {
  if (!re) return `<p>${escapeHtml(para).replace(/\n/g, "<br>")}</p>`;
  // Run regex over the raw (unescaped) paragraph so word boundaries work,
  // but render each piece via escapeHtml afterwards.
  const out = [];
  let lastIdx = 0;
  let m;
  re.lastIndex = 0;
  while ((m = re.exec(para))) {
    if (m.index > lastIdx) out.push(escapeHtml(para.slice(lastIdx, m.index)));
    const matched = m[0];
    const g = termInfo(matched, side);
    out.push(`<span class="${termClass(g)}" data-term="${escapeHtml(g ? (side === "en" ? g.term_zh : g.term_en) : matched)}" title="${escapeHtml(g ? `${g.term_zh} ↔ ${g.term_en}${g.category ? " · " + g.category : ""}` : matched)}">${escapeHtml(matched)}</span>`);
    lastIdx = m.index + matched.length;
  }
  if (lastIdx < para.length) out.push(escapeHtml(para.slice(lastIdx)));
  return `<p>${out.join("").replace(/\n/g, "<br>")}</p>`;
}

function renderParagraphsWithTerms(text, side, pattern) {
  const re = pattern && pattern[side];
  return _splitParas(text).map(para => _renderOneParaWithTerms(para, side, re)).join("");
}

function renderEnglishMarkdownWithTerms(text, pattern) {
  // The translator emits Markdown: **bold** around 【】 system
  // blocks, *italics* for first-person present-tense thought / recited text /
  // titles of works, ALL-CAPS for sound effects. Parse with marked, then
  // overlay glossary-term highlighting on the resulting text segments while
  // leaving tag content alone so we don't corrupt <strong>/<em>/<br> markup.
  const re = pattern && pattern.en;
  const src = String(text || "");
  // marked@12 does NOT sanitize raw HTML in its input; the LLM output is
  // untrusted (prompt-injection / passthrough from source can yield <script>),
  // so DOMPurify is mandatory. If either lib failed to load, degrade to the
  // plain escape-and-render path rather than risk XSS.
  if (!window.marked || !window.DOMPurify || !src) {
    return renderParagraphsWithTerms(src, "en", pattern);
  }
  const rawHtml = window.DOMPurify.sanitize(
    window.marked.parse(src, { breaks: true, gfm: true })
  );
  if (!re) return rawHtml;
  return rawHtml.split(/(<[^>]+>)/).map(seg => {
    if (!seg) return "";
    if (seg.startsWith("<")) return seg;
    // seg is HTML-escaped text from marked + sanitized by DOMPurify. Run the
    // term regex on it and wrap matches in glossary spans. For terms
    // containing HTML-special characters this would miss; cultivation
    // glossary terms don't, so it's acceptable.
    const out = [];
    let lastIdx = 0;
    let m;
    re.lastIndex = 0;
    while ((m = re.exec(seg))) {
      if (m.index > lastIdx) out.push(seg.slice(lastIdx, m.index));
      const matched = m[0];
      const g = termInfo(matched, "en");
      out.push(`<span class="${termClass(g)}" data-term="${escapeHtml(g ? g.term_zh : matched)}" title="${escapeHtml(g ? `${g.term_zh} ↔ ${g.term_en}${g.category ? " · " + g.category : ""}` : matched)}">${matched}</span>`);
      lastIdx = m.index + matched.length;
    }
    if (lastIdx < seg.length) out.push(seg.slice(lastIdx));
    return out.join("");
  }).join("");
}

/* ---- Paragraph-aligned grid (2026-06) ----
 * Pairs each source paragraph with its translation paragraph in shared rows so
 * a translator can compare and rewrite line by line. Active in edit mode and
 * in bilingual read mode (see renderChapterBody); without it the independent
 * panes drift wherever the translation merges paragraphs or drops the 本章完
 * boilerplate, and the reader sees the columns "out of order" by the tail.
 *
 * Both columns are split from the STORED text with one shared rule (_splitParas)
 * so the cell counts reflect real paragraph structure. original_text keeps its
 * leading chapter heading while the English title is stripped (it lives in the
 * masthead), so we drop that heading from the source before aligning, otherwise
 * every row shifts down by one.
 *
 * The translator may merge or split paragraphs and emits no alignment data, so
 * the counts can legitimately diverge. _alignParas runs a length-ratio
 * alignment (1:1 / 2:1 / 1:2) that keeps the columns in step past a local
 * merge/split instead of letting one mismatch cascade down the chapter. Every
 * target paragraph is stamped with its true stored-text index, so per-paragraph
 * editing stays exact even when several paragraphs share one row. When the two
 * sides are too divergent to align, the layout falls back to the independent
 * panes (where editing always works). */

// Mirror of backend tm.py::_drop_leading_heading. Conservative: only the first
// paragraph, only when it is a CN chapter-heading line.
const _ZH_HEADING_RE = /^[ \t]*第[\d零〇一二三四五六七八九十百千万两]+[ \t]*[章回节]/;
function _dropLeadingZhHeading(paras) {
  return (paras.length && _ZH_HEADING_RE.test(paras[0])) ? paras.slice(1) : paras;
}

// Render one stored English paragraph (markdown) to display HTML, stamping each
// top-level block with its true index in translated_text/refined_text so the
// edit-on-blur handler splices the right chunk regardless of row grouping.
function _renderEnChunkCell(chunk, pattern, paraIndex) {
  const tmp = document.createElement("div");
  tmp.innerHTML = renderEnglishMarkdownWithTerms(chunk, pattern);
  tmp.childNodes.forEach(node => {
    if (node.nodeType === Node.ELEMENT_NODE) node.dataset.paragraphIndex = String(paraIndex);
  });
  return tmp.innerHTML;
}

/* Length-ratio paragraph alignment (Gale-Church-lite). Returns an ordered list
 * of { src:[indices], tgt:[indices] } groups, or null when the two sides are
 * too divergent to align meaningfully (caller falls back to the plain panes).
 * Deterministic. Moves: 1:1, 2:1 (two source paras to one target), 1:2, and
 * 1:0 / 0:1 (a paragraph with no counterpart) at a high penalty so content is
 * grouped rather than dropped. Cost compares each target group's length to its
 * expected length r*sourceLength, where r is the chapter's CN to EN expansion. */
function _alignParas(srcParas, tgtParas) {
  const n = srcParas.length, m = tgtParas.length;
  if (!n || !m) return null;
  // Very divergent counts: 1:1 / 2:1 / 1:2 moves cannot span it cleanly, and the
  // plain panes read better than a forced grouping.
  if (Math.abs(n - m) / Math.max(n, m) > 0.5) return null;
  if (n * m > 4000000) return null; // perf backstop for pathological chapters
  const sLen = srcParas.map(s => s.length);
  const tLen = tgtParas.map(t => t.length);
  const sTot = sLen.reduce((a, b) => a + b, 0);
  const tTot = tLen.reduce((a, b) => a + b, 0);
  if (!sTot || !tTot) return null;
  const r = tTot / sTot;     // expected EN chars per CN char
  const avgT = tTot / m;     // one average target paragraph, the penalty unit
  // Bias hard toward 1:1. A merge or split must cut the length mismatch by more
  // than a whole paragraph to be worth it, so equal-count chapters (the common
  // case once the heading is dropped) stay 1:1 instead of reshuffling on
  // per-paragraph length noise. STEP_PEN only gates OPTIONAL deviations: when the
  // counts genuinely differ the move is forced regardless of the penalty.
  const STEP_PEN = avgT;
  const DROP_PEN = 4 * avgT; // dropping a paragraph (an empty cell) is a last resort
  const cost = (srcChars, tgtChars) => Math.abs(tgtChars - r * srcChars);
  const dp = Array.from({ length: n + 1 }, () => new Float64Array(m + 1).fill(Infinity));
  const back = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(null));
  dp[0][0] = 0;
  for (let i = 0; i <= n; i++) {
    for (let j = 0; j <= m; j++) {
      const base = dp[i][j];
      if (base === Infinity) continue;
      const relax = (di, dj, add) => {
        const ni = i + di, nj = j + dj;
        const c = base + add;
        if (c < dp[ni][nj]) { dp[ni][nj] = c; back[ni][nj] = [di, dj]; }
      };
      if (i < n && j < m) relax(1, 1, cost(sLen[i], tLen[j]));
      if (i + 1 < n && j < m) relax(2, 1, cost(sLen[i] + sLen[i + 1], tLen[j]) + STEP_PEN);
      if (i < n && j + 1 < m) relax(1, 2, cost(sLen[i], tLen[j] + tLen[j + 1]) + STEP_PEN);
      if (i < n) relax(1, 0, DROP_PEN + r * sLen[i]); // source para, no target
      if (j < m) relax(0, 1, DROP_PEN + tLen[j]);     // target para, no source
    }
  }
  if (dp[n][m] === Infinity) return null;
  const groups = [];
  let i = n, j = m;
  while (i > 0 || j > 0) {
    const mv = back[i][j];
    if (!mv) return null; // unreachable; bail to the fallback
    const [di, dj] = mv;
    const src = [];
    const tgt = [];
    for (let k = i - di; k < i; k++) src.push(k);
    for (let k = j - dj; k < j; k++) tgt.push(k);
    groups.push({ src, tgt });
    i -= di; j -= dj;
  }
  groups.reverse();
  return groups;
}

function _buildAlignedRows(zhText, enMarkdown, pattern) {
  const srcParas = _dropLeadingZhHeading(_splitParas(zhText));
  const tgtParas = _splitParas(enMarkdown);
  if (!srcParas.length || !tgtParas.length) return null;
  const groups = _alignParas(srcParas, tgtParas);
  if (!groups) return null;
  const zhRe = pattern && pattern.zh;
  let out = "";
  for (const grp of groups) {
    const srcHtml = grp.src.map(idx => _renderOneParaWithTerms(srcParas[idx], "zh", zhRe)).join("");
    const tgtHtml = grp.tgt.map(idx => _renderEnChunkCell(tgtParas[idx], pattern, idx)).join("");
    out += `<div class="prow"><div class="src" lang="zh">${srcHtml}</div><div class="tgt">${tgtHtml}</div></div>`;
  }
  return out;
}

/* ---- Selection popover ---- */
const glossaryMiniDialog = document.getElementById("glossary-mini-dialog");
// Clean up the form reference when the dialog is closed by any means
// (Esc, programmatic close, button) so the next selection doesn't re-attach
// stale state.
glossaryMiniDialog?.addEventListener("close", () => {
  if (formEl) { formEl.remove(); formEl = null; }
  if (popoverEl) { popoverEl.remove(); popoverEl = null; }
  clearSelectedTerms();
});
let popoverEl = null;
let formEl = null;
// Glossary terms in the body are quiet by default (dotted underline only);
// the highlight pill is shown via the .selected class while the term is the
// subject of the selection popover, then cleared when the popover closes.
let selectedTerms = [];
function clearSelectedTerms() {
  for (const el of selectedTerms) el.classList.remove("selected");
  selectedTerms = [];
}
function markSelectedTerms(range) {
  clearSelectedTerms();
  const node = range.commonAncestorContainer;
  const scope = node.nodeType === 1 ? node : node.parentElement;
  if (!scope) return;
  const own = scope.closest ? scope.closest(".term") : null;
  if (own) { own.classList.add("selected"); selectedTerms.push(own); }
  scope.querySelectorAll?.(".term").forEach((el) => {
    if (range.intersectsNode(el) && !el.classList.contains("selected")) {
      el.classList.add("selected");
      selectedTerms.push(el);
    }
  });
}
function clearPopover() {
  if (popoverEl) { popoverEl.remove(); popoverEl = null; }
  if (formEl) { formEl.remove(); formEl = null; }
  clearSelectedTerms();
  if (glossaryMiniDialog && glossaryMiniDialog.open) glossaryMiniDialog.close();
}
function showPopoverForSelection() {
  clearPopover();
  // Read mode = clean reading view: native selection only, no popover.
  // The selection toolbar (glossary / bookmark / concordance / copy) is a
  // translator's-workbench affordance and stays edit-mode only. In read mode
  // text behaves like any other app: drag-select, then Ctrl+C or right-click
  // → Copy. clearPopover() above still runs so flipping edit → read tears
  // down any open popover.
  if (readerMode !== "edit") return;
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) return;
  const text = sel.toString().trim();
  if (!text || text.length > 60) return;
  const range = sel.getRangeAt(0);
  // Only react if the selection is inside the reader pane. In the edit-mode
  // aligned grid the panes are #aligned-body's .src (Chinese) / .tgt (English)
  // cells; in the legacy layout they are #body-zh / #body-en.
  const node = range.commonAncestorContainer;
  const el = node.nodeType === 1 ? node : node.parentElement;
  const alignedEl = document.getElementById("aligned-body");
  const inAligned = !!(alignedEl && alignedEl.contains(node));
  if (!bodyEn.contains(node) && !bodyZh.contains(node) && !inAligned) return;
  const inZh = inAligned ? !!(el && el.closest(".src")) : bodyZh.contains(node);
  const rect = range.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return;

  // Pin-and-revise: an exact match on the appropriate side flips the primary
  // action from "add new" to "revise" the existing entry. termInfo already
  // does the case-sensitive trim-equality lookup against glossaryCache.
  const existing = termInfo(text, inZh ? "zh" : "en");

  // Pill any glossary term spans the selection touches while the popover is up.
  markSelectedTerms(range);

  popoverEl = document.createElement("div");
  popoverEl.className = "sel-pop";
  // Wireframes redesign: the .sel-pop is now the unified contextual
  // toolbar — adds 記 bookmark + 尋 concordance alongside the existing
  // glossary + copy actions, so the standalone #concordance-trigger
  // can retire. Each action wires straight into an existing handler:
  //   add → showAddForm           (the same flow the glossary page uses)
  //   revise → showReviseForm
  //   copy → navigator.clipboard
  //   bookmark → opens bookmark-add-dialog with the selection's
  //              paragraph captured (instead of the scroll-top fallback)
  //   concordance → _openConcordanceDialog (TM search)
  // retranslate-paragraph is intentionally NOT here: the backend's
  // retranslate endpoint operates on a chapter, not a paragraph. The
  // chapter-wide ⟲ in the ⋯ menu covers the use case; per-paragraph
  // retranslation can land when there's an endpoint to call.
  const extraActs = `
      <span class="sep"></span>
      <button data-act="bookmark" title="Add a bookmark on the paragraph containing this selection">記 Bookmark</button>
      <span class="sep"></span>
      <button data-act="concordance" title="Search the translation memory for this phrase">尋 Concordance</button>
  `;
  if (existing) {
    popoverEl.innerHTML = `
      <span class="sel-pop-hint">${escapeHtml(existing.term_zh)} ↔ ${escapeHtml(existing.term_en)}</span>
      <span class="sep"></span>
      <button data-act="revise">✎ Revise</button>
      <span class="sep"></span>
      <button data-act="copy">＂ Copy</button>
      ${extraActs}
    `;
  } else {
    popoverEl.innerHTML = `
      <button data-act="add">＋ Add to glossary</button>
      <span class="sep"></span>
      <button data-act="copy">＂ Copy</button>
      ${extraActs}
    `;
  }
  document.body.appendChild(popoverEl);
  // Clamp to viewport on all four sides so selecting near an edge doesn't
  // render the popover off-screen.
  const desiredTop = window.scrollY + rect.top - popoverEl.offsetHeight - 10;
  const desiredLeft = window.scrollX + rect.left + (rect.width / 2) - (popoverEl.offsetWidth / 2);
  const maxLeft = window.scrollX + window.innerWidth - popoverEl.offsetWidth - 8;
  const maxTop  = window.scrollY + window.innerHeight - popoverEl.offsetHeight - 8;
  popoverEl.style.top = `${Math.min(Math.max(window.scrollY + 8, desiredTop), maxTop)}px`;
  popoverEl.style.left = `${Math.min(Math.max(8, desiredLeft), maxLeft)}px`;

  popoverEl.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    if (btn.dataset.act === "copy") {
      const ok = await copyText(text);
      showFloatToast(ok ? "Copied" : "Copy failed", rect);
      clearPopover();
      return;
    }
    if (btn.dataset.act === "add") {
      // Capture which paragraph the selection sits in so the Add form can
      // render the partner-side paragraph (EN→ZH or ZH→EN) as a click-to-
      // fill reference for non-native users. Falls back to null when the
      // selection isn't cleanly inside a single <p>.
      const paraIdx = _paragraphIndexFromRange(range, inZh ? bodyZh : bodyEn);
      showAddForm(text, inZh, rect, paraIdx);
    } else if (btn.dataset.act === "revise") {
      showReviseForm(existing, rect);
    } else if (btn.dataset.act === "bookmark") {
      // Capture the paragraph that the selection sits in (more accurate
      // than the scroll-top fallback the ☆ button uses). Walk the range's
      // common ancestor up to the nearest <p> inside body-en/zh, then
      // index it.
      const para = _paragraphIndexFromRange(range, inZh ? bodyZh : bodyEn);
      _openBookmarkAddDialogFromSelection(para, text);
      clearPopover();
      window.getSelection()?.removeAllRanges();
    } else if (btn.dataset.act === "concordance") {
      _openConcordanceDialog(text);
      clearPopover();
      window.getSelection()?.removeAllRanges();
    }
  });
}

/* ---- Inline term-edit popover (WTR-lab style click-to-edit) ----
 *
 * Edit mode only. Click a highlighted .term span, edit its English
 * rendering in a small floating form, save: PATCH the glossary entry
 * (implicitly locks it) + apply-in-place across all chapters of this
 * novel (word-boundary, case-sensitive — see
 * backend/services/find_replace.py::apply_in_place_for_glossary_term).
 * No three-way choice dialog — the WTR-lab feel is instant rewrite.
 */
let termEditPop = null;
function clearTermEditPop() {
  if (termEditPop) { termEditPop.remove(); termEditPop = null; }
}

function showTermEditPop(span, entry, side) {
  clearTermEditPop();
  // The selection popover and the term-edit popover are mutually
  // exclusive — close the other side so they don't overlap.
  clearPopover();

  const rect = span.getBoundingClientRect();
  const pop = document.createElement("div");
  pop.className = "sel-form term-edit-pop";
  // Pin the locked checkbox to checked by default: the user is editing,
  // which already implicitly locks the entry server-side (see
  // backend/services/glossary.py::update_entry). Surfacing it as checked
  // matches the truth so users aren't surprised.
  const categories = ["character", "place", "technique", "item", "other", "idiom"];
  const cat = entry.category || "other";
  pop.innerHTML = `
    <div class="term-edit-head">
      <span class="term-edit-zh">${escapeHtml(entry.term_zh)}</span>
      <span class="term-edit-sep">↔</span>
      <span class="muted">${escapeHtml(entry.term_en)}</span>
    </div>
    <div class="row"><label style="width:64px;">English</label><input id="te-en" value="${escapeHtml(entry.term_en)}"></div>
    <div class="row"><label style="width:64px;">Category</label>
      <select id="te-cat">
        ${categories.map(c => `<option value="${c}"${c === cat ? " selected" : ""}>${c}</option>`).join("")}
      </select>
    </div>
    <div class="row"><label style="width:64px;">Locked</label>
      <input id="te-lock" type="checkbox" checked>
      <span class="muted" style="font-size:11.5px;">Uncheck to allow auto-updates.</span>
    </div>
    <div id="te-err" class="muted" style="color: var(--signal-error); min-height: 1em;"></div>
    <div class="actions">
      <button class="btn-ghost" data-act="delete" title="Remove this glossary entry">Remove</button>
      <span style="flex:1;"></span>
      <button class="btn-ghost" data-act="cancel">Cancel</button>
      <button class="btn-primary" data-act="save">Save</button>
    </div>
  `;
  document.body.appendChild(pop);

  // Position like .sel-pop: prefer above the span, fall back below if no
  // room. Viewport-clamp on all four sides so edge clicks don't render
  // off-screen.
  const popH = pop.offsetHeight;
  const popW = pop.offsetWidth;
  const above = window.scrollY + rect.top - popH - 10;
  const below = window.scrollY + rect.bottom + 10;
  const desiredTop = (rect.top > popH + 20) ? above : below;
  const desiredLeft = window.scrollX + rect.left + (rect.width / 2) - (popW / 2);
  const maxLeft = window.scrollX + window.innerWidth - popW - 8;
  const maxTop  = window.scrollY + window.innerHeight - popH - 8;
  pop.style.top  = `${Math.min(Math.max(window.scrollY + 8, desiredTop), maxTop)}px`;
  pop.style.left = `${Math.min(Math.max(8, desiredLeft), maxLeft)}px`;

  termEditPop = pop;

  const enInput = pop.querySelector("#te-en");
  const catSel  = pop.querySelector("#te-cat");
  const lockChk = pop.querySelector("#te-lock");
  const errEl   = pop.querySelector("#te-err");

  // Focus + select-all so the user can just start typing.
  setTimeout(() => { enInput.focus(); enInput.select(); }, 0);

  const close = () => clearTermEditPop();

  // Esc dismisses; Enter saves.
  pop.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { ev.preventDefault(); close(); }
    else if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      pop.querySelector("[data-act='save']").click();
    }
  });

  pop.querySelector("[data-act='cancel']").addEventListener("click", close);

  pop.querySelector("[data-act='delete']").addEventListener("click", async () => {
    if (!confirm(`Remove "${entry.term_zh} ↔ ${entry.term_en}" from the glossary? Existing chapter text is unchanged.`)) return;
    try {
      await api.deleteGlossary(entry.id);
      await loadGlossary();
      invalidateTermPattern();
      if (lastChapter) renderChapterBody(lastChapter);
      showFloatToast("Removed from glossary", rect);
      close();
    } catch (e) {
      errEl.textContent = `Delete failed: ${e.message}`;
    }
  });

  pop.querySelector("[data-act='save']").addEventListener("click", async () => {
    const newEn = enInput.value.trim();
    if (!newEn) { errEl.textContent = "English term cannot be empty."; return; }
    const newCat = catSel.value;
    const newLock = lockChk.checked;
    const oldEn = (entry.term_en || "").trim();

    // Build the PATCH body from changed fields only, comparing the lock
    // checkbox against its RENDERED default (checked) rather than the row:
    // an untouched form stays a no-op instead of a surprise lock bump.
    const patch = {};
    if (newEn !== oldEn) patch.term_en = newEn;
    if (newCat !== (entry.category || "other")) patch.category = newCat;
    if (Object.keys(patch).length === 0 && newLock) { close(); return; }
    // Ship the lock state explicitly whenever a field changed or it
    // differs from the row. An unchecked box must reach the server as
    // locked=false, or update_entry's implicit lock-on-edit would
    // override the opt-out; a checked box on an unlocked row locks it,
    // matching what the checkbox shows.
    if (Object.keys(patch).length > 0 || newLock !== !!entry.locked) {
      patch.locked = newLock;
    }
    if (Object.keys(patch).length === 0) { close(); return; }

    const saveBtn = pop.querySelector("[data-act='save']");
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    errEl.textContent = "";

    try {
      await api.updateGlossary(entry.id, patch);

      let chaptersUpdated = 0;
      let titlesUpdated = 0;
      let inPlaceFailed = false;
      if (patch.term_en && oldEn) {
        try {
          const res = await api.glossaryApplyInPlace(entry.id, oldEn, newEn);
          chaptersUpdated = res.chapters_updated || 0;
          titlesUpdated = res.rows_updated_titles || 0;
        } catch (e) {
          inPlaceFailed = true;
        }
      }

      // Refresh in-memory state so the live chapter pane reflects the
      // server-side rewrite without a manual reload.
      await loadGlossary();
      invalidateTermPattern();
      // If the title changed, the TOC entry needs updating too.
      if (titlesUpdated > 0) {
        try { await loadChapters(); } catch { /* best-effort */ }
      }
      // Refetch the current chapter so bodyEn / bodyZh pick up the
      // server-side substitution. Skip if we never had a chapter loaded
      // (defensive — shouldn't happen since the user clicked a term).
      if (lastChapter && typeof currentCh === "number") {
        try {
          const fresh = await api.chapter(novelId, currentCh);
          lastChapter = fresh;
          renderChapterBody(fresh);
        } catch {
          // Server unreachable mid-save — fall back to local re-render of
          // the stale chapter; the user will see the new highlight color
          // / term, but the body text won't reflect the rewrite until
          // they navigate. Better than a blank screen.
          renderChapterBody(lastChapter);
        }
      }

      if (inPlaceFailed) {
        showFloatToast("Glossary updated, but in-place rewrite failed. Try Glossary page.", rect);
      } else if (patch.term_en) {
        const chPart = `Renamed in ${chaptersUpdated} chapter${chaptersUpdated === 1 ? "" : "s"}`;
        const titlePart = titlesUpdated > 0 ? ` · ${titlesUpdated} title${titlesUpdated === 1 ? "" : "s"}` : "";
        showFloatToast(`${chPart}${titlePart}`, rect);
      } else {
        showFloatToast("Saved", rect);
      }
      close();
    } catch (e) {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save";
      errEl.textContent = `Save failed: ${e.message}`;
    }
  });
}

function _handleTermClick(ev, side) {
  if (readerMode !== "edit") return;
  // Defer to the selection popover when the user is making a selection.
  const sel = window.getSelection();
  if (sel && !sel.isCollapsed && sel.toString().trim()) return;
  const span = ev.target.closest(".term");
  if (!span) return;
  const dataTerm = span.getAttribute("data-term") || "";
  // The span renders the same-language text; data-term carries the
  // opposite-language partner. Look up by the side the click happened on.
  const shownText = (span.textContent || "").trim();
  const entry = termInfo(shownText, side) || (dataTerm ? termInfo(dataTerm, side === "en" ? "zh" : "en") : null);
  if (!entry) return;
  ev.preventDefault();
  ev.stopPropagation();
  showTermEditPop(span, entry, side);
}

bodyEn.addEventListener("click", (ev) => _handleTermClick(ev, "en"));
bodyZh.addEventListener("click", (ev) => _handleTermClick(ev, "zh"));
// Aligned grid (edit mode): terms live in .tgt (English) / .src (Chinese) cells.
const _alignedClickHost = document.getElementById("aligned-body");
if (_alignedClickHost) {
  _alignedClickHost.addEventListener("click", (ev) => {
    const cell = ev.target.closest(".tgt, .src");
    if (!cell) return;
    _handleTermClick(ev, cell.classList.contains("src") ? "zh" : "en");
  });
}

// Outside-click dismiss for the term-edit popover. Matches the
// mousedown handler the selection popover already uses; kept as a
// separate listener so they can't accidentally cancel each other.
document.addEventListener("mousedown", (ev) => {
  if (termEditPop && !termEditPop.contains(ev.target)) {
    // Don't dismiss when the click landed on another .term span — the
    // click handler above will rebuild the popover for the new target.
    if (!ev.target.closest(".term")) clearTermEditPop();
  }
});

/* ---- Per-chapter glossary terms rail (2026-06-04) ----
 *
 * A right-side drawer that lists, as editable cards, exactly the glossary
 * terms that appear in the chapter on screen, so terminology can be fixed
 * without hunting for the inline dotted-underline highlights in the body.
 * Edit-mode only. No backend call: the per-chapter set is derived from the
 * in-memory glossaryCache using the same matcher the body highlighter uses
 * (buildTermPattern / termInfo). Editing reuses showTermEditPop wholesale
 * (PATCH + apply-in-place + chapter refresh + toast), and because that flow
 * ends in renderChapterBody, the card list refreshes itself after a save.
 */
// applyTermsRail() now lives in reader-core.js (the boot calls it before this
// module loads). setTermsRailOpen stays here with the rest of the terms rail.
function setTermsRailOpen(open) {
  termsRailOpen = !!open;
  localStorage.setItem(TERMS_RAIL_OPEN_KEY, termsRailOpen ? "1" : "0");
  applyTermsRail();
}
termsRailToggle?.addEventListener("click", () => setTermsRailOpen(!termsRailOpen));
termsRailClose?.addEventListener("click", () => setTermsRailOpen(false));
termsBackdrop?.addEventListener("click", () => setTermsRailOpen(false));

