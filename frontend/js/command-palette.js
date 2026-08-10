/* Command palette + global nav chords (F19, 2026-05-25).
 *
 * Cmd/Ctrl+K opens a centered modal with fuzzy-search action list.
 *
 * Global nav chord: `g` followed within 1.5s by one of l/r/e/g/i/q/s/n/y/b
 * jumps to library/reader/editor/glossary/import/queue/settings/novel-page/
 * quality/global-glossary. Reader/Editor/Glossary/Quality use the lastNovel
 * localStorage (same as spine.js) so a "no novel context" tap still
 * goes somewhere sensible.
 *
 * Loaded on every HTML page (analogous to spine.js). Self-mounting via
 * a singleton guard so multiple <script> includes don't double-bind.
 */

(function () {
  if (window.__commandPaletteMounted) return;
  window.__commandPaletteMounted = true;

  // ---- DOM scaffolding (created lazily on first open) ----
  let dialog = null;
  let input = null;
  let resultsEl = null;
  let _allActions = null;
  let _filtered = null;
  let _activeIdx = 0;

  function _ensureMounted() {
    if (dialog) return;
    dialog = document.createElement("dialog");
    dialog.id = "command-palette-dialog";
    dialog.className = "dialog";
    dialog.style.cssText =
      "width: min(560px, 90vw); padding: 0; border: 1px solid var(--border); " +
      "border-radius: var(--r-md); background: var(--bg);";
    dialog.innerHTML = `
      <div style="padding: 12px 16px; border-bottom: 1px solid var(--border);">
        <input id="command-palette-input" type="text"
               placeholder="Type a command…"
               autocomplete="off" spellcheck="false"
               style="width: 100%; box-sizing: border-box; padding: 8px 10px;
                      border: 1px solid var(--border); border-radius: var(--r-md);
                      background: var(--bg); color: var(--fg); font-size: 14px;">
      </div>
      <ul id="command-palette-results" role="listbox"
          style="list-style: none; margin: 0; padding: 6px 0; max-height: 50vh;
                 overflow-y: auto;"></ul>
      <div style="padding: 6px 16px; border-top: 1px solid var(--border);
                  font-size: 11px; color: var(--muted);">
        ↑↓ navigate · ⏎ run · esc close
      </div>
    `;
    document.body.appendChild(dialog);
    input = document.getElementById("command-palette-input");
    resultsEl = document.getElementById("command-palette-results");

    input.addEventListener("input", _renderResults);
    input.addEventListener("keydown", _onKeyDown);
    dialog.addEventListener("cancel", () => { /* esc closes default */ });
    resultsEl.addEventListener("click", (e) => {
      const li = e.target.closest("li[data-cmd-idx]");
      if (!li) return;
      const idx = parseInt(li.dataset.cmdIdx, 10);
      _runByFilteredIdx(idx);
    });
  }

  // ---- Action registry ----
  function _lastNovelId() {
    try { return localStorage.getItem("ink:lastNovel") || ""; }
    catch { return ""; }
  }

  function _isReaderPage() {
    return location.pathname === "/reader";
  }

  // Each action: {id, label, hint, run}.
  function _buildActions() {
    const novelQs = _lastNovelId() ? `?novel=${_lastNovelId()}` : "";
    const novelIdQs = _lastNovelId() ? `?id=${_lastNovelId()}` : "";
    const out = [
      { id: "nav-library", label: "Go to library", hint: "g l", run: () => location.href = "/library" },
      { id: "nav-reader", label: "Open reader", hint: "g r", run: () => location.href = `/reader${novelQs}` },
      { id: "nav-editor", label: "Open CAT editor", hint: "g e", run: () => location.href = `/editor${novelQs}` },
      { id: "nav-glossary", label: "Open glossary", hint: "g g", run: () => location.href = `/glossary${novelQs}` },
      { id: "nav-glossary-global", label: "Global glossary", hint: "g b", run: () => location.href = "/glossary/global" },
      { id: "nav-quality", label: "Quality cockpit", hint: "g y", run: () => location.href = `/quality${novelQs}` },
      { id: "nav-novel-page", label: "Open novel page", hint: "g n", run: () => location.href = `/novel${novelIdQs}` },
      { id: "nav-import", label: "Import chapters", hint: "g i", run: () => location.href = "/" },
      { id: "nav-queue", label: "Open queue", hint: "g q", run: () => location.href = "/queue" },
      { id: "nav-settings", label: "Open app settings", hint: "g s", run: () => location.href = "/settings" },
      { id: "nav-find-replace", label: "Find & Replace", run: () => location.href = "/find-replace" },
      { id: "nav-stats", label: "Stats dashboard", run: () => location.href = "/stats" },
      // Theme actions — always available; call into theme.js setters.
      { id: "theme-rice", label: "Theme: Rice (light)", run: () => window.__setTheme?.("rice") },
      { id: "theme-vellum", label: "Theme: Vellum (warm)", run: () => window.__setTheme?.("vellum") },
      { id: "theme-inkstone", label: "Theme: Inkstone (dark)", run: () => window.__setTheme?.("inkstone") },
      { id: "theme-cinnabar", label: "Theme: Cinnabar (warm dark)", run: () => window.__setTheme?.("cinnabar") },
      { id: "theme-celadon", label: "Theme: Celadon (sage)", run: () => window.__setTheme?.("celadon") },
      { id: "theme-follow-system", label: "Theme: Follow system", run: () => window.__followSystemTheme?.() },
    ];

    if (_isReaderPage()) {
      out.push(
        { id: "reader-toggle-bilingual", label: "Toggle bilingual view", run: () => {
          const btn = document.querySelector(`#toggle-dual button[data-mode="bilingual"]`);
          btn?.click();
        }},
        { id: "reader-retranslate", label: "Retranslate this chapter", run: () => {
          document.getElementById("retranslate")?.click();
        }},
        { id: "reader-bookmark", label: "Bookmark this paragraph", run: () => {
          document.getElementById("bookmark-add")?.click();
        }},
      );
    }

    return out;
  }

  function _availableActions() {
    return _buildActions();
  }

  // ---- Fuzzy filter ----
  function _matchScore(label, q) {
    if (!q) return 1;
    const lower = label.toLowerCase();
    const lq = q.toLowerCase();
    if (lower.includes(lq)) return 2;
    // Acronym / character-skip match: every char of q appears in order.
    let li = 0;
    for (const ch of lq) {
      const idx = lower.indexOf(ch, li);
      if (idx < 0) return 0;
      li = idx + 1;
    }
    return 1;
  }

  function _renderResults() {
    const q = (input.value || "").trim();
    const scored = _allActions.map(a => ({ a, s: _matchScore(a.label, q) }))
      .filter(x => x.s > 0)
      .sort((x, y) => y.s - x.s);
    _filtered = scored.map(x => x.a);
    _activeIdx = 0;
    resultsEl.innerHTML = _filtered.map((a, i) => `
      <li role="option" data-cmd-idx="${i}"
          style="padding: 8px 14px; cursor: pointer; display: flex; justify-content: space-between; gap: 12px; ${i === 0 ? "background: var(--bg-tint);" : ""}">
        <span>${_escapeHtml(a.label)}</span>
        ${a.hint ? `<span class="muted" style="font-size: 11px; font-family: var(--font-family-mono, monospace);">${_escapeHtml(a.hint)}</span>` : ""}
      </li>
    `).join("") || `<li class="muted" style="padding: 8px 14px;">No matching commands.</li>`;
  }

  function _setActive(idx) {
    _activeIdx = Math.max(0, Math.min(idx, _filtered.length - 1));
    Array.from(resultsEl.children).forEach((el, i) => {
      el.style.background = i === _activeIdx ? "var(--bg-tint)" : "";
      if (i === _activeIdx) el.scrollIntoView({ block: "nearest" });
    });
  }

  function _runByFilteredIdx(idx) {
    const a = _filtered[idx];
    if (!a) return;
    dialog.close();
    try { a.run(); } catch (e) { console.error("command-palette: action failed", e); }
  }

  function _onKeyDown(e) {
    if (e.key === "ArrowDown") { e.preventDefault(); _setActive(_activeIdx + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); _setActive(_activeIdx - 1); }
    else if (e.key === "Enter") { e.preventDefault(); _runByFilteredIdx(_activeIdx); }
  }

  function _escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
    })[c]);
  }

  // ---- Open + global hotkey ----
  function open() {
    _ensureMounted();
    _allActions = _availableActions();
    input.value = "";
    _renderResults();
    if (!dialog.open) dialog.showModal();
    setTimeout(() => input.focus(), 0);
  }

  document.addEventListener("keydown", (e) => {
    // Cmd/Ctrl+K opens (or refocuses) the palette.
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      // Guard against a stray dialog already open (insert-chapter draft,
      // import preview, purge confirm): showModal over it would orphan
      // focus and stack modals. The palette's OWN dialog doesn't count, so
      // Ctrl/Cmd+K still works while the palette itself is open.
      const openDialog = document.querySelector("dialog[open]");
      if (openDialog && openDialog !== dialog) return;
      e.preventDefault();
      open();
      return;
    }
  });

  // ---- Global nav chord: `g` then a single key, within 1.5s ----
  // Guards: not inside an input/textarea/select, no modifiers, sequence
  // resets on any non-g first key or on timeout.
  let _chordPending = false;
  let _chordTimer = null;
  function _resetChord() {
    _chordPending = false;
    if (_chordTimer) clearTimeout(_chordTimer);
    _chordTimer = null;
  }

  const _CHORD_MAP = {
    l: () => location.href = "/library",
    r: () => location.href = `/reader${_lastNovelId() ? `?novel=${_lastNovelId()}` : ""}`,
    e: () => location.href = `/editor${_lastNovelId() ? `?novel=${_lastNovelId()}` : ""}`,
    g: () => location.href = `/glossary${_lastNovelId() ? `?novel=${_lastNovelId()}` : ""}`,
    i: () => location.href = "/",
    q: () => location.href = "/queue",
    s: () => location.href = "/settings",
    n: () => location.href = `/novel${_lastNovelId() ? `?id=${_lastNovelId()}` : ""}`,
    y: () => location.href = `/quality${_lastNovelId() ? `?novel=${_lastNovelId()}` : ""}`,
    b: () => location.href = "/glossary/global",
  };

  document.addEventListener("keydown", (e) => {
    // A stray g-chord while any modal is open (insert-chapter draft, import
    // preview, purge confirm) must not navigate the page away and destroy
    // the user's in-progress work.
    if (document.querySelector("dialog[open]")) return;
    if (e.target.matches?.("input, textarea, select, [contenteditable]")) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = (e.key || "").toLowerCase();
    if (_chordPending) {
      _resetChord();
      const action = _CHORD_MAP[k];
      if (action) { e.preventDefault(); action(); }
      return;
    }
    if (k === "g") {
      _chordPending = true;
      _chordTimer = setTimeout(_resetChord, 1500);
    }
  });

  // Expose for explicit programmatic open (e.g., a "?" hint button).
  window.__openCommandPalette = open;
})();
