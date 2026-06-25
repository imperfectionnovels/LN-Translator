/* Quality + consistency cockpit (read-only).
 *
 * The standalone half of "see -> fix -> learn": it surfaces the scorecard /
 * consistency IP that used to live only in CLI scripts, and turns the worst
 * terms + worst chapters into clickable worklists that deep-link into the
 * glossary editor and the reader's edit mode. Plain HTML cards + innerHTML
 * templates (the stats.js pattern); no charting lib, no framework, no build.
 */

const novelSelect = document.getElementById("quality-novel");
const grid = document.getElementById("quality-grid");

/* ---- Display config ---- */
const CATEGORY_LABELS = {
  glossary_presence: "Glossary presence",
  glossary_casing: "Glossary casing",
  glossary_observers: "Glossary observers",
  punctuation_carry: "Punctuation carry-over",
  sentence_shape: "Sentence shape",
  banned_words: "Banned words",
  stock_phrases: "Stock phrases",
  costume_constructions: "Costume constructions",
  epithet_frequency: "Epithet frequency",
  thought_format: "Thought formatting",
  envelope_format: "Envelope format",
  unit_conversion: "Unit conversion",
};

/* ---- Formatting helpers ---- */
function pct1(x) { return `${(100 * x).toFixed(1)}%`; }
function band(rate) {
  // rate = TCR or compliance (higher is better). Color by health band.
  if (rate >= 0.9) return "good";
  if (rate >= 0.75) return "warn";
  return "bad";
}

/* ---- Card renderers ---- */
function cardSummary(card, cons) {
  const tcr = cons.tcr.overall_tcr;
  const stale = card.schema_outdated
    ? `<div class="q-note q-note-warn">This novel's data predates the per-chapter
        fixup detail column. Rebuild and restart the app to enable full detail.</div>`
    : "";
  return `
    <div class="q-card q-card-wide">
      <h3>Overview</h3>
      <div class="q-headline">
        <span class="q-big ${band(tcr)}">${pct1(tcr)}</span>
        <span class="q-big-label">term consistency (TCR)</span>
      </div>
      <div class="q-row"><span class="lbl">Chapters scored</span><span class="val">${card.chapters_scored}</span></div>
      <div class="q-row"><span class="lbl">Checkable term occurrences</span><span class="val">${cons.tcr.checkable.toLocaleString()}</span></div>
      <div class="q-row"><span class="lbl">Glossary (locked / total)</span><span class="val">${cons.glossary_locked} / ${cons.glossary_terms}</span></div>
      ${stale}
    </div>`;
}

function _barRows(rows) {
  // rows: [{label, value (0..1), right}]
  return rows.map(r => `
    <div class="q-bar-row">
      <span class="q-bar-label">${escapeHtml(r.label)}</span>
      <span class="q-bar-val ${band(r.value)}">${r.right}</span>
      <div class="q-bar"><span class="q-fill ${band(r.value)}" style="width:${(100 * r.value).toFixed(1)}%"></span></div>
    </div>`).join("");
}

function cardTcrByCategory(cons) {
  const cats = cons.tcr.by_category || {};
  const rows = Object.entries(cats)
    .sort((a, b) => a[1].tcr - b[1].tcr)
    .map(([name, v]) => ({
      label: name, value: v.tcr,
      right: `${pct1(v.tcr)} <span class="q-dim">(${v.consistent}/${v.checkable})</span>`,
    }));
  return `
    <div class="q-card">
      <h3>Consistency by category</h3>
      ${rows.length ? _barRows(rows) : `<div class="q-empty">No checkable terms.</div>`}
    </div>`;
}

function cardCompliance(card) {
  const cats = card.categories || {};
  const rows = Object.entries(cats)
    .filter(([, c]) => c.opportunities > 0)
    .map(([name, c]) => {
      const compliance = 1 - c.rate;
      return {
        label: CATEGORY_LABELS[name] || name,
        value: compliance,
        right: `${pct1(compliance)} <span class="q-dim">(${c.violations}/${c.opportunities})</span>`,
        _v: c.violations,
      };
    })
    .sort((a, b) => a.value - b.value);
  return `
    <div class="q-card">
      <h3>Rule compliance by category</h3>
      <div class="q-card-sub">Share of opportunities with no rule violation. Lower bars first.</div>
      ${rows.length ? _barRows(rows) : `<div class="q-empty">Nothing scored.</div>`}
    </div>`;
}

function cardWorstTerms(cons, novelId) {
  const worst = (cons.tcr.worst_terms || []).filter(t => t.tcr < 1);
  if (!worst.length) {
    return `<div class="q-card"><h3>Drifting terms</h3>
      <div class="q-empty">No locked term drifts (every checkable term renders consistently).</div></div>`;
  }
  return `
    <div class="q-card">
      <h3>Drifting terms · triage</h3>
      <div class="q-card-sub">Locked terms rendered inconsistently. Click to open the glossary entry.</div>
      <div class="q-worklist">
        ${worst.slice(0, 20).map(t => `
          <a class="q-work-row" href="/glossary?novel=${novelId}&focus=${t.id}">
            <span class="q-work-tag ${band(t.tcr)}">${pct1(t.tcr)}</span>
            <span class="q-work-main">
              <span class="q-work-zh">${escapeHtml(t.term_zh)}</span>
              <span class="q-work-en">${escapeHtml(t.term_en)}</span>
            </span>
            <span class="q-work-meta">${t.consistent}/${t.checkable} ch</span>
            <span class="q-work-go" aria-hidden="true">→</span>
          </a>`).join("")}
      </div>
    </div>`;
}

function cardWorstChapters(card, novelId) {
  const worst = (card.worst_chapters || []).filter(c => c.violations > 0);
  if (!worst.length) {
    return `<div class="q-card"><h3>Lowest-scoring chapters</h3>
      <div class="q-empty">No rule violations across the scored range.</div></div>`;
  }
  return `
    <div class="q-card">
      <h3>Lowest-scoring chapters · triage</h3>
      <div class="q-card-sub">Most rule violations first. Click to open in the reader's edit mode.</div>
      <div class="q-worklist">
        ${worst.slice(0, 20).map(c => `
          <a class="q-work-row" href="/reader?novel=${novelId}&ch=${c.chapter_num}&mode=edit">
            <span class="q-work-tag bad">${c.violations}</span>
            <span class="q-work-main">
              <span class="q-work-zh">Ch.${c.chapter_num}</span>
              <span class="q-work-en">${escapeHtml(c.title_en || "")}</span>
            </span>
            <span class="q-work-meta">${c.fixup_total ? `${c.fixup_total} fixups` : ""}</span>
            <span class="q-work-go" aria-hidden="true">→</span>
          </a>`).join("")}
      </div>
    </div>`;
}

function cardSignals(card) {
  const obs = card.observations || {};
  const fx = card.fixup_churn || {};
  const obsRows = Object.entries(obs)
    .sort((a, b) => b[1].count - a[1].count)
    .slice(0, 8)
    .map(([kind, v]) => `<div class="q-row"><span class="lbl">${escapeHtml(kind)}</span><span class="val">${v.count} <span class="q-dim">/ ${v.chapters} ch</span></span></div>`)
    .join("");
  const fxRows = Object.entries(fx.rule_counts || {})
    .slice(0, 8)
    .map(([name, n]) => `<div class="q-row"><span class="lbl">${escapeHtml(name)}</span><span class="val">${n} <span class="q-dim">/ ${(fx.rule_chapters || {})[name] || 0} ch</span></span></div>`)
    .join("");
  return `
    <div class="q-card">
      <h3>QA signals</h3>
      <div class="q-card-sub">Observer hits and deterministic fixups are telemetry, not failures.</div>
      <div class="q-subhead">Observers</div>
      ${obsRows || `<div class="q-empty">No observer hits recorded.</div>`}
      <div class="q-subhead">Fixup churn</div>
      ${fxRows || `<div class="q-empty">No fixups recorded (chapters may predate the audit column).</div>`}
    </div>`;
}

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

/* ---- Render ---- */
async function render() {
  const novelId = novelSelect.value;
  if (!novelId) {
    grid.setAttribute("aria-busy", "false");
    grid.innerHTML = `<p class="muted">Pick a novel to see its quality scorecard.</p>`;
    return;
  }
  grid.setAttribute("aria-busy", "true");
  grid.innerHTML = `<p class="muted">Scoring the novel… (a full-novel scan can take a few seconds)</p>`;
  try {
    const [card, cons] = await Promise.all([
      api.qualityScorecard(novelId),
      api.qualityConsistency(novelId),
    ]);
    grid.setAttribute("aria-busy", "false");
    grid.innerHTML = [
      cardSummary(card, cons),
      cardWorstTerms(cons, novelId),
      cardWorstChapters(card, novelId),
      cardTcrByCategory(cons),
      cardCompliance(card),
      cardSignals(card),
    ].join("");
  } catch (e) {
    grid.setAttribute("aria-busy", "false");
    grid.innerHTML = `<p class="status err">Load failed: ${escapeHtml(e.message)}</p>`;
  }
}

novelSelect.addEventListener("change", () => {
  const id = novelSelect.value;
  history.replaceState(null, "", id ? `/quality?novel=${encodeURIComponent(id)}` : "/quality");
  render();
});

(async () => {
  await loadNovelList();
  const initialId = new URLSearchParams(location.search).get("novel");
  if (initialId) novelSelect.value = initialId;
  await render();
})();
