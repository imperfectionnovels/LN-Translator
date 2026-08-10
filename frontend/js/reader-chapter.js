/* ---- Chapter body renderers ----
 * Relocated from the retired reader-glossary.js during the CAT pivot. Term
 * marks returned 2026-08-07: bodies render glossary-term spans again, built by
 * the shared term-marks.js so the reader and the CAT editor mark terms
 * identically. This stays a READING surface: a mark is a read-only affordance
 * (quiet dotted underline, click opens the Revise dialog owned by
 * reader-glossary.js), never contenteditable. Marks are opt-out per reader via
 * the type-settings toggle (_termMarksOn); when off, no spans are built at
 * all. glossaryCache also feeds the pre-translation cockpit's preflight pane. */

// Shared paragraph splitter for both reading panes. Blank lines delimit
// paragraphs. When a raw has internal newlines but no blank-line separator
// (some scraped CN raws), each non-empty line becomes its own paragraph so
// the source pane does not collapse into one giant <p>.
function _splitParas(text) {
  const raw = String(text || "");
  if (raw.includes("\n") && !/\n\s*\n/.test(raw)) {
    return raw.split(/\n+/).map(p => p.trim()).filter(Boolean);
  }
  return raw.split(/\n\s*\n/).map(p => p.trim()).filter(Boolean);
}

function renderParagraphs(text, side = "zh", pattern = null) {
  return _splitParas(text)
    .map(para => {
      const inner = pattern
        ? TermMarks.wrapText(para, side, pattern)
        : escapeHtml(para);
      return `<p>${inner.replace(/\n/g, "<br>")}</p>`;
    })
    .join("");
}

// Term marks are opt-out, default ON. Read at render time so a toggle flip
// takes effect on the next renderChapterBody with no reload.
function _termMarksOn() {
  return localStorage.getItem("readerTermMarks") !== "0";
}

function renderEnglishMarkdown(text, pattern = null) {
  // The translator emits Markdown: **bold** around 【】 system blocks,
  // *italics* for first-person present-tense thought / recited text / titles
  // of works. Parse with marked, sanitize with DOMPurify (LLM output is
  // untrusted; a passthrough <script> must never reach the DOM). If either
  // lib failed to load, degrade to the plain escape-and-render path.
  const src = String(text || "");
  if (!window.marked || !window.DOMPurify || !src) {
    return renderParagraphs(src, "en", pattern);
  }
  const clean = window.DOMPurify.sanitize(
    window.marked.parse(src, { breaks: true, gfm: true })
  );
  // Term marks go on strictly AFTER DOMPurify: re-sanitizing our own spans
  // would strip data-entry-id and break the click-to-revise path.
  return TermMarks.wrapSanitizedHtml(clean, pattern);
}

/* ---- Bilingual paragraph alignment (restored 2026-08-06) ----
 * The CAT pivot removed the aligned bilingual grid along with the edit-mode
 * chrome, reverting bilingual reading to two independent panes whose rows
 * drift apart at the first merge/split (and are offset by one from the top:
 * the ZH pane keeps its leading heading paragraph while the EN title lives
 * in the masthead). Row alignment is a READING feature, so it returns here,
 * minus every editing hook the old builder carried. */

// Mirror of backend segmentation.py::_drop_leading_heading. Conservative:
// only the first paragraph, only when it is a CN chapter-heading line.
const _ZH_HEADING_RE = /^[ \t]*第[\d零〇一二三四五六七八九十百千万两]+[ \t]*[章回节]/;
function _dropLeadingZhHeading(paras) {
  return (paras.length && _ZH_HEADING_RE.test(paras[0])) ? paras.slice(1) : paras;
}

/* Length-ratio paragraph alignment (Gale-Church-lite). Returns an ordered list
 * of { src:[indices], tgt:[indices] } groups, or null when the two sides are
 * too divergent to align meaningfully (caller falls back to the plain panes).
 * Deterministic. Moves: 1:1, 2:1 (two source paras to one target), 1:2, and
 * 1:0 / 0:1 (a paragraph with no counterpart) at a high penalty so content is
 * grouped rather than dropped. Cost compares each target group's length to its
 * expected length r*sourceLength, where r is the chapter's CN to EN expansion.
 * The backend's segment-store retro-aligner (tm._dp_moves groups=True) runs
 * this same cost model server-side, so the reader rows and the CAT editor
 * grid agree about where merges and splits happened. */
function _alignParas(srcParas, tgtParas) {
  const n = srcParas.length, m = tgtParas.length;
  if (!n || !m) return null;
  // Very divergent counts: 1:1 / 2:1 / 1:2 moves cannot span it cleanly, and
  // the plain panes read better than a forced grouping.
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

function _buildAlignedRows(zhText, enMarkdown, pattern = null) {
  const srcParas = _dropLeadingZhHeading(_splitParas(zhText));
  const tgtParas = _splitParas(enMarkdown);
  if (!srcParas.length || !tgtParas.length) return null;
  const groups = _alignParas(srcParas, tgtParas);
  if (!groups) return null;
  let out = "";
  for (const grp of groups) {
    const srcHtml = grp.src
      .map(idx => {
        const inner = pattern
          ? TermMarks.wrapText(srcParas[idx], "zh", pattern)
          : escapeHtml(srcParas[idx]);
        return `<p>${inner.replace(/\n/g, "<br>")}</p>`;
      })
      .join("");
    const tgtHtml = grp.tgt.map(idx => renderEnglishMarkdown(tgtParas[idx], pattern)).join("");
    out += `<div class="prow"><div class="src" lang="zh">${srcHtml}</div><div class="tgt">${tgtHtml}</div></div>`;
  }
  return out;
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
    // Presence-keyed like the body itself, so the chip does not vanish
    // during a refinement retry window while the polished text stays up.
    const refined = ch && _displayedVariant(ch) === "refined"
      && _displayedEnglish(ch) === ch.refined_text;
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
// cache the response. On Next, _prefetchedChapter is consumed if fresh
// (loadChapter's cache-hit fast path).
// 2-minute TTL, plus three explicit eviction points (L2 fix, 2026-08-08:
// the TTL alone let a stale prefetched BODY survive a glossary rename and
// get served alongside freshly-built term marks):
//   1. Consumption itself (the fast path clears num/data on a cache hit).
//   2. A "glossary" or "inserted" onNovelChange broadcast (novel-wide text
//      rewrite or chapter renumbering can invalidate whatever's cached).
//   3. The top of loadChapter, whenever it's called for the chapter the
//      reader is already on (num === currentCh): a same-chapter reload is
//      always a post-mutation refresh (retranslate, local glossary apply,
//      reread, a poll catching a status flip), and the mutation that
//      changed the CURRENT chapter may equally have rewritten whatever sits
//      in the prefetch slot.
const _PREFETCH_TTL_MS = 2 * 60_000;
const _prefetchedChapter = { num: null, data: null, at: 0 };
// Eviction generation. Clearing the slot cannot cancel an already-issued
// fetch, so every eviction bumps this counter and _prefetchNext's .then
// commits only while its captured value still matches. Without it, a fetch
// in flight across a glossary rewrite re-populates the slot with
// pre-mutation text moments after the eviction that was meant to kill it.
let _prefetchGen = 0;
function _evictPrefetch() {
  _prefetchedChapter.num = null;
  _prefetchedChapter.data = null;
  _prefetchedChapter.at = 0;
  _prefetchGen++;
}

function _prefetchNext(currentDoneChapter) {
  const nextNum = currentDoneChapter + 1;
  // Find the next chapter in cache list; only prefetch when it's also done.
  const cached = chaptersCache.find(c => c.chapter_num === nextNum);
  if (!cached || cached.status !== "done") return;
  if (_prefetchedChapter.num === nextNum
      && Date.now() - _prefetchedChapter.at < _PREFETCH_TTL_MS) {
    return; // already fresh
  }
  const gen = _prefetchGen;
  api.chapter(novelId, nextNum)
    .then(d => {
      if (gen !== _prefetchGen) return; // evicted mid-flight, drop the result
      _prefetchedChapter.num = nextNum;
      _prefetchedChapter.data = d;
      _prefetchedChapter.at = Date.now();
    })
    .catch(() => { /* best-effort */ });
}

// L1 fix (2026-08-08): shared post-render epilogue for a status='done'
// chapter (end-of-chapter card, scroll-position restore, last-read
// breadcrumb). Both loadChapter's normal fetch path and its F14 prefetch-
// cache fast path land on a done chapter and must leave the same UI behind
// them; the fast path used to `return` before any of this ran, so a
// prefetch-served Next left the end card and scroll position from the
// PREVIOUS chapter on screen. Content is unchanged from the normal path's
// former inline version: only the wrapping is new.
function _paintDoneChapterEpilogue(ch, num) {
  persistLastRead(ch);
  endBlock.classList.remove("hidden");
  paintEndCard(ch);
  // Restore the user's last scroll position within this chapter. A one-shot
  // editor -> reader paragraph handoff (Block 5, 2026-08-07) overrides the
  // stored position exactly once; double rAF matches the restore path's own
  // paint-timing choreography, and _scrollToParagraph already suppresses the
  // synthetic scroll save.
  // The handoff is anchored to the chapter the ?para= URL named
  // (pendingDeepParaCh, reader-core.js): landing anywhere else clears it and
  // falls through to that chapter's own saved position rather than stealing
  // the jump.
  if (pendingDeepPara != null && num === pendingDeepParaCh) {
    const targetPara = pendingDeepPara;
    pendingDeepPara = null;
    pendingDeepParaCh = null;
    requestAnimationFrame(() => requestAnimationFrame(() => _scrollToParagraph(targetPara)));
  } else {
    if (pendingDeepPara != null) {
      pendingDeepPara = null;
      pendingDeepParaCh = null;
    }
    restoreScrollFor(num);
  }
}

// Continuation polls, shared by loadChapter's normal fetch path and its F14
// prefetch-cache fast path. Two independent background lanes can still be
// running on a chapter the reader is already looking at:
//   * refinement pending / in_progress on a done chapter: the body is
//     deliberately blank under the no-draft-preview rule, so without a re-poll
//     a prefetch-served chapter stays blank forever;
//   * free_draft pending / in_progress: the mechanical draft lands seconds
//     later and the body switches to it.
// One timer covers both: pollHandle holds a single handle, so arming the two
// separately (the old shape) leaked the first timer whenever both fired.
function _armContinuationPolls(ch, num) {
  if (!ch) return;
  const refining = ch.status === "done"
    && (ch.refinement_status === "pending" || ch.refinement_status === "in_progress");
  const drafting = ch.free_draft_status === "pending"
    || ch.free_draft_status === "in_progress";
  if (refining || drafting) {
    pollHandle = setTimeout(() => loadChapter(num), pollInterval(num, 3000));
  }
}

async function loadChapter(num) {
  // L2 fix (2026-08-08): a same-chapter reload (num === currentCh) is
  // always a post-mutation refresh gesture, never a fresh navigation: see
  // the eviction-rules comment above _prefetchedChapter's declaration.
  // Evict defensively: harmless when the cache is already empty, including
  // on the very first boot call, where num happens to equal the just-set
  // currentCh too.
  if (num === currentCh) {
    _evictPrefetch();
    // Scroll-position protection for a same-chapter reload (the glossary
    // mini-form save, a cross-tab glossary repaint, reread, a poll tick). The
    // body wipe below collapses the document and the browser clamps scrollY
    // toward 0; the resulting scroll event's debounced save would then DELETE
    // this chapter's stored offset (the y <= 16 branch) whenever the fetch
    // outlasts the debounce. Flush a genuinely pending save first (scrollY is
    // still the user's real position at this instant), then suppress saves
    // across the reload window. Only flush when a timer is actually pending:
    // an unconditional _persistCurrentScroll() would run at boot, where
    // scrollY is 0, and erase the very key the epilogue is about to restore.
    // No scrollTo either: a same-chapter reload keeps the reader where they
    // were, and restoreScrollFor runs in the epilogue.
    if (_scrollSaveTimer) {
      clearTimeout(_scrollSaveTimer);
      _scrollSaveTimer = null;
      _persistCurrentScroll();
    }
    _ignoreScrollFor(600);
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
    // Only renderChapterBody repaints the badge, so every path that returns
    // without it has to clear the previous chapter's score itself.
    if (typeof resetQualityBadge === "function") resetQualityBadge();
    return;
  }

  // Drift guard: the TOC cache refreshes on the 6s poll, on visibilitychange
  // and on cross-tab broadcasts, so it can know about a status change the
  // prefetched row predates (a retranslate landed, a glossary edit flipped the
  // row to 'stale', a refinement finished). Serving the cached body then shows
  // the reader a chapter the app itself already considers different. Checked
  // BEFORE the freshness test so an evicted row can never be served.
  if (_prefetchedChapter.num === num && _prefetchedChapter.data) {
    const tocRow = chaptersCache.find(c => c.chapter_num === num);
    if (tocRow && tocRow.status !== _prefetchedChapter.data.status) _evictPrefetch();
  }

  // F14 (2026-05-25): consume the pre-render cache if fresh. The cache
  // hit avoids the API round-trip; the user sees the next chapter
  // render essentially instantly.
  if (_prefetchedChapter.num === num
      && _prefetchedChapter.data
      && Date.now() - _prefetchedChapter.at < _PREFETCH_TTL_MS) {
    const cachedCh = _prefetchedChapter.data;
    _evictPrefetch();
    // Apply the same prologue as the normal path so the loader / scroll
    // reset still fires before render — easier than carving out a
    // shortcut path.
    if (currentCh !== num) {
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
    // L1 fix (2026-08-08): reset the status banner the same way the normal
    // path does (~571-574) so a leftover banner ("Chapter N removed from
    // the translation queue.", a dismissed error, …) doesn't survive a
    // prefetch-served navigation.
    statusEl.className = "status";
    statusEl.textContent = "";
    statusEl.hidden = false;
    statusEl.removeAttribute("role");
    lastChapter = cachedCh;
    // L1 fix (2026-08-08): keep the reading-rail's CSS status hook current,
    // same as the normal path (~597); otherwise it reports the PREVIOUS
    // chapter's status until the next full fetch.
    document.body.dataset.chapterStatus = cachedCh.status || "";
    stopLoader();
    bodyEn.innerHTML = "";
    bodyZh.innerHTML = "";
    document.getElementById("quality-banner")?.remove();
    document.getElementById("glossary-merge-error-card")?.remove();
    renderToc();
    // Parity with the normal path: pull the chapter list when this row's state
    // has drifted from the cache, fire-and-forget so the render isn't blocked.
    refreshTocIfStale(cachedCh);
    renderChapterBody(cachedCh);
    if (cachedCh.status === "done") {
      // L1 fix (2026-08-08): run the same post-render epilogue the normal
      // path runs for a done chapter (end-of-chapter card, scroll restore /
      // pending-paragraph-handoff consumption, last-read breadcrumb)
      // instead of returning before any of it fires.
      // renderChapterBody already prefetched the next chapter for a done row,
      // so there is no explicit _prefetchNext call here.
      _paintDoneChapterEpilogue(cachedCh, num);
    } else {
      endBlock.classList.add("hidden");
      if (typeof resetQualityBadge === "function") resetQualityBadge();
    }
    // Parity with the normal path: a cached done row can still have a
    // refinement or a free draft in flight, and without these polls it would
    // render blank (no-draft-preview) and never update.
    _armContinuationPolls(cachedCh, num);
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
  // Keep the util-menu's CAT-editor deep link on the chapter being read.
  // Null-guarded for cached old HTML without the menu item.
  const openInEditor = document.getElementById("open-in-editor");
  if (openInEditor) openInEditor.href = `/editor?novel=${novelId}&ch=${num}`;
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
      // Nothing below this point calls renderChapterBody, the only site that
      // repaints the badge, and every exit from this branch is a return.
      // Clear it here so the previous chapter's score doesn't sit in the
      // chapter bar over a pending / translating one.
      if (typeof resetQualityBadge === "function") resetQualityBadge();
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
      // Only on status=done: for pending/error states there's nothing
      // meaningful to scroll into yet. (Shared with the F14 prefetch fast
      // path's cache-hit branch: see _paintDoneChapterEpilogue, L1 fix
      // 2026-08-08.)
      _paintDoneChapterEpilogue(ch, num);
    } else {
      endBlock.classList.add("hidden");
    }
    // Refinement-in-flight and free-draft-in-flight continuation polls, shared
    // with the prefetch fast path so a cached chapter gets the same treatment.
    _armContinuationPolls(ch, num);
  } catch (e) {
    if (e.message && e.message.startsWith("404:")) {
      // No body renders on either 404 sub-branch, so drop the previous
      // chapter's quality badge here.
      if (typeof resetQualityBadge === "function") resetQualityBadge();
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
    if (typeof resetQualityBadge === "function") resetQualityBadge();
  }
}

// Reader -> editor paragraph handoff (Block 5, 2026-08-07): rewrite the href
// at click time, not on every render, so the CAT editor lands near whichever
// paragraph the reader had at the top of the viewport. Registered once at
// module scope (openInEditorLink is declared in reader-core.js, which loads
// first); loadChapter keeps the base novel/ch href current on every
// navigation, this listener layers the paragraph on top of that.
openInEditorLink?.addEventListener("click", (e) => {
  const idx = _currentTopParagraphIndex();
  let href = `/editor?novel=${novelId}&ch=${currentCh}`;
  if (Number.isFinite(idx) && idx >= 0) href += `&para=${idx}`;
  e.currentTarget.href = href;
});

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
  // 2026-07-31 presence keying (CAT Phase 4 retry-window fix): refined
  // text is canonical whenever it exists, regardless of refinement_status.
  // A refinement RETRY retains the previous refined_text through its
  // pending/in_progress window (and after a failed retry), and that
  // retained polish must stay on screen: flipping to the draft mid-window
  // both violates the no-draft-preview rule and lets the editor's
  // self-heal rebuild the segment store against the draft. The keying
  // itself lives in reader-core's _displayedVariant (authority:
  // segments.displayed_body on the backend).
  if (_displayedVariant(ch) === "refined") {
    return ch.refined_text;
  }
  // 2026-05-27: first-ever polish mid-flight (no refined_text yet):
  // suppress the draft. The user explicitly does not want to see the draft
  // body and then watch it get replaced by the refined version a minute
  // later — they only want the polished output. The refinement banner
  // still surfaces the "polishing in progress" status.
  if (ch.refinement_status === "pending" || ch.refinement_status === "in_progress") {
    return "";
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
  // One pattern per render, threaded into all three body renders (the cache in
  // term-marks.js keys on the glossaryCache array identity, so this is cheap).
  const termPattern = _termMarksOn() ? TermMarks.buildPattern(glossaryCache) : null;
  bodyEn.innerHTML = renderEnglishMarkdown(enSource, termPattern);
  bodyZh.innerHTML = renderParagraphs(ch.original_text || "", "zh", termPattern);
  // Bilingual row alignment (restored 2026-08-06): in bilingual mode, pair
  // source and translation paragraphs into shared rows so the columns stay
  // in step past merges/splits. Falls back to the independent panes when the
  // two sides diverge too far to align.
  const alignedEl = document.getElementById("aligned-body");
  let alignedOn = false;
  if (dualMode && alignedEl && enSource) {
    const rows = _buildAlignedRows(ch.original_text || "", enSource, termPattern);
    if (rows) { alignedEl.innerHTML = rows; alignedEl.hidden = false; alignedOn = true; }
  }
  if (!alignedOn && alignedEl) { alignedEl.hidden = true; alignedEl.innerHTML = ""; }
  stage.dataset.aligned = alignedOn ? "on" : "off";
  // F14 (2026-05-25): pre-render next chapter so Next click feels
  // instant. Only fires when the current chapter is done; pending /
  // translating chapters skip (no point cacheing what isn't ready).
  if (ch.status === "done") _prefetchNext(ch.chapter_num);
  applyGlossaryMergeBanner(ch);
  applyQualityBanner(ch);
  applyRefinementBanner(ch);
  // Per-chapter quality badge (cockpit). Guarded: reader-quality.js loads after
  // this module, but by the time a chapter actually renders (after awaited
  // network loads) it has executed.
  if (typeof renderQualityBadge === "function") renderQualityBadge(ch);
  updatePaneEnLabel(ch);
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


/* ===========================================================================
 * Nav, actions, reading chrome, bookmarks + boot.
 * Relocated from the retired reader-edit.js (Phase 6): everything below is
 * READING chrome. The editing surfaces that file carried (paragraph editing,
 * QA observations, attempts/last-prompt, insert-chapter, style note,
 * concordance, refresh-free-draft) live in the CAT editor now.
 * ======================================================================== */

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

// Pre-flight confirm gate on the Retranslate action. This is part of the
// TRANSLATE flow (it guards an LLM spend), not edit chrome; the standalone
// pre-check report lives in the CAT editor's util menu.
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
  // dialog would be overkill for a one-off "are you sure?" gate.
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

// Copy the whole chapter (English title + body) to the clipboard. Reads the
// source markdown string straight from the loaded chapter so the copy matches
// the .md download shape rather than the rendered DOM.
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
const termMarksToggle = document.getElementById("term-marks-toggle");
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
  if (termMarksToggle) termMarksToggle.checked = _termMarksOn();
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
  localStorage.removeItem("readerTermMarks");
  document.documentElement.style.removeProperty("--fs-body");
  document.documentElement.style.removeProperty("--fs-body-lh");
  document.documentElement.removeAttribute("data-focus-mode");
  if (fsBodySlider) fsBodySlider.value = String(DEFAULT_FS_BODY);
  if (fsLhSlider) fsLhSlider.value = String(DEFAULT_FS_LH);
  if (focusModeToggle) focusModeToggle.checked = false;
  if (pageTurnSelect) pageTurnSelect.value = "shift";
  // Marks default ON, so the reset repaints the chapter to bring them back.
  if (termMarksToggle) termMarksToggle.checked = true;
  _syncTypeReadouts();
  if (lastChapter) renderChapterBody(lastChapter);
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

/* Glossary term marks. Same shape as the focus-mode toggle: localStorage is
 * the canonical state, read fresh on every render. A flip repaints the current
 * chapter so the change is visible without a reload. */
if (termMarksToggle) {
  termMarksToggle.checked = _termMarksOn();
  termMarksToggle.addEventListener("change", () => {
    localStorage.setItem("readerTermMarks", termMarksToggle.checked ? "1" : "0");
    if (lastChapter) renderChapterBody(lastChapter);
  });
}

document.addEventListener("keydown", (e) => {
  // An open dialog owns the keyboard: b / arrows / "/" would otherwise turn
  // the page behind the shortcuts panel, the bookmark form or the glossary
  // mini dialog, leaving the user editing against a chapter they can't see.
  if (document.querySelector("dialog[open]")) return;
  // Guard inputs so shortcuts don't fire while the user is typing in the TOC
  // search. Modifiers are reserved for browser shortcuts.
  if (e.target.matches("input, textarea, select")) return;
  if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
  if (e.key === "ArrowLeft" || e.key === "h" || e.key === "k") { e.preventDefault(); prevBtn.click(); }
  else if (e.key === "ArrowRight" || e.key === "l" || e.key === "j") { e.preventDefault(); nextBtn.click(); }
  else if (e.key === "b" || e.key === "B") {
    e.preventDefault();
    toggleDual.querySelector(`button[data-mode="${dualMode ? "english" : "bilingual"}"]`)?.click();
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

// ===========================================================================
// Bookmarks (Initiative 2): a READING aid, kept in the reader.
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

// Scroll units = the paragraph elements the top-of-viewport index is
// computed against (bookmark capture, paragraph handoff). Visibility-aware
// (Block 5, 2026-08-07): in bilingual aligned mode #body-en is hidden behind
// #aligned-body, so reading its paragraphs would count elements nobody can
// see. Falls back to #body-en's direct <p> children otherwise.
function _activeScrollUnits() {
  if (stage.dataset.aligned === "on") {
    const aligned = document.getElementById("aligned-body");
    if (aligned && !aligned.hidden) {
      return Array.from(aligned.querySelectorAll(".prow .tgt > p"));
    }
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
  if (!paras.length) return;
  const clamped = Math.max(0, Math.min(paraIndex, paras.length - 1));
  const target = paras[clamped];
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

/* ---- Deferred cross-tab glossary repaint ----
 * A "glossary" broadcast that lands while a dialog is open must not yank the
 * mini add/revise form away mid-edit, but the repaint is still owed: the
 * stored chapter text may have been rewritten by an apply-in-place, so
 * skipping it silently leaves the reader on pre-rename text until the next
 * navigation. Park the repaint and drain it when the last dialog closes.
 * `close` does not bubble, hence the capture-phase listener on document; the
 * one-task defer covers a stacked dialog closing in the same tick (and any
 * ordering where the open attribute has not been dropped yet). */
let _glossaryRepaintPending = false;
document.addEventListener("close", () => {
  if (!_glossaryRepaintPending) return;
  setTimeout(() => {
    if (!_glossaryRepaintPending) return;
    if (document.querySelector("dialog[open]")) return;
    _glossaryRepaintPending = false;
    loadChapter(currentCh);
  }, 0);
}, true);

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
  // Cross-tab refresh (Block 2, 2026-08-07): when another tab appends
  // chapters to this novel, or the glossary changes, pick up the change
  // without waiting for the 6s poll tick. onNovelChange (utils.js) wraps the
  // shared BroadcastChannel and owns the null / novelId matching; a no-op
  // where the browser lacks BroadcastChannel.
  onNovelChange(novelId, (d) => {
    loadChapters();
    refreshNovelMeta();
    // L2 fix (2026-08-08): a "glossary" broadcast means apply-in-place may
    // have rewritten stored chapter text anywhere in the novel
    // (find_replace.py's apply_in_place_for_glossary_term sweeps every
    // chapter), and an "inserted" broadcast means chapter numbers past the
    // insertion point were renumbered (services/uploads.py::
    // insert_parsed_chapters): either way a chapter_num the prefetch cache
    // is holding may now map to different content, so evict it. "appended"
    // is excluded on purpose: it only lands new chapters at the end and
    // never rewrites or renumbers existing ones, so nothing already cached
    // can go stale from it.
    if (d.type === "glossary" || d.type === "inserted") {
      _evictPrefetch();
    }
    if (d.type === "glossary") {
      // Same-chapter reload preserves scroll; the open-dialog guard protects
      // the mini add/revise form mid-edit from a re-render yanking it away.
      // The repaint is deferred rather than dropped, and drains on dialog close.
      loadGlossary().then(() => {
        if (document.querySelector("dialog[open]")) _glossaryRepaintPending = true;
        else loadChapter(currentCh);
      });
    }
  });
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
