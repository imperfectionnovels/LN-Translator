/* Project stats dashboard (Initiative 6).
 *
 * One-pager that toggles between a global view and per-novel views.
 * Plain HTML cards; throughput is a tiny inline-SVG sparkline. No
 * charting library — keeps the no-build constraint intact.
 */

const novelSelect = document.getElementById("stats-novel");
const grid = document.getElementById("stats-grid");

/* ---- API helpers ---- */
const statsApi = {
  global: () => fetch("/api/stats/global").then(async r => {
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  }),
  novel: (id) => fetch(`/api/stats/novel/${id}`).then(async r => {
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  }),
};

/* ---- Load novels into the picker ---- */
async function loadNovelList() {
  try {
    const novels = await api.novels();
    novels.sort((a, b) => a.title.localeCompare(b.title));
    novelSelect.insertAdjacentHTML(
      "beforeend",
      novels.map(n => `<option value="${n.id}">${escapeHtml(n.title)}</option>`).join("")
    );
  } catch (e) {
    novelSelect.insertAdjacentHTML(
      "beforeend",
      `<option disabled>Load failed: ${escapeHtml(e.message)}</option>`
    );
  }
}

/* ---- Formatting helpers ---- */
function fmtInt(v) {
  if (v == null) return "…";
  return Number(v).toLocaleString();
}
function fmtPct(num, den) {
  if (!den) return "…";
  return `${(100 * num / den).toFixed(1)}%`;
}

/* ---- Sparkline ---- */
function sparkline(points) {
  if (!points || !points.length) {
    return `<div class="stats-empty">No throughput in the last 30 days.</div>`;
  }
  // Map values to bar heights inside a fixed viewBox. SVG instead of
  // a charting lib keeps payload tiny and theme-aware via fill: var(...).
  const max = Math.max(...points.map(p => p.count));
  const w = Math.max(120, points.length * 14);
  const h = 40;
  const bw = Math.max(2, Math.floor(w / points.length) - 2);
  const bars = points.map((p, i) => {
    const bh = Math.max(2, Math.round((p.count / max) * (h - 8)));
    const x = i * (bw + 2);
    const y = h - bh;
    return `<rect x="${x}" y="${y}" width="${bw}" height="${bh}"><title>${p.day}: ${p.count}</title></rect>`;
  }).join("");
  return `<svg class="stats-sparkline" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${bars}</svg>
          <div class="stats-sub">${points.length} days with translations · max ${max}/day</div>`;
}

/* ---- Card renderers ---- */
function cardCoverage(s) {
  const cov = s.coverage;
  return `
    <div class="stats-card">
      <h3>Coverage</h3>
      <div class="stats-headline">${cov.done_chapters} / ${cov.total_chapters} translated</div>
      <div class="stats-bar-row">
        <span class="lbl">Translated</span>
        <span>${fmtPct(cov.done_chapters, cov.total_chapters)}</span>
        <div class="bar"><span class="fill" style="width:${(100 * cov.done_chapters / Math.max(cov.total_chapters, 1)).toFixed(1)}%"></span></div>
      </div>
      ${cov.refined_chapters != null ? `
      <div class="stats-bar-row">
        <span class="lbl">Refined</span>
        <span>${fmtPct(cov.refined_chapters, cov.total_chapters)}</span>
        <div class="bar"><span class="fill" style="width:${(100 * cov.refined_chapters / Math.max(cov.total_chapters, 1)).toFixed(1)}%"></span></div>
      </div>` : ""}
      ${cov.style_edit_chapters != null ? `
      <div class="stats-row"><span class="lbl">Chapters with style edits</span><span class="val">${fmtInt(cov.style_edit_chapters)}</span></div>` : ""}
      ${cov.observation_chapters != null ? `
      <div class="stats-row"><span class="lbl">Chapters with QA flags</span><span class="val">${fmtInt(cov.observation_chapters)}</span></div>` : ""}
    </div>`;
}

function cardTokens(s) {
  const t = s.tokens;
  if (!t) return "";
  return `
    <div class="stats-card">
      <h3>Tokens</h3>
      <div class="stats-row"><span class="lbl">Input tokens</span><span class="val">${fmtInt(t.input_tokens_total)}</span></div>
      <div class="stats-row"><span class="lbl">Output tokens</span><span class="val">${fmtInt(t.output_tokens_total)}</span></div>
      ${t.cached_input_tokens_total ? `
      <div class="stats-row"><span class="lbl">Cached input tokens</span><span class="val">${fmtInt(t.cached_input_tokens_total)}</span></div>` : ""}
    </div>`;
}

function cardWords(s) {
  const w = s.words || {};
  return `
    <div class="stats-card">
      <h3>Words</h3>
      <div class="stats-headline">${fmtInt(w.english_words)} EN</div>
      <div class="stats-row"><span class="lbl">Source chars (CJK)</span><span class="val">${fmtInt(w.source_chars)}</span></div>
      <div class="stats-row"><span class="lbl">Refined words</span><span class="val">${fmtInt(w.refined_words)}</span></div>
      ${w.english_words_per_source_char != null ? `
      <div class="stats-row"><span class="lbl">Words / source char</span><span class="val">${w.english_words_per_source_char.toFixed(2)}</span></div>` : ""}
    </div>`;
}

function cardThroughput(s) {
  return `
    <div class="stats-card">
      <h3>Throughput · last 30 days</h3>
      ${sparkline(s.throughput)}
    </div>`;
}

function cardGlossary(s) {
  const g = s.glossary || {};
  return `
    <div class="stats-card">
      <h3>Glossary</h3>
      ${g.locked != null ? `
      <div class="stats-row"><span class="lbl">Locked</span><span class="val">${fmtInt(g.locked)}</span></div>
      <div class="stats-row"><span class="lbl">Auto-detected</span><span class="val">${fmtInt(g.auto)}</span></div>` : ""}
      <div class="stats-row"><span class="lbl">Global (cross-novel)</span><span class="val">${fmtInt(g.global_total)}</span></div>
    </div>`;
}

function cardObservations(s) {
  const obs = s.observations || {};
  if (!obs.by_kind || obs.by_kind.length === 0) {
    return `
      <div class="stats-card">
        <h3>QA observations</h3>
        <div class="stats-empty">No undismissed observations.</div>
      </div>`;
  }
  return `
    <div class="stats-card">
      <h3>QA observations</h3>
      <div class="stats-headline">${fmtInt(obs.total_undismissed)} undismissed</div>
      ${obs.by_kind.map(k => `
        <div class="stats-row"><span class="lbl">${escapeHtml(k.kind)}</span><span class="val">${fmtInt(k.count)}</span></div>
      `).join("")}
    </div>`;
}

function cardTm(s) {
  const tm = s.tm || {};
  return `
    <div class="stats-card">
      <h3>Translation memory</h3>
      <div class="stats-headline">${fmtInt(tm.total_segments)} segments</div>
      ${tm.distinct_source_hashes != null ? `
      <div class="stats-row"><span class="lbl">Distinct source paragraphs</span><span class="val">${fmtInt(tm.distinct_source_hashes)}</span></div>` : ""}
      ${tm.duplication_ratio != null ? `
      <div class="stats-row"><span class="lbl">Duplication ratio</span><span class="val">${tm.duplication_ratio.toFixed(2)}×</span></div>` : ""}
      ${tm.avg_source_chars ? `
      <div class="stats-row"><span class="lbl">Avg source chars / segment</span><span class="val">${tm.avg_source_chars.toFixed(0)}</span></div>` : ""}
      ${tm.avg_target_chars ? `
      <div class="stats-row"><span class="lbl">Avg target chars / segment</span><span class="val">${tm.avg_target_chars.toFixed(0)}</span></div>` : ""}
    </div>`;
}

function cardProviderMix(s) {
  if (!s.provider_mix || s.provider_mix.length === 0) {
    return `
      <div class="stats-card">
        <h3>Provider mix</h3>
        <div class="stats-empty">No completed chapters yet.</div>
      </div>`;
  }
  const totalChapters = s.provider_mix.reduce((a, p) => a + p.chapter_count, 0);
  return `
    <div class="stats-card">
      <h3>Provider mix</h3>
      ${s.provider_mix.map(p => {
        const share = totalChapters ? (100 * p.chapter_count / totalChapters).toFixed(0) : 0;
        return `
          <div class="stats-bar-row">
            <span class="lbl">${escapeHtml(p.provider_name)}</span>
            <span>${p.chapter_count} ch</span>
            <div class="bar"><span class="fill" style="width:${share}%"></span></div>
          </div>`;
      }).join("")}
    </div>`;
}

function cardNovelHeader(s) {
  if (s.novel_count != null) {
    return `
      <div class="stats-card">
        <h3>Library</h3>
        <div class="stats-headline">${fmtInt(s.novel_count)} novels</div>
      </div>`;
  }
  return `
    <div class="stats-card">
      <h3>Novel</h3>
      <div class="stats-headline">${escapeHtml(s.novel_title || "…")}</div>
    </div>`;
}

/* ---- Render ---- */
async function render() {
  grid.setAttribute("aria-busy", "true");
  grid.innerHTML = `<p class="muted">Loading…</p>`;
  const novelId = novelSelect.value;
  try {
    const s = novelId ? await statsApi.novel(novelId) : await statsApi.global();
    grid.setAttribute("aria-busy", "false");
    const parts = [
      cardNovelHeader(s),
      cardCoverage(s),
    ];
    if (novelId) parts.push(cardTokens(s));
    if (!novelId) parts.push(cardProviderMix(s));
    if (novelId) parts.push(cardWords(s));
    parts.push(cardThroughput(s));
    parts.push(cardGlossary(s));
    if (novelId) parts.push(cardObservations(s));
    parts.push(cardTm(s));
    grid.innerHTML = parts.join("");
  } catch (e) {
    grid.setAttribute("aria-busy", "false");
    grid.innerHTML = `<p class="status err">Load failed: ${escapeHtml(e.message)}</p>`;
  }
}

novelSelect.addEventListener("change", render);

(async () => {
  await loadNovelList();
  // Preselect via ?novel=N query param if present.
  const initialId = new URLSearchParams(location.search).get("novel");
  if (initialId) {
    novelSelect.value = initialId;
  }
  await render();
})();
