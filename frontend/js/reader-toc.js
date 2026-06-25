/* ---- TOC rendering ---- */
// Status glyphs (color-blind safe + redundancy of color). Active row keeps no
// glyph since it gets the seal-mark treatment via CSS.
function statusGlyph(state) {
  return state === "done" ? "✓"
       : state === "translating" ? "◐"
       : state === "queued" ? "⏳"
       : state === "error" ? "!"
       : state === "stale" ? "↻"
       : "·";
}
function chapterState(c) {
  // Single-pass pipeline: translator state is the only state. Queued is
  // distinct from pending so the user can see queue depth in the TOC.
  if (c.status === "error") return "error";
  if (c.status === "translating") return "translating";
  if (c.status === "done") return "done";
  if (c.translate_queued) return "queued";
  if (c.status === "stale") return "stale";
  return "pending";
}
let _ftsHits = null; // {q: lastQuery, matches: [...]}

// `chaptersCache` is loaded once at page open and never refreshed
// automatically, so the TOC glyphs (queued, translating, done, stale, …)
// drift out of date during a long session: chapters move through the queue,
// glossary edits in another tab flip rows to 'stale', etc. Whenever we
// re-fetch a single chapter we compare it to the cached entry and only
// re-pull the whole list when something visible to the TOC actually moved.
// One-navigation-behind staleness in exchange for a single small GET.
// loadChapters() (defined below) is the unconditional refresh; callers that
// already know state moved (retranslate/cancel) should use it.
async function refreshTocIfStale(ch) {
  if (!ch) return;
  const cached = chaptersCache.find(c => c.chapter_num === ch.chapter_num);
  const drift = !cached || (
    cached.status !== ch.status ||
    Boolean(cached.translate_queued) !== Boolean(ch.translate_queued)
  );
  if (!drift) return;
  try { await loadChapters(); }
  catch (_) { /* transient failure — keep the stale cache */ }
}

function renderToc() {
  const q = (tocSearch.value || "").toLowerCase();
  const filtered = chaptersCache.filter(c => {
    if (!q) return true;
    const t = (c.title_en || c.title_zh || "").toLowerCase();
    return t.includes(q) || String(c.chapter_num).includes(q);
  });
  tocList.setAttribute("aria-busy", "false");
  if (filtered.length === 0) {
    // No title hit — surface a "Search body" CTA that runs the FTS query.
    // Cached on `_ftsHits` so repeat keystrokes that produce the same query
    // don't re-fire the request.
    if (_ftsHits && _ftsHits.q === q && _ftsHits.matches.length > 0) {
      tocList.innerHTML = _ftsHits.matches.map(m => `
        <a class="toc-row toc-fts-row" href="/reader?novel=${novelId}&ch=${m.chapter_num}" data-ch="${m.chapter_num}">
          <span class="ti">${escapeHtml(m.title_en || displayTitleZh(m.title_zh) || "")}</span>
          <div class="toc-snippet">${highlightSnippet(m.snippet)}</div>
        </a>
      `).join("");
      tocList.querySelectorAll(".toc-row").forEach(row => {
        row.addEventListener("click", (e) => {
          if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0) return;
          e.preventDefault();
          loadChapter(parseInt(row.dataset.ch, 10));
        });
      });
      return;
    }
    tocList.innerHTML = `
      <div class="muted" style="padding: 12px 14px;">
        No title matches.
        <button type="button" class="btn-ghost" id="toc-fts-search" style="margin-left:6px;">Search body text</button>
      </div>`;
    document.getElementById("toc-fts-search")?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Searching…";
      try {
        const res = await api.searchChapters(novelId, q);
        _ftsHits = { q, matches: res.matches || [] };
        renderToc();
      } catch (err) {
        btn.disabled = false;
        btn.textContent = `Search failed: ${err.message}`;
      }
    });
    return;
  }
  tocList.innerHTML = filtered.map(c => {
    const state = chapterState(c);
    const cls = state !== "pending" ? state : "";
    const title = c.title_en || displayTitleZh(c.title_zh) || `Chapter ${c.chapter_num}`;
    const active = c.chapter_num === currentCh ? " active" : "";
    const aria = `aria-label="Chapter ${c.chapter_num}, ${state}"`;
    // Queued rows get a click-to-cancel × in place of the static ⏳ glyph.
    // chapterState() only returns "queued" for rows that are flagged AND not
    // in-flight, so the × never appears on the chapter currently holding a
    // stage lock (which can't be cancelled mid-LLM-call anyway).
    const trailing = active
      ? ""
      : state === "queued"
        ? `<button class="toc-cancel" data-cancel="${c.chapter_num}" title="Remove from queue" aria-label="Remove chapter ${c.chapter_num} from queue">×</button>`
        : `<span class="st">${statusGlyph(state)}</span>`;
    return `<a class="toc-row ${cls}${active}" href="/reader?novel=${novelId}&ch=${c.chapter_num}" data-ch="${c.chapter_num}" ${aria}>
      <span class="ti">${escapeHtml(title)}</span>
      ${trailing}
    </a>`;
  }).join("");
  tocList.querySelectorAll(".toc-row").forEach(row => {
    row.addEventListener("click", (e) => {
      // Don't navigate when the user clicked the row's cancel button.
      if (e.target.closest(".toc-cancel")) return;
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0) return;
      e.preventDefault();
      loadChapter(parseInt(row.dataset.ch, 10));
    });
  });
  tocList.querySelectorAll(".toc-cancel").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      cancelOneFromQueue(parseInt(btn.dataset.cancel, 10), btn);
    });
  });
  const total = chaptersCache.length;
  const done = chaptersCache.filter(c => c.status === "done").length;
  const queued = chaptersCache.filter(c => c.translate_queued).length;
  // Surface the queue depth in the TOC footer so the user knows what's
  // outstanding without having to scan the rail. When queued > 0, also offer
  // a one-click "Clear queue" affordance — the bulk DELETE endpoint leaves
  // the single in-flight chapter per stage running (it can't be cancelled
  // mid-LLM-call) but drops every waiting chapter.
  if (queued > 0) {
    tocFootStats.innerHTML =
      `${done} / ${total} translated · ${queued} queued ` +
      `<button type="button" id="toc-clear-queue" class="toc-clear-queue" ` +
      `title="Remove every waiting chapter from the queue (in-flight chapter finishes)">Clear</button>`;
    const clearBtn = document.getElementById("toc-clear-queue");
    if (clearBtn) clearBtn.addEventListener("click", () => cancelAllFromQueue(clearBtn));
  } else {
    tocFootStats.textContent = `${done} / ${total} translated`;
  }
  // Surface the jump-to-pending button only when there's actually a pending
  // chapter to jump to. Status counts as "pending" if it's untranslated or
  // still mid-translate — anything that won't be readable right now.
  const jumpBtn = document.getElementById("toc-jump-pending");
  if (jumpBtn) {
    const firstPending = chaptersCache.find(c => c.status !== "done" && c.status !== "error");
    if (firstPending) {
      jumpBtn.hidden = false;
      jumpBtn.textContent = `Jump to next pending (Ch. ${firstPending.chapter_num}) →`;
      jumpBtn.onclick = () => loadChapter(firstPending.chapter_num);
    } else {
      jumpBtn.hidden = true;
    }
  }
  // Mass-queue button. Visible whenever at least one chapter is queueable
  // (i.e. not currently translating, not already done, and not already
  // waiting in the queue). Errored chapters count too because the dialog's
  // include-errors checkbox lets the user opt them in.
  const massQueueBtn = document.getElementById("toc-mass-queue");
  if (massQueueBtn) {
    const queueable = chaptersCache.some(c =>
      c.status !== "done" && c.status !== "translating" && !c.translate_queued
    );
    massQueueBtn.hidden = !queueable;
  }
  // 2026-05-25: error banner. Counts chapters in `error` status and
  // surfaces a one-click Retry-all action. Replaces the old stale-
  // chapter banner — per user feedback the stale concept stayed
  // server-side but stopped being a sidebar concern. Visible in both
  // Read and Edit modes since a failed translation is a state the
  // reader cares about regardless of mode.
  const errorBanner = document.getElementById("toc-error-banner");
  const errorCount = document.getElementById("toc-error-count");
  if (errorBanner && errorCount) {
    const failedN = chaptersCache.filter(c => c.status === "error").length;
    // Show only when there is an actual problem AND the user hasn't dismissed
    // this (or a smaller) failure set. New failures beyond the dismissed count
    // re-surface it.
    if (failedN > 0 && failedN > _errorBannerDismissedCount) {
      errorBanner.hidden = false;
      errorCount.textContent =
        `${failedN} failed chapter${failedN === 1 ? "" : "s"}`;
    } else {
      errorBanner.hidden = true;
    }
  }

  const active = tocList.querySelector(".toc-row.active");
  if (active && !tocUserScrolling()) active.scrollIntoView({ block: "nearest" });
}

/* ---- Mass-queue dialog ---- */
// One-click bulk translate. Lets the user queue many chapters from the TOC
// rail in one gesture instead of clicking each chapter's Retranslate. The
// preview count is derived from chaptersCache so we don't pay a round-trip
// before the user commits — chaptersCache is also what /api uses, so the
// preview matches the server's view as long as the page isn't stale.
(function bindMassQueueDialog() {
  const openBtn = document.getElementById("toc-mass-queue");
  const dialog = document.getElementById("mass-queue-dialog");
  if (!openBtn || !dialog) return;
  const rangeBox = dialog.querySelector("#mass-queue-range");
  const fromInput = dialog.querySelector("#mass-queue-from");
  const toInput = dialog.querySelector("#mass-queue-to");
  const includeErrorsCb = dialog.querySelector("#mass-queue-include-errors");
  const preview = dialog.querySelector("#mass-queue-preview");
  const errorEl = dialog.querySelector("#mass-queue-error");
  const submitBtn = dialog.querySelector("#mass-queue-submit");
  const cancelBtn = dialog.querySelector("#mass-queue-cancel");

  function selectedMode() {
    const checked = dialog.querySelector("input[name='mass-queue-mode']:checked");
    return checked ? checked.value : "all_untranslated";
  }

  function clearError() {
    errorEl.style.display = "none";
    errorEl.textContent = "";
  }
  function showError(msg) {
    errorEl.style.display = "";
    errorEl.textContent = msg;
  }

  function refreshPreview() {
    clearError();
    const mode = selectedMode();
    rangeBox.hidden = mode !== "range";
    const includeErrors = includeErrorsCb.checked;
    let candidates = chaptersCache;
    if (mode === "range") {
      const from = parseInt(fromInput.value, 10);
      const to = parseInt(toInput.value, 10);
      if (!Number.isFinite(from) || !Number.isFinite(to)) {
        preview.textContent = "Enter a From and To chapter number to see the count.";
        return;
      }
      if (from > to) {
        preview.textContent = "From chapter must be less than or equal to To.";
        return;
      }
      candidates = chaptersCache.filter(c =>
        c.chapter_num >= from && c.chapter_num <= to
      );
    }
    let willQueue = 0;
    let skipDone = 0, skipInFlight = 0, skipAlreadyQueued = 0, skipError = 0;
    for (const c of candidates) {
      if (c.status === "translating") { skipInFlight++; continue; }
      if (c.status === "done") { skipDone++; continue; }
      if (c.translate_queued) { skipAlreadyQueued++; continue; }
      if (c.status === "error") {
        if (includeErrors) willQueue++;
        else skipError++;
        continue;
      }
      willQueue++;
    }
    const parts = [`${willQueue} chapter${willQueue === 1 ? "" : "s"} will be queued.`];
    const skipBits = [];
    if (skipDone) skipBits.push(`${skipDone} already done`);
    if (skipInFlight) skipBits.push(`${skipInFlight} translating now`);
    if (skipAlreadyQueued) skipBits.push(`${skipAlreadyQueued} already queued`);
    if (skipError) skipBits.push(`${skipError} failed (skipped)`);
    if (skipBits.length) parts.push(`Skipping ${skipBits.join(", ")}.`);
    preview.textContent = parts.join(" ");
    submitBtn.disabled = willQueue === 0;
  }

  function openDialog() {
    clearError();
    // Default range bounds = full chapter span. Saves the user typing on the
    // common case ("queue chapters 50–200") and makes the preview meaningful
    // immediately when the user flips to Range.
    const nums = chaptersCache.map(c => c.chapter_num);
    if (nums.length) {
      fromInput.value = Math.min(...nums);
      toInput.value = Math.max(...nums);
    }
    // Always reset to the default mode on open so a previous Range selection
    // doesn't surprise the user the next time they open the dialog.
    const allRadio = dialog.querySelector("input[name='mass-queue-mode'][value='all_untranslated']");
    if (allRadio) allRadio.checked = true;
    includeErrorsCb.checked = true;
    rangeBox.hidden = true;
    refreshPreview();
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function closeDialog() {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  openBtn.addEventListener("click", openDialog);
  cancelBtn.addEventListener("click", closeDialog);
  dialog.querySelectorAll("input[name='mass-queue-mode']").forEach(r => {
    r.addEventListener("change", refreshPreview);
  });
  fromInput.addEventListener("input", refreshPreview);
  toInput.addEventListener("input", refreshPreview);
  includeErrorsCb.addEventListener("change", refreshPreview);

  submitBtn.addEventListener("click", async () => {
    const mode = selectedMode();
    const body = { mode, include_errors: includeErrorsCb.checked };
    if (mode === "range") {
      const from = parseInt(fromInput.value, 10);
      const to = parseInt(toInput.value, 10);
      if (!Number.isFinite(from) || !Number.isFinite(to) || from > to) {
        showError("Enter a valid From / To range.");
        return;
      }
      body.from_chapter = from;
      body.to_chapter = to;
    }
    submitBtn.disabled = true;
    const oldLabel = submitBtn.textContent;
    submitBtn.textContent = "Queueing…";
    try {
      const res = await api.massQueueChapters(novelId, body);
      closeDialog();
      // Refresh the TOC so the queued glyphs appear immediately.
      if (typeof loadChapters === "function") await loadChapters();
      // Best-effort toast through the existing status pipeline. Falls back to
      // a quiet console line when the toast helper isn't on this page.
      const msg = `Queued ${res.queued_count} chapter${res.queued_count === 1 ? "" : "s"} for translation.`;
      if (typeof window.toast === "function") window.toast(msg);
      else console.info(msg);
    } catch (err) {
      showError(`Queue failed: ${err.message || err}`);
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = oldLabel;
    }
  });
})();

// Retry-all handler for the error banner. Loops through every `error`
// chapter and re-queues it via /retranslate; failures don't abort the
// loop (we want best-effort coverage and the user can see the per-row
// status afterward).
document.getElementById("toc-retry-failed")?.addEventListener("click", async () => {
  const failed = chaptersCache.filter(c => c.status === "error");
  if (failed.length === 0) return;
  const btn = document.getElementById("toc-retry-failed");
  if (btn) { btn.disabled = true; btn.textContent = "Queueing…"; }
  try {
    for (const c of failed) {
      try { await api.retranslate(novelId, c.chapter_num); }
      catch (_) { /* keep going; surface aggregate result */ }
    }
    if (typeof loadChapters === "function") await loadChapters();
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Retry all"; }
  }
});

// Dismiss the failed-chapters banner for this session. Records the current
// failed count so renderToc keeps it hidden until a NEW failure pushes the
// count higher (see _errorBannerDismissedCount).
document.getElementById("toc-error-dismiss")?.addEventListener("click", () => {
  _errorBannerDismissedCount = chaptersCache.filter(c => c.status === "error").length;
  const banner = document.getElementById("toc-error-banner");
  if (banner) banner.hidden = true;
});

/* Suppress auto-scroll of the active TOC row when the user is actively
 * scrolling the rail themselves. Resets 500ms after the last scroll event. */
let tocLastScrolledAt = 0;
tocList.addEventListener("scroll", () => { tocLastScrolledAt = Date.now(); }, { passive: true });
function tocUserScrolling() {
  return Date.now() - tocLastScrolledAt < 500;
}
let tocSearchTimer = null;
tocSearch.addEventListener("input", () => {
  // Novels can have thousands of chapters; debounce so typing doesn't tear
  // down and rebuild the entire TOC list on every keystroke.
  if (tocSearchTimer) clearTimeout(tocSearchTimer);
  tocSearchTimer = setTimeout(renderToc, 120);
});

