/* Shared cross-page queue panel.
 *
 * Mounts a fixed pill near the bottom-right of every page showing the global
 * queue depth and exposing a popover with the in-flight chapter for each
 * stage plus a Cancel-all button. Polls /api/novels/queue/all every 4 seconds
 * (paused when the tab is hidden). On the reader page the TOC rail already
 * shows per-chapter queue glyphs — this panel covers the cross-novel view
 * the user previously had no way to see.
 *
 * Conventions: no framework, no build step. Inserts into <body> on DOM ready
 * and styles itself from existing CSS tokens via inline classes that base.css
 * defines under `.queue-panel-pill` / `.queue-panel-pop`. */
(function () {
  if (window.__queuePanelMounted) return;
  window.__queuePanelMounted = true;

  // C16: /queue runs its own dedicated 4s poll against the same endpoint.
  // Mounting the pill here would mean two pollers hitting
  // /api/novels/queue/all in lockstep — duplicate traffic plus a visible pill
  // sitting on top of the page that IS the queue view. Short-circuit on that
  // route; the dedicated queue.js owns the UI.
  if (location.pathname === "/queue") return;

  const POLL_MS = 4000;
  let pollTimer = null;
  let lastSnapshot = null;
  let popoverOpen = false;

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2).toLowerCase(), v);
      else node.setAttribute(k, v);
    }
    for (const c of children) {
      if (c == null) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }

  // Pill (always visible when the queue is non-empty) + popover (toggle by
  // clicking the pill). The popover contains the in-flight head of each
  // stage + the cancel-all action. Empty queues hide the pill entirely so
  // it doesn't clutter pages where there's nothing to look at.
  const pill = el("button", {
    class: "queue-panel-pill hidden",
    type: "button",
    "aria-label": "Open global queue panel",
    "aria-haspopup": "dialog",
  });
  const pop = el("div", {
    class: "queue-panel-pop hidden",
    role: "dialog",
    "aria-label": "Global queue",
  });
  // confirmDialog lives in frontend/js/utils.js (C7). It lazy-creates the
  // canonical <dialog id="confirm-dialog">, so we no longer need a bespoke
  // qp-confirm-dialog for pages that don't ship their own.

  function depthLabel(snap) {
    if (!snap) return "";
    const t = snap.translate_depth || 0;
    if (t === 0) return "";
    return `⏳ ${t}`;
  }

  function renderPill(snap) {
    const label = depthLabel(snap);
    if (!label) {
      pill.classList.add("hidden");
      pop.classList.add("hidden");
      popoverOpen = false;
      document.body.classList.remove("has-queue-pill");
      return;
    }
    pill.classList.remove("hidden");
    pill.textContent = label;
    document.body.classList.add("has-queue-pill");
  }

  function renderPop(snap) {
    if (!snap) {
      pop.innerHTML = "<p class='muted'>Loading…</p>";
      return;
    }
    const t = snap.translate || [];
    const linkTo = (item) =>
      `/reader?novel=${item.novel_id}&ch=${item.chapter_num}`;
    const itemLine = (item, stage) => {
      if (!item) return `<li class="qp-empty muted">${stage} queue empty</li>`;
      const tag = item.in_flight ? "in flight" : "waiting";
      return `
        <li>
          <a href="${linkTo(item)}" class="qp-link">${escape(item.novel_title)} · Ch. ${item.chapter_num}</a>
          <span class="muted"> · ${tag}</span>
        </li>`;
    };
    const tList = t.slice(0, 5).map(it => itemLine(it, "translate")).join("");
    pop.innerHTML = `
      <div class="qp-head">
        <strong>Global queue</strong>
        <span class="muted">${t.length} translate</span>
      </div>
      <div class="qp-section">
        <div class="qp-stage">Translate (${t.length})</div>
        <ul class="qp-list">${tList || itemLine(null, "translate")}</ul>
        ${t.length > 5 ? `<div class="muted qp-more">+ ${t.length - 5} more</div>` : ""}
      </div>
      <div class="qp-actions">
        <button type="button" class="btn-ghost" id="qp-cancel-all">Cancel all queued</button>
        <button type="button" class="btn-ghost" id="qp-close">Close</button>
      </div>
    `;
    pop.querySelector("#qp-cancel-all").addEventListener("click", onCancelAll);
    pop.querySelector("#qp-close").addEventListener("click", closePop);
  }

  function escape(s) {
    return String(s || "").replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  async function onCancelAll() {
    const ok = await confirmDialog({
      title: "Cancel queued chapters?",
      body: "<p>This drops every waiting chapter from the queue across all novels. The two chapters currently mid-LLM finish on their own.</p>",
      okText: "Cancel all queued",
      cancelText: "Keep them",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.cancelGlobalQueue();
      await refresh();
    } catch (e) {
      pop.querySelector(".qp-actions").insertAdjacentHTML(
        "afterbegin",
        `<div class="status err" style="margin-bottom:6px;">Cancel failed: ${escape(e.message)}</div>`
      );
    }
  }

  function openPop() {
    popoverOpen = true;
    pop.classList.remove("hidden");
    renderPop(lastSnapshot);
  }
  function closePop() {
    popoverOpen = false;
    pop.classList.add("hidden");
  }

  pill.addEventListener("click", () => {
    if (popoverOpen) closePop();
    else openPop();
  });

  // Click-outside dismissal. Don't bind this until the popover is actually
  // open, otherwise every click anywhere on the page eats a comparison.
  document.addEventListener("click", (e) => {
    if (!popoverOpen) return;
    if (pop.contains(e.target) || pill.contains(e.target)) return;
    closePop();
  });

  async function refresh() {
    try {
      lastSnapshot = await api.globalQueue();
      renderPill(lastSnapshot);
      if (popoverOpen) renderPop(lastSnapshot);
    } catch {
      // Silent. A transient fetch failure shouldn't flash a banner across
      // every page. The next tick retries.
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, POLL_MS);
  }

  function mount() {
    document.body.appendChild(pill);
    document.body.appendChild(pop);
    refresh();
    startPolling();
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") refresh();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
