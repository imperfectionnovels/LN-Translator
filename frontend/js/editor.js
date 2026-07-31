// CAT editor page (Phase 2: read-only segment grid).
//
// One fetch per chapter open: GET /api/novels/{id}/chapters/{n}/segments
// (the backend lazily builds / self-heals the segment store on that read).
// The page renders exactly what the server returns; there is no client-side
// alignment. Targets render as PLAIN TEXT (escapeHtml + pre-wrap), no
// marked/DOMPurify. Editing, keyboard map, and the assist rail land with
// Phase 3+.
//
// URL shape: /editor?novel=N&ch=M[&seg=K] (param names match reader-core.js
// so spine.js novel-capture works unchanged). A bare /editor?novel=N resumes
// the last-opened chapter via the localStorage breadcrumb editorLast:{N}.

(() => {
  const params = new URLSearchParams(location.search);
  const novelId = Number.parseInt(params.get("novel"), 10);
  if (!Number.isFinite(novelId)) {
    // Same bounce as the reader boot: no novel context, go pick one.
    location.replace("/library");
    return;
  }

  const LAST_KEY = `editorLast:${novelId}`;
  let currentCh = Number.parseInt(params.get("ch"), 10);
  if (!Number.isFinite(currentCh)) {
    try {
      currentCh = JSON.parse(localStorage.getItem(LAST_KEY) || "{}").ch;
    } catch (_) { /* corrupted breadcrumb: fall through */ }
    if (!Number.isFinite(currentCh)) currentCh = 1;
  }
  let focusSeg = Number.parseInt(params.get("seg"), 10);
  if (!Number.isFinite(focusSeg)) focusSeg = null;

  // --- DOM handles (all ids are static in editor.html) ---
  const crumbNovel = document.getElementById("editor-crumb-novel");
  const crumbCh = document.getElementById("editor-crumb-ch");
  const prevBtn = document.getElementById("prev-ch");
  const nextBtn = document.getElementById("next-ch");
  const chSelect = document.getElementById("ch-select");
  const progressLabel = document.getElementById("progress-label");
  const progressBar = document.getElementById("progress-bar");
  const progressFill = document.getElementById("progress-fill");
  const readerLink = document.getElementById("reader-link");
  const statusEl = document.getElementById("editor-status");
  const gridHead = document.getElementById("seg-grid-head");
  const grid = document.getElementById("seg-grid");

  crumbNovel.href = `/novel?id=${novelId}`;

  let chaptersCache = []; // ChapterSummary list, ordered by chapter_num
  let pollTimer = null;
  let loadSeq = 0; // stale-response guard for fast chapter switches

  // --- URL + breadcrumb ---
  function syncUrl() {
    const seg = focusSeg != null ? `&seg=${focusSeg}` : "";
    history.replaceState(null, "", `/editor?novel=${novelId}&ch=${currentCh}${seg}`);
  }
  function saveBreadcrumb() {
    try {
      localStorage.setItem(LAST_KEY, JSON.stringify({ ch: currentCh }));
    } catch (_) { /* storage full or blocked: resume just degrades */ }
  }

  // --- Editor bar ---
  function chapterIndex(n) {
    return chaptersCache.findIndex(c => c.chapter_num === n);
  }
  function updateBar() {
    crumbCh.textContent = `Ch. ${currentCh}`;
    readerLink.href = `/reader?novel=${novelId}&ch=${currentCh}`;
    chSelect.value = String(currentCh);
    const i = chapterIndex(currentCh);
    prevBtn.disabled = i <= 0;
    nextBtn.disabled = i < 0 || i >= chaptersCache.length - 1;
  }
  function renderProgress(p) {
    progressLabel.textContent = `${p.confirmed} / ${p.total} confirmed`;
    progressBar.setAttribute("aria-valuemax", String(p.total));
    progressBar.setAttribute("aria-valuenow", String(p.confirmed));
    progressFill.style.width =
      p.total ? `${((100 * p.confirmed) / p.total).toFixed(1)}%` : "0";
  }

  // --- Status banner cards ---
  function showStatus(kind, html) {
    statusEl.className = `status editor-status ${kind}`;
    statusEl.innerHTML = html;
    statusEl.hidden = false;
  }
  function clearStatus() {
    statusEl.hidden = true;
    statusEl.innerHTML = "";
  }
  function readerHref() {
    return `/reader?novel=${novelId}&ch=${currentCh}`;
  }

  // --- Segment grid ---
  function statusLabel(s) {
    if (s === "edited") return "Edited";
    if (s === "confirmed") return "Confirmed";
    return "AI";
  }
  function renderSegments(data) {
    gridHead.hidden = false;
    grid.innerHTML = data.segments.map(s => `
      <div class="seg-row" data-seg="${s.index}" data-status="${escapeHtml(s.status)}"
           data-provenance="${escapeHtml(s.origin)}">
        <span class="seg-idx">${s.index + 1}</span>
        <div class="seg-src" lang="zh">${escapeHtml(s.source_text)}</div>
        <div class="seg-tgt">${
          s.target_text
            ? escapeHtml(s.target_text)
            : `<span class="seg-empty">(no paragraph assigned)</span>`
        }</div>
        <div class="seg-meta">
          <span class="seg-badge seg-badge-${escapeHtml(s.status)}">${statusLabel(s.status)}</span>
          ${s.aligned ? "" : `<span class="seg-chip-review" title="Automatic alignment could not pin this row to a single source paragraph. Check it against the source.">needs review</span>`}
        </div>
      </div>`).join("");
  }
  function selectRow(row, { scroll = true } = {}) {
    grid.querySelectorAll(".seg-row.active").forEach(r => r.classList.remove("active"));
    row.classList.add("active");
    focusSeg = Number.parseInt(row.dataset.seg, 10);
    syncUrl();
    if (scroll) row.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  // Row interaction via delegation only (rows carry data-seg, no ids).
  grid.addEventListener("click", (e) => {
    const row = e.target.closest(".seg-row");
    if (row) selectRow(row, { scroll: false });
  });

  // --- Load + state routing ---
  async function loadSegments() {
    const seq = ++loadSeq;
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    grid.setAttribute("aria-busy", "true");
    grid.innerHTML = "";
    gridHead.hidden = true;
    clearStatus();
    let data;
    try {
      data = await api.chapterSegments(novelId, currentCh);
    } catch (e) {
      if (seq !== loadSeq) return;
      grid.setAttribute("aria-busy", "false");
      showStatus("err", `Could not load this chapter's segments: ${escapeHtml(e.message)}`);
      return;
    }
    if (seq !== loadSeq) return;
    grid.setAttribute("aria-busy", "false");
    renderState(data);
  }
  function renderState(data) {
    renderProgress(data.progress);
    if (data.chapter_status === "pending") {
      showStatus("info",
        `This chapter is not translated yet. ` +
        `<a href="${readerHref()}">Open it in the reader</a> to translate it, ` +
        `then come back to edit the segments.`);
      return;
    }
    if (data.chapter_status === "translating") {
      showStatus("info",
        `Translating this chapter now. The segment grid appears automatically when it finishes.`);
      pollTimer = setTimeout(loadSegments, 2500);
      return;
    }
    if (data.chapter_status === "error") {
      showStatus("err",
        `Translation failed for this chapter. ` +
        `<a href="${readerHref()}">Open it in the reader</a> to see the error and retry.`);
      return;
    }
    if (data.segments_state === "unaligned") {
      showStatus("info",
        `Automatic paragraph alignment failed for this chapter. ` +
        `Retranslate it: newer translations enforce 1:1 paragraphs. ` +
        `<a href="${readerHref()}">Open it in the reader</a> to retranslate.`);
      return;
    }
    clearStatus();
    renderSegments(data);
    if (focusSeg != null) {
      const row = grid.querySelector(`.seg-row[data-seg="${focusSeg}"]`);
      if (row) selectRow(row);
    }
  }

  // --- Chapter navigation ---
  function gotoChapter(n) {
    if (n === currentCh) return;
    currentCh = n;
    focusSeg = null;
    syncUrl();
    saveBreadcrumb();
    updateBar();
    loadSegments();
  }
  prevBtn.addEventListener("click", () => {
    const i = chapterIndex(currentCh);
    if (i > 0) gotoChapter(chaptersCache[i - 1].chapter_num);
  });
  nextBtn.addEventListener("click", () => {
    const i = chapterIndex(currentCh);
    if (i >= 0 && i < chaptersCache.length - 1) {
      gotoChapter(chaptersCache[i + 1].chapter_num);
    }
  });
  chSelect.addEventListener("change", () => {
    const n = Number.parseInt(chSelect.value, 10);
    if (Number.isFinite(n)) gotoChapter(n);
  });

  // --- Boot ---
  (async () => {
    let novel, chapters;
    try {
      [novel, chapters] = await Promise.all([
        api.novel(novelId),
        api.chapters(novelId),
      ]);
    } catch (e) {
      grid.setAttribute("aria-busy", "false");
      showStatus("err", `Could not load this novel: ${escapeHtml(e.message)}`);
      return;
    }
    document.title = `Editor · ${novel.title}`;
    crumbNovel.textContent = novel.title;
    chaptersCache = chapters;
    if (!chapters.length) {
      grid.setAttribute("aria-busy", "false");
      showStatus("info", "This novel has no chapters yet.");
      return;
    }
    if (chapterIndex(currentCh) < 0) currentCh = chapters[0].chapter_num;
    chSelect.innerHTML = chapters.map(c => {
      const title = c.title_en || c.title_zh || "";
      return `<option value="${c.chapter_num}">Ch. ${c.chapter_num}${title ? ` · ${escapeHtml(title)}` : ""}</option>`;
    }).join("");
    syncUrl();
    saveBreadcrumb();
    updateBar();
    await loadSegments();
  })();
})();
