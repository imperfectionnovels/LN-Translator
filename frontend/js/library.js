const summaryEl = document.getElementById("library-summary");
const stripEl = document.getElementById("continue-strip");
const gridEl = document.getElementById("novel-list");
const chipsEl = document.getElementById("status-chips");
const sortByEl = document.getElementById("sort-by");

let novels = [];
let filterStatus = "all";
let searchQuery = "";
// Counts for the masthead ledger and per-chip counts. Both lists are
// fetched on every load so the ledger reports the *active* shelf even
// when the user is sitting on the Archive tab. The chip counts mirror
// the same source-of-truth — All / Reading / Finished count Active, and
// Archive counts the soft-deleted set.
let archivedCount = 0;
let activeNovelsCache = [];
// Track previous done-chapter counts so we can spot novels that translated
// progress since the last poll and surface a brief pulsing dot to draw the
// eye. Reset (not cleared) on every load.
const prevDoneMap = new Map();
const recentlyAdvancedIds = new Set();
// QA dashboard (Initiative 1): per-novel undismissed-observation counts.
// Loaded once per refresh alongside the novels list. Renderers consult
// this map for the small ⚠ badge on each card.
let _observationsByNovel = {};

function observationsBadgeHtml(novelId) {
  const n = _observationsByNovel[novelId] || 0;
  if (!n) return "";
  const plural = n === 1 ? "" : "s";
  return `<span class="obs-badge" title="${n} translation issue${plural} flagged">⚠ ${n}</span>`;
}

/* ---- Native <dialog> helper for Purge confirmation ----
 * Rename and per-novel-settings used to live here too; both moved to the
 * Novel page in Phase 4. The confirm dialog stays because the Archive
 * tab's Purge action still needs a danger confirmation. */
// `confirmDialog` and `escapeHtml` live in frontend/js/utils.js (C7).

function firstCJK(s) {
  const m = String(s || "").match(/[㐀-鿿]/);
  return m ? m[0] : (String(s || "").trim()[0] || "書");
}
// L3: unified cover palette. coverClass and generatedCoverClass used to
// disagree — coverClass rotated through cover-c1..c8 (eight gradients
// already defined in library.css), generatedCoverClass picked one of
// three jade/indigo/ochre palettes. The same novel could read with two
// different visual identities depending on whether it landed on the
// shelf or the hero card. Same function now feeds both surfaces; the
// .cover-c1..8 gradients apply to .cover-gen and .hero-cover-gen too.
function coverClass(id) {
  return `cover-c${(Number(id) % 8) + 1}`;
}
const generatedCoverClass = coverClass;
function statusOf(n) {
  if (n.total_chapters === 0) return "pending";
  if (n.done_chapters === 0) return "pending";
  if (n.done_chapters >= n.total_chapters) return "finished";
  return "translating";
}
// L4: lastReadInfo was called three times per novel per render
// (effectiveStatus inside filter + reduce + per-chip filter), each doing a
// fresh JSON.parse against localStorage. Library renders the whole shelf on
// every poll; that's 150 parses every few seconds on a 50-novel shelf. We
// snapshot the read-state per load() into _lastReadCache and let
// lastReadInfo prefer it. Outside load() (e.g. on the open-novel callback
// when a user clicks a card) the cache is empty and lookups fall back to
// localStorage as before.
let _lastReadCache = null;
function lastReadInfo(id) {
  if (_lastReadCache && _lastReadCache.has(id)) return _lastReadCache.get(id);
  const raw = localStorage.getItem(`lastRead:${id}`);
  if (!raw) return null;
  try { return JSON.parse(raw); } catch { return null; }
}
// Read-state normalizer. Prefers the durable DB position carried on the novel
// object (n.last_read_chapter_num / n.last_read_at) so resume + the "Continue
// reading" strip survive a WebView2 storage wipe; falls back to the
// localStorage breadcrumb for users whose DB column hasn't been backfilled
// yet. Returns the same { ch, ts, lastLine } shape lastReadInfo does. The DB
// timestamp is SQLite's UTC "YYYY-MM-DD HH:MM:SS" — normalize to a ms epoch so
// relTime() and the last_read sort stay consistent across both sources.
function readInfoFor(n) {
  if (n && n.last_read_chapter_num != null) {
    const at = n.last_read_at;
    const ts = at ? Date.parse(String(at).replace(" ", "T") + "Z") : 0;
    return {
      ch: n.last_read_chapter_num,
      ts: Number.isFinite(ts) ? ts : 0,
      // Keep the prose snippet from the local cache when we have it; the DB
      // intentionally doesn't store it (volatile, per-chapter).
      lastLine: (lastReadInfo(n.id) || {}).lastLine || null,
    };
  }
  return lastReadInfo(n.id);
}
function relTime(ts) {
  if (!ts) return null;
  const d = (Date.now() - ts) / 86400000;
  if (d < 0.04) return "just now";
  if (d < 1) return "today";
  if (d < 2) return "yesterday";
  if (d < 7) return `${Math.floor(d)} days ago`;
  if (d < 30) return `${Math.floor(d / 7)} wk ago`;
  return `${Math.floor(d / 30)} mo ago`;
}

function effectiveStatus(n) {
  // The chip filter is two-axis: "reading" means user has opened it AND it
  // isn't finished. A finished novel the user has read still belongs under
  // "Finished" (otherwise the chip looks empty); an unstarted one still falls
  // through to its translation status.
  const last = readInfoFor(n);
  const s = statusOf(n);
  if (last && last.ts && s !== "finished") return "reading";
  return s;
}

const lastSyncEl = document.getElementById("last-sync");
const pollErrorEl = document.getElementById("poll-error");
function fmtClock(ts) {
  const d = new Date(ts);
  const pad = n => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
// Providers cache for the Ledger view's Provider/Refinement columns. Lazily
// populated alongside the novels load; the Ledger gracefully falls back to
// showing the raw id (or "—") if a lookup misses, so a slow/failed providers
// fetch never blocks the library.
let _providersCache = null;
function providerNameById(id) {
  if (!id || !_providersCache) return null;
  const p = _providersCache.find(x => x.id === id);
  return p ? p.name : null;
}

async function load() {
  try {
    // Both lists ship on every load: the active set drives the ledger and
    // chip counts; the archived set drives the Archive chip count and (when
    // the user is on the Archive tab) the visible grid. Two cheap GETs
    // beat any cleverer scheme — DB is local and the rows are small.
    const [activeList, archivedList, obsSummary, providersList] = await Promise.all([
      api.novels(),
      api.novels({ archived: true }).catch(() => []),
      api.observationsLibrarySummary().catch(() => ({})),
      api.providers().catch(() => []),
    ]);
    activeNovelsCache = activeList;
    archivedCount = archivedList.length;
    const fresh = filterStatus === "archived" ? archivedList : activeList;
    // Build the per-novel last-read snapshot once for the render that's
    // about to follow; lastReadInfo / effectiveStatus pick it up via
    // _lastReadCache rather than re-parsing localStorage per call.
    _lastReadCache = new Map();
    for (const n of fresh) {
      const raw = localStorage.getItem(`lastRead:${n.id}`);
      if (!raw) { _lastReadCache.set(n.id, null); continue; }
      try { _lastReadCache.set(n.id, JSON.parse(raw)); }
      catch { _lastReadCache.set(n.id, null); }
    }
    _observationsByNovel = obsSummary || {};
    _providersCache = providersList || [];
    // Compute which novels translated more chapters since the last poll.
    // Skip on first load (empty prevDoneMap) so we don't pulse everything.
    recentlyAdvancedIds.clear();
    if (prevDoneMap.size > 0) {
      for (const n of fresh) {
        const prev = prevDoneMap.get(n.id);
        if (prev != null && (n.done_chapters || 0) > prev) recentlyAdvancedIds.add(n.id);
      }
    }
    for (const n of fresh) prevDoneMap.set(n.id, n.done_chapters || 0);
    novels = fresh;
    renderSummary();
    renderContinue();
    renderGrid();
    if (lastSyncEl) lastSyncEl.textContent = `Synced ${fmtClock(Date.now())}`;
    if (pollErrorEl) pollErrorEl.classList.add("hidden");
  } catch (e) {
    // Preserve the existing grid (don't blank it on poll failure) and surface
    // the problem as an inline chip in the toolbar so the user knows the
    // page might be showing stale state.
    if (pollErrorEl) {
      pollErrorEl.textContent = `Sync failed · ${e.message}`;
      pollErrorEl.classList.remove("hidden");
    }
    // First-paint failure: there's no grid to preserve, so write into it.
    if (gridEl.querySelector(".skeleton-card")) {
      gridEl.classList.remove("skeleton-grid");
      gridEl.innerHTML = `<p class="status err">Failed to load: ${escapeHtml(e.message)}</p>`;
    }
  }
}

function renderSummary() {
  // The ledger + chip counts always describe the *active* shelf regardless
  // of which tab the user is on. The archive count is the one exception
  // and comes from its own cached fetch.
  const activeNovels = activeNovelsCache;
  const total = activeNovels.length;
  const totalCh = activeNovels.reduce((s, n) => s + (n.done_chapters || 0), 0);
  const readingCount = activeNovels.filter(n => effectiveStatus(n) === "reading").length;
  const finishedCount = activeNovels.filter(n => effectiveStatus(n) === "finished").length;

  const set = (key, value) => {
    const el = summaryEl.querySelector(`[data-cell="${key}"]`);
    if (el) el.textContent = value;
  };
  set("novels", total.toLocaleString());
  set("chapters", totalCh.toLocaleString());
  set("reading", readingCount.toLocaleString());
  set("archive", archivedCount.toLocaleString());

  // Per-chip counts — appended after each label by the HTML, just update
  // the inner numerals. "All" counts Active novels only (Archive is the
  // separate set-aside view, never folded into All).
  const setChip = (key, value) => {
    const el = chipsEl.querySelector(`[data-count="${key}"]`);
    if (el) el.textContent = value.toLocaleString();
  };
  setChip("all", total);
  setChip("reading", readingCount);
  setChip("finished", finishedCount);
  setChip("archived", archivedCount);
}

function renderContinue() {
  // Continue strip now renders a single rich hero card for the most-recent
  // novel only. The previous 2-compact-card secondary column was visual
  // noise the design proposal called out — the shelf below already surfaces
  // the rest of the user's recent reads.
  //
  // Archive tab hides the strip entirely: a "continue reading" pitch is
  // misleading when the user is browsing what they've set aside.
  if (filterStatus === "archived") {
    stripEl.style.display = "none";
    stripEl.innerHTML = "";
    return;
  }
  const recent = novels
    .map(n => ({ n, last: readInfoFor(n) }))
    .filter(x => x.last && x.last.ts)
    .sort((a, b) => b.last.ts - a.last.ts)
    .slice(0, 1);
  stripEl.style.display = "";
  if (recent.length === 0) {
    stripEl.innerHTML = `
      <div class="continue-empty">
        Open any novel. Your spot will appear here next time.
      </div>`;
    return;
  }
  const { n, last } = recent[0];
  const pct = n.total_chapters ? Math.round((last.ch / n.total_chapters) * 100) : 0;
  const lastLine = last.lastLine
    ? `<p class="last-line">${escapeHtml(last.lastLine)}</p>` : "";
  // Cover block — pull from the same /api/novels/{id}/cover endpoint as
  // the shelf cards. When no scraped/uploaded cover exists, fall back to
  // the same `.cover-gen` palette set the shelf uses so the hero card
  // shares the shelf's visual vocabulary.
  const han = firstCJK(n.title);
  let coverHtml;
  if (n.cover_image_path) {
    const url = `/api/novels/${n.id}/cover?t=${encodeURIComponent(n.cover_image_path)}`;
    coverHtml = `<div class="hero-cover hero-cover-art" style="background-image: url(${url})"></div>`;
  } else {
    coverHtml = `<div class="hero-cover hero-cover-gen ${generatedCoverClass(n.id)}"><span class="han">${escapeHtml(han)}</span></div>`;
  }
  stripEl.innerHTML = `
    <div class="continue-card primary hero" data-novel="${n.id}" data-ch="${last.ch}">
      ${coverHtml}
      <div class="hero-body">
        <div class="continue-eyebrow">Continue reading</div>
        <h3 class="continue-title">${escapeHtml(n.title)}</h3>
        <div class="continue-meta">
          <span>Ch ${last.ch} of ${n.total_chapters || "…"}</span>
          <span>${pct}%</span>
          <span>${relTime(last.ts)}</span>
        </div>
        <div class="mini-prog"><div class="fill" style="width:${pct}%"></div></div>
        ${lastLine}
        <div class="continue-actions">
          <button class="btn-resume">Resume →</button>
        </div>
      </div>
    </div>`;
  stripEl.querySelectorAll(".continue-card").forEach(card => {
    card.addEventListener("click", () => {
      const id = card.dataset.novel;
      const ch = card.dataset.ch;
      location.href = `/reader?novel=${id}&ch=${ch}`;
    });
  });
}

/* Library now always renders the cover-forward layout (Phase 3 of the
 * wireframes redesign dropped the visible view-mode toggle). The
 * renderSpinesView / renderLedgerView functions below are intentionally
 * kept dormant so a future restore is one HTML edit + one renderGrid
 * branch, not a re-archeology of the row card. */

// Per-book palettes [deep, mid, accent] — a rotating mix of ink, cinnabar,
// gold, jade, plum and slate so a shelf reads with rhythm, not monotony.
const COVER_PALETTES = [
  ["#1a2630", "#3a4a5e", "#5fb8a4"],
  ["#1a1414", "#3a2230", "#c8423a"],
  ["#2a1208", "#5a3520", "#d0a040"],
  ["#10242a", "#244a52", "#5fb8a4"],
  ["#221218", "#4a2838", "#c8423a"],
  ["#181c20", "#2a3240", "#7a92b4"],
  ["#0e1410", "#2a3a30", "#a8a050"],
];
function paletteFor(id) { return COVER_PALETTES[Number(id) % COVER_PALETTES.length]; }
const SPINE_HEIGHTS = [244, 286, 260, 300, 250, 274, 238];

// A short vertical run of the title for spines (CJK if present, else Latin).
function spineLabel(title) {
  const t = String(title || "").trim();
  const cjk = t.match(/[㐀-鿿]+/g);
  if (cjk) return cjk.join("").slice(0, 6);
  return t.slice(0, 10);
}

// Shared per-novel metrics used by every view.
function novelStats(n) {
  const pct = n.total_chapters ? Math.round((n.done_chapters / n.total_chapters) * 100) : 0;
  const s = statusOf(n);
  const last = readInfoFor(n);
  const tq = n.translate_queue || 0;
  const queueTotal = n.queue_chapters != null ? n.queue_chapters : tq;
  const inFlightLabel = n.translating_now > 0 ? "translating"
    : queueTotal > 0 ? "queued" : "";
  let badge = "";
  if (last && last.ch) badge = `<span class="badge-mini">Ch. ${last.ch}</span>`;
  else if (s === "finished") badge = `<span class="badge-mini done">Finished</span>`;
  else if (s === "pending") badge = `<span class="badge-mini warn">Raw</span>`;
  const queueBadge = queueTotal > 0
    ? `<span class="badge-mini queue" title="${tq} queued for translation">⏳ ${queueTotal} ${inFlightLabel}</span>`
    : "";
  const isActive = n.translating_now > 0 || recentlyAdvancedIds.has(n.id);
  return { pct, s, last, tq, queueTotal, badge, queueBadge, isActive };
}

function sortedVisibleNovels() {
  let list = novels.slice();
  // F11 (2026-05-25): Archive tab — list already came from
  // ?archived=1 so just bypass effectiveStatus filtering.
  if (filterStatus !== "all" && filterStatus !== "archived") {
    list = list.filter(n => effectiveStatus(n) === filterStatus);
  }
  if (searchQuery) {
    const q = searchQuery.toLowerCase();
    list = list.filter(n => (n.title || "").toLowerCase().includes(q));
  }
  const by = sortByEl.value;
  list.sort((a, b) => {
    if (by === "title") return a.title.localeCompare(b.title);
    if (by === "progress") {
      const pa = a.total_chapters ? a.done_chapters / a.total_chapters : 0;
      const pb = b.total_chapters ? b.done_chapters / b.total_chapters : 0;
      return pb - pa;
    }
    if (by === "last_read") {
      const la = (readInfoFor(a) || {}).ts || 0;
      const lb = (readInfoFor(b) || {}).ts || 0;
      return lb - la;
    }
    return b.id - a.id;
  });
  return list;
}

function _coverCardHtml(n) {
  // Cover-forward card design per the wireframes redesign. Horizontal
  // layout: 116px cover (2:3) on the left, meta column on the right.
  // Status moves to a thin top-edge ribbon (no more corner seal that
  // covers the title). Source pip on the cover names where the image
  // came from ('scraped' for URL imports, 'epub' for EPUB), pip absent
  // when the source is NULL (paste, manual upload pre-redesign, etc.).
  const st = novelStats(n);
  const status = effectiveStatus(n);
  const ribbonClass = {
    translating: "translating",
    reading: "reading",
    finished: "finished",
    pending: "pending",
  }[status] || "pending";
  const statusLabel = {
    translating: "translating now",
    reading: "currently reading",
    finished: "complete",
    pending: "pending",
  }[status] || "pending";
  const pulse = st.isActive ? `<span class="pulse-dot" title="Worker active" aria-label="Active"></span>` : "";
  const han = firstCJK(n.title);

  // Cover area — uploaded image or thread-bound fallback. The cache-bust
  // query string keys on cover_image_path so a re-upload bypasses the
  // browser cache without a hard refresh.
  const hasCover = !!n.cover_image_path;
  let coverHtml;
  if (hasCover) {
    const url = `/api/novels/${n.id}/cover?t=${encodeURIComponent(n.cover_image_path)}`;
    // Source pip names the ingestion path so the user can tell at a
    // glance which covers came along with the import vs. were uploaded
    // manually. NULL source → no pip.
    const pipText = n.cover_source === "url" ? "scraped"
                  : n.cover_source === "epub" ? "epub"
                  : null;
    const pip = pipText
      ? `<div class="cover-pip">${pipText}</div>`
      : "";
    coverHtml = `
      <div class="cover-art" style="background-image: url(${url})">
        <div class="stitch top" aria-hidden="true"></div>
        <div class="stitch bot" aria-hidden="true"></div>
        ${pip}
      </div>`;
  } else {
    // Generated cover — hash-tinted gradient + single Han glyph. Same
    // palette set the hero card uses, so the shelf reads as one family.
    // Never a blank rectangle, never the muted vellum-fallback again.
    coverHtml = `
      <div class="cover-gen ${generatedCoverClass(n.id)}">
        <div class="stitch top" aria-hidden="true"></div>
        <div class="stitch bot" aria-hidden="true"></div>
        <span class="han-glyph">${escapeHtml(han)}</span>
      </div>`;
  }

  // Author line — first preference is the user-entered author; falls
  // back to the original Chinese title in italics if no author but an
  // original title is set; suppressed entirely when neither.
  const authorBits = [];
  if (n.author) authorBits.push(`by ${escapeHtml(n.author)}`);
  if (n.original_title && !n.author) authorBits.push(`<em>${escapeHtml(n.original_title)}</em>`);
  const authorLine = authorBits.length
    ? `<div class="meta-author">${authorBits.join("")}</div>`
    : "";

  const chapterLine = `<div class="meta-chapters">${n.total_chapters} chapter${n.total_chapters === 1 ? "" : "s"}${n.done_chapters > 0 ? ` · ${st.pct}% done` : ""}</div>`;
  // Progress thread — dashed rule + a colored bar on top. Uses the same
  // status color as the top ribbon so the card reads consistently.
  const progressBar = `
    <div class="meta-progress" title="Translation progress: ${n.done_chapters}/${n.total_chapters}">
      <div class="thread-dash" aria-hidden="true"></div>
      <div class="thread-fill ${ribbonClass}" style="width:${st.pct}%"></div>
    </div>`;
  const queueLine = st.queueBadge
    ? `<div class="meta-queue">${st.queueBadge}</div>`
    : "";
  // Resumable import badge — surfaces in-progress and paused crawls so a
  // 60-minute scrape doesn't look like a dead novel sitting in the library.
  // import_status=NULL means "atomic-create / done" (most novels); we don't
  // render anything for those.
  const importBadge = _importBadgeHtml(n);
  const observations = observationsBadgeHtml(n.id);

  return `
    <div class="book-card" data-id="${n.id}">
      <div class="status-ribbon ${ribbonClass}" aria-hidden="true"></div>
      <div class="card-body" data-go>
        ${coverHtml}
        <div class="meta">
          <div class="meta-title">${escapeHtml(n.title)}${pulse}</div>
          <div class="meta-han">${escapeHtml(han)}</div>
          ${authorLine}
          <div class="meta-rule" aria-hidden="true"></div>
          <div class="meta-status">
            <span class="status-dot ${ribbonClass}"></span>
            <span class="status-label">${statusLabel}</span>
          </div>
          ${chapterLine}
          ${progressBar}
          ${queueLine}
          ${importBadge}
          ${observations ? `<div class="meta-observations">${observations}</div>` : ""}
          <div class="meta-spacer"></div>
          <div class="meta-cta">open ›</div>
        </div>
      </div>
      <div class="book-actions">${_coverCardActionsHtml(n)}</div>
    </div>`;
}

function _importBadgeHtml(n) {
  // n.import_status: NULL (legacy/atomic-create — treated as done) |
  // 'in_progress' | 'paused' | 'done' | 'cancelled'.
  // We only render for in_progress + paused; done means the runner
  // finished and the regular progress thread tells the story.
  if (!n.import_status || n.import_status === "done") return "";
  const total = n.total_chapters || 0;
  const pending = n.import_pending_chapters || 0;
  // fetched = total - pending for recipe scrapes (skeleton-based).
  // For bulk/EPUB paused novels (no skeleton URLs), pending is 0 and
  // total is whatever was already committed; we show "Paused at N".
  const fetched = Math.max(total - pending, 0);
  if (n.import_status === "in_progress") {
    return `
      <div class="meta-import is-fetching" data-import-status="in_progress">
        <span class="ind-dot"></span>
        <span class="ind-label">Importing <strong>${fetched}</strong> / ${total}</span>
        <button class="ind-btn" data-import-cancel title="Pause the import; keeps what's already been fetched">Cancel</button>
      </div>`;
  }
  // 'paused' — resume only when there's pending recipe work.
  const canResume = pending > 0;
  const resumeBtn = canResume
    ? `<button class="ind-btn primary" data-import-resume>Resume</button>`
    : "";
  const label = canResume
    ? `Paused at <strong>${fetched}</strong> / ${total}`
    : `Paused · ${fetched} chapter${fetched === 1 ? "" : "s"} kept`;
  return `
    <div class="meta-import is-paused" data-import-status="paused">
      <span class="ind-dot"></span>
      <span class="ind-label">${label}</span>
      ${resumeBtn}
    </div>`;
}


function _coverCardActionsHtml(n) {
  // F11: Archive cards show Restore + Purge instead of the usual stack.
  // Active novels now show a short row of jump-points only — Rename,
  // Settings, and Archive moved to /novel?id=N per the Phase 4 design
  // consolidation. The Novel page link is the bridge to all per-novel
  // configuration.
  if (n.deleted_at) {
    return `
        <button class="row-btn restore">Restore</button>
        <button class="row-btn danger purge" title="Permanently delete. Cannot be undone">Purge</button>
    `;
  }
  return `
        <a class="row-btn primary" href="/reader?novel=${n.id}&ch=${(_lastReadFor(n)) || n.first_chapter_num || 1}">Open</a>
        <a class="row-btn" href="/novel?id=${n.id}">Novel page</a>
        <a class="row-btn" href="/?novel=${n.id}" title="Append more chapters to this novel">＋ Add chapters</a>
        <a class="row-btn" href="/glossary?novel=${n.id}">Glossary</a>
  `;
}

function _lastReadFor(n) {
  const st = novelStats(n);
  return st.last && st.last.ch;
}

function _groupNovelsBySeries(list) {
  // Build a Map preserving insertion order so series appear in the order
  // their first novel was sorted. Standalone novels (no series_name) all
  // land in a "" key and render under a single "Standalone" heading at
  // the bottom — gives series prominence without exiling solo novels.
  const groups = new Map();
  for (const n of list) {
    const key = (n.series_name || "").trim();
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(n);
  }
  // Within each series, sort by series_index ASC with NULL pushed last so
  // unsequenced rows sit at the end of their group.
  for (const arr of groups.values()) {
    arr.sort((a, b) => {
      const ai = a.series_index;
      const bi = b.series_index;
      if (ai == null && bi == null) return 0;
      if (ai == null) return 1;
      if (bi == null) return -1;
      return ai - bi;
    });
  }
  return groups;
}

function renderCoversView(list) {
  gridEl.className = "shelf-grid";
  // Two-level grouping per the design proposal:
  //   outer = reading state (Reading / Finished), inner = series.
  // When a specific filter is active (Reading / Finished / Archive), the
  // outer header is suppressed because the chip already names the state.
  // Archive uses a single bucket — it doesn't split Reading/Finished.
  const renderSeriesGrouped = (arr) => {
    const groups = _groupNovelsBySeries(arr);
    const seriesGroups = [...groups.entries()].filter(([k]) => k !== "");
    const standalone = groups.get("") || [];
    if (seriesGroups.length === 0) {
      return standalone.map(_coverCardHtml).join("");
    }
    const parts = [];
    for (const [name, items] of seriesGroups) {
      parts.push(`<div class="series-group-head">${escapeHtml(name)}</div>`);
      parts.push(items.map(_coverCardHtml).join(""));
    }
    if (standalone.length) {
      parts.push(`<div class="series-group-head">Standalone</div>`);
      parts.push(standalone.map(_coverCardHtml).join(""));
    }
    return parts.join("");
  };
  const stateHeader = (han, title, count, sublabel) => `
    <div class="shelf-group">
      <span class="han">${han}</span>
      <span class="t">${title}</span>
      <span class="c">${count} ${count === 1 ? "novel" : "novels"}${sublabel ? " · " + sublabel : ""}</span>
    </div>`;
  // Non-"All" filters skip the outer state header (the chip is the header).
  // Archive also lands here — single flat bucket of soft-deleted novels.
  if (filterStatus !== "all") {
    gridEl.innerHTML = renderSeriesGrouped(list);
    return;
  }
  // "All" view: bucket into Reading (anything not finished) and Finished.
  // Reading sits up top; the order matches the chip order.
  const reading = list.filter(n => effectiveStatus(n) !== "finished");
  const finished = list.filter(n => effectiveStatus(n) === "finished");
  const newCount = reading.filter(n => recentlyAdvancedIds.has(n.id)).length;
  const parts = [];
  if (reading.length) {
    parts.push(stateHeader("讀", "Reading", reading.length,
      newCount > 0 ? `${newCount} with new chapters` : null));
    parts.push(renderSeriesGrouped(reading));
  }
  if (finished.length) {
    parts.push(stateHeader("畢", "Finished", finished.length, null));
    parts.push(renderSeriesGrouped(finished));
  }
  gridEl.innerHTML = parts.join("");
}

function renderSpinesView(list) {
  gridEl.className = "lib-spines";
  gridEl.innerHTML = list.map((n, i) => {
    const st = novelStats(n);
    const [c1, c2, accent] = paletteFor(n.id);
    const h = SPINE_HEIGHTS[i % SPINE_HEIGHTS.length];
    const pulse = st.isActive ? `<span class="pulse-dot" aria-label="Active"></span>` : "";
    return `
      <div class="lib-spine-book" data-id="${n.id}" data-go title="${escapeHtml(n.title)}"
           style="--bh:${h}px;--c1:${c1};--c2:${c2};--c-accent:${accent};--pct:${st.pct}%">
        <div class="spine-face"></div>
        <div class="seal-cap">${escapeHtml(firstCJK(n.title))}${pulse}</div>
        <div class="spine-title">${escapeHtml(spineLabel(n.title))}</div>
        <div class="spine-num">vol. ${String(n.id).padStart(2, "0")}</div>
        <div class="ring"></div>
      </div>`;
  }).join("");
}

function renderLedgerView(list) {
  // Design v2 Phase C — Workbench: extend the row with Provider and
  // Last-read columns so the heavy-reader can scan translator stack +
  // recency at a glance. Falls back to "—" when a field is null so a
  // novel without configured providers still renders cleanly.
  gridEl.className = "lib-ledger";
  const head = `<div class="lib-row head">
    <div></div><div>Title</div><div>Progress</div><div>Provider</div><div>Last read</div><div>Status</div><div></div>
  </div>`;
  gridEl.innerHTML = head + list.map(n => {
    const st = novelStats(n);
    const pulse = st.isActive ? `<span class="pulse-dot" aria-label="Active"></span>` : "";
    const tname = providerNameById(n.translator_provider_id);
    const rname = providerNameById(n.refinement_provider_id);
    const providerCell = tname
      ? `<span class="prov-primary">${escapeHtml(tname)}</span>${
          rname ? `<span class="prov-refine" title="Refinement pass">+ ${escapeHtml(rname)}</span>` : ""
        }`
      : `<span class="prov-default muted">default</span>`;
    const last = st.last;
    const lastReadCell = last && last.ts
      ? `<span class="last-read-ch">Ch ${last.ch}</span><span class="last-read-when muted">${escapeHtml(relTime(last.ts) || "")}</span>`
      : `<span class="muted">…</span>`;
    const rowCls = st.isActive ? "lib-row is-translating" : "lib-row";
    return `
      <div class="${rowCls}" data-id="${n.id}">
        <div class="han">${escapeHtml(firstCJK(n.title))}</div>
        <div class="l-title" data-go>
          ${escapeHtml(n.title)}${pulse}
          <small>${n.total_chapters} chapter${n.total_chapters === 1 ? "" : "s"} · vol. ${String(n.id).padStart(2, "0")}</small>
        </div>
        <div class="pcell">
          <div class="num"><span>${n.done_chapters} / ${n.total_chapters}</span><span>${st.pct}%</span></div>
          <div class="pbar"><div class="f" style="width:${st.pct}%"></div></div>
        </div>
        <div class="l-provider">${providerCell}</div>
        <div class="l-last-read">${lastReadCell}</div>
        <div class="l-status">${st.badge}${st.queueBadge}${observationsBadgeHtml(n.id)}</div>
        <div class="l-actions">
          <a class="row-btn primary" href="/reader?novel=${n.id}&ch=${(st.last && st.last.ch) || n.first_chapter_num || 1}">Open</a>
          <a class="row-btn" href="/novel?id=${n.id}">Novel page</a>
          <a class="row-btn" href="/?novel=${n.id}" title="Append more chapters to this novel">＋ Add chapters</a>
          <a class="row-btn" href="/glossary?novel=${n.id}">Glossary</a>
          <button class="row-btn rename">Rename</button>
          <button class="row-btn settings">Settings</button>
          <button class="row-btn danger del">Delete</button>
        </div>
      </div>`;
  }).join("");
}

function wireCardActions() {
  // [data-go] anywhere → open the reader at the last-read chapter.
  gridEl.querySelectorAll("[data-id]").forEach(card => {
    const id = card.dataset.id;
    // [data-go] may sit on descendants (covers / ledger) or the card itself
    // (a whole spine book is one click target).
    const goTargets = Array.prototype.slice.call(card.querySelectorAll("[data-go]"));
    if (card.hasAttribute("data-go")) goTargets.push(card);
    goTargets.forEach(el => {
      el.addEventListener("click", () => {
        // last-read → novel's first_chapter_num (handles partial imports
        // starting at 第296章) → 1 fallback. Without this fallback, opening
        // a brand-new partial-import novel from the library lands at
        // /reader?ch=1 and 404s.
        const novel = novels.find(x => x.id == id);
        const last = readInfoFor(novel);
        const firstCh = novel && novel.first_chapter_num;
        const ch = (last && last.ch) || firstCh || 1;
        location.href = `/reader?novel=${id}&ch=${ch}`;
      });
    });
    // F11: Restore + Purge actions on archived cards.
    const restoreBtn = card.querySelector(".restore");
    if (restoreBtn) restoreBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      restoreBtn.disabled = true;
      try { await api.restoreNovel(id); await load(); }
      catch (err) {
        restoreBtn.disabled = false;
        await confirmDialog({ title: "Restore failed", body: `<p>${escapeHtml(err.message)}</p>`, okText: "OK", cancelText: "" });
      }
    });
    // Resumable-import controls.
    const cancelImportBtn = card.querySelector("[data-import-cancel]");
    if (cancelImportBtn) cancelImportBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      cancelImportBtn.disabled = true;
      try {
        const res = await fetch(`/api/imports/${id}/cancel`, { method: "POST" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        await load();
      } catch (err) {
        cancelImportBtn.disabled = false;
        await confirmDialog({ title: "Cancel failed", body: `<p>${escapeHtml(err.message)}</p>`, okText: "OK", cancelText: "" });
      }
    });
    const resumeImportBtn = card.querySelector("[data-import-resume]");
    if (resumeImportBtn) resumeImportBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      resumeImportBtn.disabled = true;
      try {
        const res = await fetch(`/api/imports/${id}/resume`, { method: "POST" });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `HTTP ${res.status}`);
        }
        await load();
      } catch (err) {
        resumeImportBtn.disabled = false;
        await confirmDialog({ title: "Resume failed", body: `<p>${escapeHtml(err.message)}</p>`, okText: "OK", cancelText: "" });
      }
    });
    const purgeBtn = card.querySelector(".purge");
    if (purgeBtn) purgeBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const n = novels.find(x => x.id == id);
      const ok = await confirmDialog({
        title: "Purge permanently?",
        body:
          `<p>This permanently deletes <strong>${escapeHtml(n.title)}</strong> ` +
          `and all its chapters, glossary entries, bookmarks, and snapshots.</p>` +
          `<p class="muted">This action cannot be undone.</p>`,
        okText: "Purge permanently",
        danger: true,
      });
      if (!ok) return;
      purgeBtn.disabled = true;
      try { await api.purgeNovel(id); await load(); }
      catch (err) {
        purgeBtn.disabled = false;
        await confirmDialog({ title: "Purge failed", body: `<p>${escapeHtml(err.message)}</p>`, okText: "OK", cancelText: "" });
      }
    });
  });
}

function renderGrid() {
  // Wireframes redesign: the library always renders cover-forward cards.
  // renderSpinesView / renderLedgerView remain defined above as dormant
  // helpers in case the toggle ever comes back, but the visible switch
  // (and the libView state machinery) is gone.
  const list = sortedVisibleNovels();
  gridEl.setAttribute("aria-busy", "false");
  if (list.length === 0) {
    gridEl.className = "";
    gridEl.innerHTML = _emptyStateHtml();
    return;
  }
  renderCoversView(list);
  wireCardActions();
}

function _emptyStateHtml() {
  // Two cases:
  //   1. Truly empty shelf (no active and no archived novels) → full
  //      onboarding layout: 4 import paths + sample shelf.
  //   2. Empty under a specific filter (Reading / Finished / Archive)
  //      with novels elsewhere → quiet "nothing here" placeholder so
  //      we don't pitch onboarding to an existing user.
  const totalNovels = activeNovelsCache.length + archivedCount;
  if (filterStatus !== "all" && filterStatus !== "archived" && totalNovels > 0) {
    return `<p class="muted">No novels in this view. Try another tab.</p>`;
  }
  if (filterStatus === "archived" && archivedCount === 0 && activeNovelsCache.length > 0) {
    return `<p class="muted">Nothing in the archive. Soft-deleted novels appear here for 30 days.</p>`;
  }
  // True zero state — full onboarding layout.
  return `
    <div class="onboard">
      <div class="left">
        <div class="eyebrow"><span class="h">入</span><span>First import</span></div>
        <h2>Bring a novel <em>onto the shelf.</em></h2>
        <p class="onboard-sub">Four ways in. Pick whichever matches the file you have. The parser does the rest, and pulls the cover when it can.</p>
        <div class="import-paths">
          <a class="path" href="/" data-mode="url">
            <span class="han">網</span>
            <div>
              <div class="ti">From a URL</div>
              <div class="sub">scrapes cover &amp; chapters</div>
            </div>
          </a>
          <a class="path" href="/" data-mode="epub">
            <span class="han jade">書</span>
            <div>
              <div class="ti">EPUB file</div>
              <div class="sub">drop or browse</div>
            </div>
          </a>
          <a class="path" href="/" data-mode="paste">
            <span class="han">字</span>
            <div>
              <div class="ti">Paste characters</div>
              <div class="sub">parser finds breaks</div>
            </div>
          </a>
          <a class="path" href="/" data-mode="folder">
            <span class="han jade">夾</span>
            <div>
              <div class="ti">Open a folder</div>
              <div class="sub">.txt batch import</div>
            </div>
          </a>
        </div>
        <div class="or-row"><span>or</span></div>
        <a class="btn-primary" href="/" style="align-self: start;">＋ Import a novel</a>
      </div>
      <div class="right">
        <div class="lbl"><span class="h">例</span><span>Sample shelf · how it will look</span></div>
        <div class="book-stack">
          <div class="sample-book"><div class="sample-cover cover-gen jade"><span class="han-glyph">凡</span></div><div class="sample-meta"><div class="sample-ti">A Mortal's Journey</div><div class="sample-zh">凡人修仙传</div></div></div>
          <div class="sample-book"><div class="sample-cover cover-gen indigo"><span class="han-glyph">劍</span></div><div class="sample-meta"><div class="sample-ti">Sword Comes</div><div class="sample-zh">剑来</div></div></div>
          <div class="sample-book"><div class="sample-cover cover-gen ochre"><span class="han-glyph">朝</span></div><div class="sample-meta"><div class="sample-ti">Morning Court</div><div class="sample-zh">早朝</div></div></div>
          <div class="sample-book"><div class="sample-cover cover-gen indigo"><span class="han-glyph">夜</span></div><div class="sample-meta"><div class="sample-ti">Capital Night</div><div class="sample-zh">夜入皇城</div></div></div>
          <div class="sample-book"><div class="sample-cover cover-gen jade"><span class="han-glyph">道</span></div><div class="sample-meta"><div class="sample-ti">Way of Choices</div><div class="sample-zh">择天记</div></div></div>
          <div class="sample-book"><div class="sample-cover cover-gen ochre"><span class="han-glyph">霜</span></div><div class="sample-meta"><div class="sample-ti">Lord of Frost</div><div class="sample-zh">霜君</div></div></div>
        </div>
        <div class="tip">
          <strong>Covers fall back gracefully.</strong> URL scrapes and EPUBs pull real art; pasted text and bulk folders land with a typographic Han-glyph cover tinted from the title hash. Never a blank rectangle.
        </div>
      </div>
    </div>`;
}

chipsEl.querySelectorAll("button").forEach(b => {
  b.addEventListener("click", () => {
    chipsEl.querySelectorAll("button").forEach(x => x.classList.remove("on"));
    b.classList.add("on");
    const wasArchive = filterStatus === "archived";
    filterStatus = b.dataset.status;
    // F11: switching to/from Archive re-fetches from a different endpoint.
    if ((filterStatus === "archived") !== wasArchive) {
      load().then(() => renderGrid()).catch(() => renderGrid());
    } else {
      renderGrid();
    }
  });
});
sortByEl.addEventListener("change", renderGrid);

const searchInput = document.getElementById("library-search");
searchInput?.addEventListener("input", () => {
  searchQuery = (searchInput.value || "").trim();
  renderGrid();
});

load();
// Skip background polling when the tab is hidden so we don't hammer the DB
// for a view nobody is looking at. Catch up with one fresh load whenever the
// tab returns to the foreground.
setInterval(() => {
  if (document.visibilityState === "visible") load();
}, 5000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") load();
});
