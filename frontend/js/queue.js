/* Design v2 Phase E2 — Queue Stack page.
 *
 * Read-only kanban of the cross-novel translate queue: Now translating,
 * Up next, Recent (done + errored). Reuses /api/novels/queue/all (extended
 * to return `recent` in the same commit as this page). Polls every 4s,
 * paused when the tab is hidden, identical cadence to the floating
 * queue-panel pill. No reorder in this phase — Phase E3 wires the
 * queue_position column and ⬆⬇ controls. */

(function () {
  const POLL_MS = 4000;
  const nowEl    = document.getElementById("qs-now");
  const nextEl   = document.getElementById("qs-next");
  const recentEl = document.getElementById("qs-recent");
  const nowCount    = document.getElementById("qs-now-count");
  const nextCount   = document.getElementById("qs-next-count");
  const recentCount = document.getElementById("qs-recent-count");
  const sumEl   = document.getElementById("queue-summary");
  const refreshBtn = document.getElementById("queue-refresh-btn");
  const cancelAllBtn = document.getElementById("queue-cancel-all");
  // Signature of the last rendered snapshot. Re-rendering identical data on
  // every 4s poll is what made the page feel clunky: the columns flickered and
  // lost scroll/hover each tick even when nothing had changed. We now skip the
  // rebuild when the snapshot is byte-identical to the last one.
  let lastSig = null;
  // escapeHtml lives in utils.js (loaded before this script); use the shared one.
  function relTime(ts) {
    if (!ts) return "";
    const iso = ts.includes("T") ? ts : ts.replace(" ", "T") + "Z";
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return "";
    const minsAgo = (Date.now() - t) / 60000;
    if (minsAgo < 1) return "just now";
    if (minsAgo < 60) return `${Math.floor(minsAgo)}m ago`;
    const hours = minsAgo / 60;
    if (hours < 24) return `${Math.floor(hours)}h ago`;
    return `${Math.floor(hours / 24)}d ago`;
  }
  function firstCJK(s) {
    const m = String(s || "").match(/[㐀-鿿]/);
    return m ? m[0] : "·";
  }
  // showToast is window.showToast from utils.js (audit 6.6).
  // confirmDialog lives in frontend/js/utils.js (C7).

  function renderActiveCard(item) {
    // The single "now translating" card. Larger than queued rows so the eye
    // catches what's actually happening. The compact 6-stage strip is a
    // placeholder for the live-pipeline diagram coming in Phase F.
    return `
      <div class="qs-active-card" data-novel="${item.novel_id}" data-ch="${item.chapter_num}">
        <div class="qs-motif">${escapeHtml(firstCJK(item.novel_title))}</div>
        <div class="qs-active-body">
          <div class="qs-active-eyebrow">${escapeHtml(item.novel_title)}</div>
          <div class="qs-active-title">Ch. ${item.chapter_num} · ${escapeHtml(item.title)}</div>
          <div class="qs-stage-bar" aria-hidden="true">
            <span class="qs-stage done"></span>
            <span class="qs-stage done"></span>
            <span class="qs-stage active"></span>
            <span class="qs-stage"></span>
            <span class="qs-stage"></span>
            <span class="qs-stage"></span>
          </div>
          <div class="qs-active-meta">
            <span class="qs-pulse"></span>
            <span>${item.refining ? "Polishing…" : "Translating…"}</span>
            <a class="qs-link" href="/reader?novel=${item.novel_id}&ch=${item.chapter_num}">Open in reader</a>
            ${item.refining ? "" : `<button class="qs-row-btn" data-act="cancel" type="button" title="Cancel this translation">Cancel</button>`}
          </div>
        </div>
      </div>`;
  }

  function renderQueuedRow(item) {
    // A done chapter waiting on the polish lane has no queue flag to drop, so
    // it shows a status instead of the dequeue control.
    const waitingPolish = item.refining;
    return `
      <div class="qs-row" data-novel="${item.novel_id}" data-ch="${item.chapter_num}">
        <div class="qs-row-motif">${escapeHtml(firstCJK(item.novel_title))}</div>
        <div class="qs-row-body">
          <div class="qs-row-title">${escapeHtml(item.novel_title)}</div>
          <div class="qs-row-sub">Ch. ${item.chapter_num} · ${escapeHtml(item.title)}${waitingPolish ? " · waiting to polish" : ""}</div>
        </div>
        <div class="qs-row-actions">
          <a class="qs-row-btn" href="/reader?novel=${item.novel_id}&ch=${item.chapter_num}">Open</a>
          ${waitingPolish ? "" : `<button class="qs-row-btn" data-act="next" type="button" title="Move this chapter to the front of the queue">Next</button>`}
          ${waitingPolish ? "" : `<button class="qs-row-btn" data-act="dequeue" type="button" title="Drop this chapter from the queue">x</button>`}
        </div>
      </div>`;
  }

  function renderRecentRow(item) {
    const isError = item.status === "error";
    // A done translation whose polish pass failed is still readable, but the
    // failure is surfaced with a distinct status + a Retry polish action.
    const polishFailed = !isError && item.refinement_status === "error";
    const refined = !isError && item.refinement_status === "done";
    let statusLabel, statusCls;
    if (isError) { statusLabel = "error"; statusCls = "is-err"; }
    else if (polishFailed) { statusLabel = "polish failed"; statusCls = "is-err"; }
    else if (refined) { statusLabel = "refined"; statusCls = "is-ok"; }
    else { statusLabel = "done"; statusCls = "is-ok"; }
    const errText = isError ? item.error_msg : (polishFailed ? item.refinement_error : "");
    const readLink = `<a class="qs-row-btn" href="/reader?novel=${item.novel_id}&ch=${item.chapter_num}">Read</a>`;
    let actions;
    if (isError) actions = `<button class="qs-row-btn" data-act="retry" type="button">Retry</button>`;
    else if (polishFailed) actions = `<button class="qs-row-btn" data-act="retry-polish" type="button">Retry polish</button>${readLink}`;
    else actions = readLink;
    return `
      <div class="qs-row qs-row-recent${isError || polishFailed ? " is-error" : ""}" data-novel="${item.novel_id}" data-ch="${item.chapter_num}">
        <div class="qs-row-motif">${escapeHtml(firstCJK(item.novel_title))}</div>
        <div class="qs-row-body">
          <div class="qs-row-title">${escapeHtml(item.novel_title)}</div>
          <div class="qs-row-sub">
            Ch. ${item.chapter_num}
            <span class="qs-status ${statusCls}">${statusLabel}</span>
            <span class="qs-when muted">${escapeHtml(relTime(item.translated_at))}</span>
          </div>
          ${errText ? `<div class="qs-row-error">${escapeHtml(errText)}</div>` : ""}
        </div>
        <div class="qs-row-actions">
          ${actions}
        </div>
      </div>`;
  }

  async function dequeueChapter(novelId, chapterNum, btn) {
    if (btn) btn.disabled = true;
    try {
      // Reuse the existing per-chapter cancel — same endpoint the reader
      // uses to drop a chapter from the queue. Cheap, atomic.
      await api.cancelQueueChapter(novelId, chapterNum);
      await refresh();
    } catch (e) {
      showToast(`Drop failed: ${e.message}`, "err");
      if (btn) btn.disabled = false;
    }
  }

  async function cancelActive(novelId, chapterNum, btn) {
    if (btn) { btn.disabled = true; btn.textContent = "Cancelling…"; }
    try {
      // Same endpoint as dequeue; for an in-flight row the backend also
      // interrupts the running worker and resets the chapter.
      await api.cancelQueueChapter(novelId, chapterNum);
      showToast(`Cancelled chapter ${chapterNum}.`, "ok");
      await refresh();
    } catch (e) {
      showToast(`Cancel failed: ${e.message}`, "err");
      if (btn) { btn.disabled = false; btn.textContent = "Cancel"; }
    }
  }

  async function retryChapter(novelId, chapterNum, btn) {
    if (btn) btn.disabled = true;
    try {
      await api.retranslate(novelId, chapterNum);
      await refresh();
    } catch (e) {
      showToast(`Retry failed: ${e.message}`, "err");
      if (btn) btn.disabled = false;
    }
  }

  async function retryPolish(novelId, chapterNum, btn) {
    if (btn) { btn.disabled = true; btn.textContent = "…"; }
    try {
      await api.retryRefinement(novelId, chapterNum);
      await refresh();
    } catch (e) {
      showToast(`Retry polish failed: ${e.message}`, "err");
      if (btn) { btn.disabled = false; btn.textContent = "Retry polish"; }
    }
  }

  function render(snap) {
    // Skip the full rebuild when the snapshot is unchanged since the last poll.
    // This is the core de-clunk: an idle queue no longer flickers every 4s.
    const sig = JSON.stringify(snap);
    if (sig === lastSig) return;
    lastSig = sig;
    const translate = snap.translate || [];
    const recent = snap.recent || [];
    const inFlight = translate.filter(t => t.in_flight);
    const upNext = translate.filter(t => !t.in_flight);

    nowCount.textContent = inFlight.length;
    nextCount.textContent = upNext.length;
    recentCount.textContent = recent.length;
    // Q1: keep each column section's aria-label aligned with its current
    // count so screen-readers hear "Now translating, 2 items" rather than
    // an unlabelled <section>.
    const labelItems = (n) => `${n} ${n === 1 ? "item" : "items"}`;
    document.getElementById("qs-col-now")?.setAttribute("aria-label", `Now translating, ${labelItems(inFlight.length)}`);
    document.getElementById("qs-col-next")?.setAttribute("aria-label", `Up next, ${labelItems(upNext.length)}`);
    document.getElementById("qs-col-recent")?.setAttribute("aria-label", `Recent, ${labelItems(recent.length)}`);

    nowEl.innerHTML = inFlight.length
      ? inFlight.map(renderActiveCard).join("")
      : `<div class="qs-empty muted">Nothing in flight.</div>`;

    nextEl.innerHTML = upNext.length
      ? upNext.map(renderQueuedRow).join("")
      : `<div class="qs-empty muted">Queue is empty.</div>`;

    recentEl.innerHTML = recent.length
      ? recent.map(renderRecentRow).join("")
      : `<div class="qs-empty muted">No recent activity.</div>`;

    sumEl.textContent = `${inFlight.length} translating · ${upNext.length} queued · ${recent.length} recent`;

    // Wire row actions.
    nowEl.querySelectorAll(".qs-active-card[data-novel]").forEach(card => {
      const cx = card.querySelector("[data-act='cancel']");
      if (cx) cx.addEventListener("click", () => cancelActive(
        parseInt(card.dataset.novel, 10),
        parseInt(card.dataset.ch, 10),
        cx,
      ));
    });
    nextEl.querySelectorAll(".qs-row[data-novel]").forEach(row => {
      const dq = row.querySelector("[data-act='dequeue']");
      if (dq) dq.addEventListener("click", () => dequeueChapter(
        parseInt(row.dataset.novel, 10),
        parseInt(row.dataset.ch, 10),
        dq,
      ));
      const nx = row.querySelector("[data-act='next']");
      if (nx) nx.addEventListener("click", async () => {
        nx.disabled = true;
        try {
          await api.translateNext(parseInt(row.dataset.novel, 10), parseInt(row.dataset.ch, 10));
          showToast("Moved to the front of the queue.", "ok");
          // Force a fresh render by clearing lastSig so the re-ordered queue
          // is reflected immediately even if the snapshot bytes change.
          lastSig = null;
          await refresh();
        } catch (e) {
          showToast(`Couldn't prioritize: ${e.message}`, "err");
          nx.disabled = false;
        }
      });
    });
    recentEl.querySelectorAll(".qs-row[data-novel]").forEach(row => {
      const novel = parseInt(row.dataset.novel, 10);
      const ch = parseInt(row.dataset.ch, 10);
      const retry = row.querySelector("[data-act='retry']");
      if (retry) retry.addEventListener("click", () => retryChapter(novel, ch, retry));
      const retryPol = row.querySelector("[data-act='retry-polish']");
      if (retryPol) retryPol.addEventListener("click", () => retryPolish(novel, ch, retryPol));
    });
  }

  async function refresh() {
    try {
      const snap = await api.globalQueue();
      render(snap);
    } catch (e) {
      showToast(`Refresh failed: ${e.message}`, "err");
    }
  }

  refreshBtn?.addEventListener("click", refresh);
  cancelAllBtn?.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "Cancel all queued chapters?",
      body: "<p>This drops every waiting chapter from the queue across all novels. The chapter currently mid-translation finishes on its own.</p>",
      okText: "Cancel all queued",
    });
    if (!ok) return;
    try {
      await api.cancelGlobalQueue();
      showToast("Cleared all queued chapters.", "ok");
      await refresh();
    } catch (e) {
      showToast(`Cancel failed: ${e.message}`, "err");
    }
  });

  // Poll while tab is visible.
  refresh();
  setInterval(() => {
    if (document.visibilityState === "visible") refresh();
  }, POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
})();
