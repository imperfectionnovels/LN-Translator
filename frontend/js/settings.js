// Settings page (control-room redesign, 2026-05-26).
//
// Layout: sticky left TOC rail + section stack.
// Sections (top to bottom): providers, defaults, themes, keyboard, about.
//
// Providers section is the heaviest — for each row we fetch /stats,
// /routed-novels, and /activity in parallel and render into slots that
// show "—" or "Awaiting first translation" on a fresh DB.
//
// Defaults section replaces the two-dropdown form with an inline-editable
// sentence ("Every novel imported translates with X, and runs the Y
// pass…"). Optimistic save + undo toast.
//
// Per-novel state (genre, source language, translator override) lives on
// each novel's own page — never mixed into App Settings.

// ============================================================
// DOM cache
// ============================================================
const els = {
  list: document.getElementById("provider-list"),
  addBtn: document.getElementById("add-provider-btn"),
  dialog: document.getElementById("provider-dialog"),
  form: document.getElementById("provider-form"),
  dialogTitle: document.getElementById("provider-dialog-title"),
  fId: document.getElementById("provider-id"),
  fName: document.getElementById("provider-name"),
  fType: document.getElementById("provider-type"),
  fTypeHint: document.getElementById("provider-type-hint"),
  fModelSelect: document.getElementById("provider-model-select"),
  fModel: document.getElementById("provider-model-id"),
  fModelIdRow: document.getElementById("provider-model-id-row"),
  fBaseUrl: document.getElementById("provider-base-url"),
  fBaseUrlRow: document.getElementById("provider-base-url-row"),
  fSecret: document.getElementById("provider-secret-ref"),
  fSecretRow: document.getElementById("provider-secret-ref-row"),
  fSecretValue: document.getElementById("provider-secret-value"),
  fSecretValueRow: document.getElementById("provider-secret-value-row"),
  fAuthCallout: document.getElementById("provider-auth-callout"),
  fAuthCalloutTitle: document.getElementById("provider-auth-title"),
  fAuthCalloutText: document.getElementById("provider-auth-text"),
  fAuthCalloutCmd: document.getElementById("provider-auth-cmd"),
  // fDefault dropped — the per-card "Set default" button is the single
  // surface for that operation now (S3).
  // confirm-dialog handles are managed by utils.js::confirmDialog (C7).
  secretDialog: document.getElementById("secret-dialog"),
  secretForm: document.getElementById("secret-form"),
  secretProviderId: document.getElementById("secret-provider-id"),
  secretValue: document.getElementById("secret-value"),
  secretExplainer: document.getElementById("secret-explainer"),
  toast: document.getElementById("settings-toast"),
  tocList: document.getElementById("settings-toc-list"),
};

// Provider type catalog. Fetched once per page load from
// /api/providers/catalog and cached. Drives the Type dropdown, Model
// dropdown, and the auto-fill logic when the user picks a new type.
let _catalogCache = null;
let _catalogByType = new Map();

async function loadCatalog() {
  if (_catalogCache) return _catalogCache;
  try {
    const res = await fetch("/api/providers/catalog");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _catalogCache = await res.json();
  } catch (e) {
    console.error("Failed to load provider catalog:", e);
    _catalogCache = [];
  }
  _catalogByType = new Map(_catalogCache.map(t => [t.type, t]));
  populateTypeSelect();
  return _catalogCache;
}

function populateTypeSelect() {
  if (!els.fType) return;
  // Group entries by .group field — "Subscription", "API key", "Local".
  const groups = new Map();
  for (const entry of _catalogCache) {
    const arr = groups.get(entry.group) || [];
    arr.push(entry);
    groups.set(entry.group, arr);
  }
  const orderedGroups = ["Subscription", "API key", "Local"];
  let html = "";
  for (const groupName of orderedGroups) {
    const entries = groups.get(groupName);
    if (!entries || entries.length === 0) continue;
    html += `<optgroup label="${escapeHtml(groupName)}">`;
    for (const e of entries) {
      html += `<option value="${escapeHtml(e.type)}">${escapeHtml(e.display)}</option>`;
    }
    html += `</optgroup>`;
  }
  els.fType.innerHTML = html;
}

function populateModelSelect(typeKey, preselectedModelId) {
  const entry = _catalogByType.get(typeKey);
  if (!entry || !els.fModelSelect) return;
  const sentinel = entry.custom_model_sentinel || "__custom__";
  let html = "";
  for (const m of entry.models || []) {
    html += `<option value="${escapeHtml(m.id)}">${escapeHtml(m.display)}</option>`;
  }
  // Always offer the custom escape hatch — a new model release should never
  // block the user.
  html += `<option value="${escapeHtml(sentinel)}">Other (custom ID)…</option>`;
  els.fModelSelect.innerHTML = html;

  // Decide which option to select: the existing model_id (when editing) if
  // it matches a curated entry, else the custom sentinel + populate the
  // free-text input. For create mode (no preselectedModelId), pick the
  // first curated entry as the default.
  if (preselectedModelId) {
    const match = (entry.models || []).find(m => m.id === preselectedModelId);
    if (match) {
      els.fModelSelect.value = preselectedModelId;
      els.fModel.value = preselectedModelId;
      els.fModelIdRow.hidden = true;
    } else {
      els.fModelSelect.value = sentinel;
      els.fModel.value = preselectedModelId;
      els.fModelIdRow.hidden = false;
    }
  } else if ((entry.models || []).length > 0) {
    els.fModelSelect.value = entry.models[0].id;
    els.fModel.value = entry.models[0].id;
    els.fModelIdRow.hidden = true;
  } else {
    els.fModelSelect.value = sentinel;
    els.fModel.value = "";
    els.fModelIdRow.hidden = false;
  }
}

function applyTypeDefaults(typeKey, { keepUserValues = false } = {}) {
  // Auto-fill base_url, secret_ref hint, and show/hide rows based on the
  // catalog entry's auth shape. When `keepUserValues` is true (edit mode)
  // we don't clobber what the user already had in the form — we only fill
  // empty fields.
  const entry = _catalogByType.get(typeKey);
  if (!entry) return;

  const isApiKey = entry.auth === "api_key";
  const isSubscription = entry.auth === "subscription";
  const isLocalNoAuth = entry.auth === "none";

  // The inline install hint under the Type dropdown is only useful for
  // api_key types — for subscription / local types we display the same
  // information (plus the login command) in the auth callout below.
  if (els.fTypeHint) {
    if (isApiKey && entry.install_hint) {
      els.fTypeHint.textContent = entry.install_hint;
      els.fTypeHint.hidden = false;
    } else {
      els.fTypeHint.textContent = "";
      els.fTypeHint.hidden = true;
    }
  }

  // Auth callout — only for subscription / local types. Makes the
  // alternative auth path explicit instead of relying on absent fields.
  if (els.fAuthCallout) {
    if (isSubscription || isLocalNoAuth) {
      els.fAuthCalloutTitle.textContent = isSubscription
        ? "Sign in with your subscription"
        : "Local provider. No key needed";
      els.fAuthCalloutText.textContent =
        entry.install_hint
        || (isSubscription
          ? "This provider uses your existing subscription. Authentication happens out-of-band. No API key goes in this form."
          : "This provider runs locally on your machine. No authentication needed.");
      if (entry.auth_command) {
        els.fAuthCalloutCmd.textContent = entry.auth_command;
        els.fAuthCalloutCmd.hidden = false;
      } else {
        els.fAuthCalloutCmd.textContent = "";
        els.fAuthCalloutCmd.hidden = true;
      }
      els.fAuthCallout.hidden = false;
    } else {
      els.fAuthCallout.hidden = true;
      els.fAuthCalloutCmd.hidden = true;
    }
  }

  // Base URL: shown for API-key types (so the user can override the
  // catalog default) and for local types like ollama (where the endpoint
  // is the auth). Hidden for subscription types — they don't talk HTTP
  // from this side.
  const showBaseUrl = !isSubscription;
  els.fBaseUrlRow.hidden = !showBaseUrl;
  if (!keepUserValues || !els.fBaseUrl.value) {
    els.fBaseUrl.value = entry.base_url_default || "";
  }

  // Secret env var input + inline API-key field: only for api_key types.
  els.fSecretRow.hidden = !isApiKey;
  if (!keepUserValues || !els.fSecret.value) {
    els.fSecret.value = entry.secret_ref_hint || "";
  }
  const creating = !els.fId.value;
  els.fSecretValueRow.hidden = !(creating && isApiKey);
  if (els.fSecretValueRow.hidden) {
    els.fSecretValue.value = "";
  }
}

function onTypeChange() {
  const typeKey = els.fType.value;
  applyTypeDefaults(typeKey);
  populateModelSelect(typeKey, null);
}

function onModelSelectChange() {
  const typeKey = els.fType.value;
  const entry = _catalogByType.get(typeKey);
  const sentinel = entry?.custom_model_sentinel || "__custom__";
  const choice = els.fModelSelect.value;
  if (choice === sentinel) {
    els.fModelIdRow.hidden = false;
    if (!els.fModel.value) els.fModel.focus?.();
  } else {
    els.fModel.value = choice;
    els.fModelIdRow.hidden = true;
  }
}

// ============================================================
// Utilities
// ============================================================
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  })[c]);
}
function fmtBytes(b) {
  if (!b || b < 0) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, n = b;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
function fmtRel(iso) {
  if (!iso) return "…";
  // Backend writes naive UTC strings like "2026-05-26 12:34:56"; treat as UTC.
  let d;
  try {
    d = new Date(iso.includes("T") ? iso : iso.replace(" ", "T") + "Z");
  } catch { return iso; }
  const ms = Date.now() - d.getTime();
  if (!Number.isFinite(ms) || ms < 0) return "just now";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const days = Math.floor(h / 24);
  return `${days}d ago`;
}
function fmtMoney(usd) {
  if (usd == null || !Number.isFinite(usd)) return "$0.00";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

let _toastTimer = null;
function showToast(message, undoAction) {
  if (!els.toast) return;
  els.toast.innerHTML = `<span>${escapeHtml(message)}</span>` +
    (undoAction ? `<button type="button" data-toast-undo>Undo</button>` : "");
  els.toast.hidden = false;
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    els.toast.hidden = true;
    _toastTimer = null;
  }, 5000);
  if (undoAction) {
    const btn = els.toast.querySelector("[data-toast-undo]");
    btn?.addEventListener("click", () => {
      try { undoAction(); } finally {
        clearTimeout(_toastTimer);
        els.toast.hidden = true;
      }
    }, { once: true });
  }
}

// ============================================================
// Provider dialog secrets — replaced by catalog-driven applyTypeDefaults
// above. The legacy _toggleSecretValueField shim stays as a thin alias so
// any caller that wasn't migrated still works.
// ============================================================
function _toggleSecretValueField() {
  if (!els.fType.value) return;
  applyTypeDefaults(els.fType.value, { keepUserValues: true });
}

async function openSecretDialog(providerId, secretRef) {
  els.secretProviderId.value = providerId;
  els.secretValue.value = "";
  els.secretExplainer.textContent =
    `Stored under LN-Translator/${secretRef} in the OS keychain. ` +
    `On Windows: visible in Control Panel → Credential Manager. ` +
    `Existing value (if any) will be overwritten.`;
  return new Promise(resolve => {
    const onSubmit = async (e) => {
      e.preventDefault();
      const value = els.secretValue.value;
      if (!value) return;
      try {
        await api.setProviderSecret(providerId, value);
        cleanup();
        els.secretDialog.close();
        showToast("API key stored in OS keychain.");
        resolve(true);
      } catch (err) {
        alert(`Failed to store: ${err.message}`);
      }
    };
    const onCancel = (e) => { e?.preventDefault?.(); cleanup(); els.secretDialog.close(); resolve(false); };
    const onCancelEvt = () => { cleanup(); resolve(false); };
    function cleanup() {
      els.secretForm.removeEventListener("submit", onSubmit);
      const cancelBtn = els.secretForm.querySelector("[data-act='cancel']");
      if (cancelBtn) cancelBtn.removeEventListener("click", onCancel);
      els.secretDialog.removeEventListener("cancel", onCancelEvt);
      els.secretDialog.removeEventListener("close", _clearSecretInput);
    }
    function _clearSecretInput() { els.secretValue.value = ""; }
    els.secretForm.addEventListener("submit", onSubmit);
    const cancelBtn = els.secretForm.querySelector("[data-act='cancel']");
    if (cancelBtn) cancelBtn.addEventListener("click", onCancel);
    els.secretDialog.addEventListener("cancel", onCancelEvt);
    els.secretDialog.addEventListener("close", _clearSecretInput, { once: true });
    els.secretDialog.showModal();
    setTimeout(() => els.secretValue.focus(), 0);
  });
}

// ============================================================
// §01 · Provider control-room card
// ============================================================
// Visual treatment per type. Glyph + hue are aesthetic; the `label` is a
// short type name (the longer catalog display lives in the Add Provider
// dialog). Unknown types fall back to a generic glyph + the raw type key.
const PROVIDER_TYPE_META = {
  // Subscription
  claude_agent: { glyph: "C", hue: "#c8423a", label: "Claude Agent" },
  claude_cli:   { glyph: "$", hue: "#a83a32", label: "Claude CLI" },
  codex_cli:    { glyph: "O", hue: "#10a37f", label: "Codex CLI" },
  gemini_cli:   { glyph: "G", hue: "#4a6da8", label: "Gemini CLI" },
  opencode:     { glyph: "@", hue: "#7b5da8", label: "OpenCode" },
  // API key
  anthropic_api:     { glyph: "A", hue: "#c8423a", label: "Anthropic API" },
  gemini:            { glyph: "G", hue: "#4a6da8", label: "Gemini" },
  openai:            { glyph: "O", hue: "#10a37f", label: "OpenAI" },
  deepseek:          { glyph: "D", hue: "#2f6a5e", label: "DeepSeek" },
  xai:               { glyph: "X", hue: "#222222", label: "xAI Grok" },
  mistral:           { glyph: "M", hue: "#ff7000", label: "Mistral" },
  openrouter:        { glyph: "R", hue: "#6b6b6b", label: "OpenRouter" },
  qwen:              { glyph: "Q", hue: "#d96962", label: "Qwen" },
  zhipu:             { glyph: "Z", hue: "#2f6a5e", label: "Zhipu GLM" },
  moonshot:          { glyph: "K", hue: "#1a3a8a", label: "Moonshot Kimi" },
  groq:              { glyph: "g", hue: "#f55036", label: "Groq" },
  openai_compatible: { glyph: "*", hue: "#888888", label: "OpenAI-compatible" },
  // Local
  ollama: { glyph: "L", hue: "#5a7a4a", label: "Ollama" },
};

const _NOVEL_CHIP_HUES = ["", "b2", "b3", "b4", "b5"];
function novelChipHue(novelId) {
  return _NOVEL_CHIP_HUES[novelId % _NOVEL_CHIP_HUES.length];
}
function novelChipInitial(title) {
  // Prefer first CJK char (matches the design's mini-cover aesthetic); fall
  // back to first letter / glyph.
  if (!title) return "?";
  const m = String(title).match(/\p{Script=Han}/u);
  if (m) return m[0];
  const ch = String(title).trim()[0];
  return ch ? ch.toUpperCase() : "?";
}

function providerStatus(p) {
  // Catalog-driven: only `api_key` types need a secret_ref. Subscription
  // types (claude_*, codex_cli, gemini_cli, opencode) and local (ollama)
  // authenticate out-of-band. Falls back to the legacy {gemini, deepseek}
  // set if the catalog hasn't loaded yet — defensive against a /providers
  // render that races the catalog fetch on cold boot.
  const entry = _catalogByType.get(p.provider_type);
  const needsSecret = entry
    ? entry.auth === "api_key"
    : (p.provider_type === "gemini" || p.provider_type === "deepseek");
  if (!needsSecret) {
    const hint = entry?.auth === "none"
      ? "Local. No secret needed"
      : "No secret needed (subscription / local auth)";
    return { kind: "ok", hint };
  }
  if (p.secret_ref) return { kind: "ok", hint: `Secret resolves via ${p.secret_ref}` };
  return { kind: "warn", hint: "Needs an API key. Click Set API key" };
}

function _sparkSpans(buckets, cls = "") {
  if (!buckets || buckets.length === 0) {
    return Array.from({ length: 14 }, () =>
      `<span class="${cls}" style="height:6%"></span>`).join("");
  }
  const max = Math.max(...buckets, 1);
  return buckets.map((v, i) => {
    const pct = Math.max(2, Math.round((v / max) * 100));
    const hi = i === buckets.length - 1 ? " hi" : "";
    return `<span class="${cls}${hi}" style="height:${pct}%"></span>`;
  }).join("");
}

function renderProviderCardSkeleton(p) {
  const meta = PROVIDER_TYPE_META[p.provider_type] || { glyph: "·", hue: "var(--accent)", label: p.provider_type };
  const status = providerStatus(p);
  const tested = p.last_tested_at
    ? `<span class="tested-tag">tested ${fmtRel(p.last_tested_at)}</span>`
    : `<span class="tested-tag never">never tested</span>`;
  return `
    <article class="prov-card" data-id="${p.id}" data-secret-ref="${escapeHtml(p.secret_ref || '')}" style="--prov-hue:${meta.hue}">
      <header class="prov-head">
        <div class="prov-seal">${escapeHtml(meta.glyph)}</div>
        <div>
          <div class="prov-name">
            <span class="status ${status.kind}" title="${escapeHtml(status.hint)}"></span>
            ${escapeHtml(p.name)}
            ${p.is_default ? '<span class="default-pill">default</span>' : ""}
          </div>
          <div class="prov-meta">
            <span class="ty">${escapeHtml(meta.label)}</span>
            <span>model:</span> <code>${escapeHtml(p.model_id)}</code>
            ${p.base_url ? `<span>·</span> <code>${escapeHtml(p.base_url)}</code>` : ""}
            ${p.secret_ref ? `<span>·</span> <code>${escapeHtml(p.secret_ref)}</code>` : ""}
            <span>·</span> ${tested}
          </div>
        </div>
        <div class="prov-actions">
          <button type="button" class="btn-tiny primary" data-act="test"><span style="font-family: var(--font-family-han); font-weight: 700;">試</span> Test</button>
          ${p.secret_ref ? '<button type="button" class="btn-tiny" data-act="set-secret">Key</button>' : ""}
          ${p.is_default ? "" : '<button type="button" class="btn-tiny" data-act="set-default">Set default</button>'}
          <button type="button" class="btn-tiny" data-act="edit">Edit</button>
          <button type="button" class="btn-tiny danger" data-act="delete">Delete</button>
        </div>
      </header>
      <div class="prov-stats" data-slot="stats">
        ${_skeletonStats()}
      </div>
      <div class="prov-routes" data-slot="routes">
        <span class="lbl">Routes</span>
        <span class="empty">Loading…</span>
      </div>
      <div class="prov-log" data-slot="log">
        <div class="l-head">Recent activity</div>
        <div class="empty">Loading…</div>
      </div>
      <div class="test-result" data-test-result hidden></div>
    </article>
  `;
}

function _skeletonStats() {
  const tile = (lbl) => `
    <div class="prov-stat">
      <div class="lbl">${lbl}</div>
      <div class="v">…</div>
      <div class="sparkline">${_sparkSpans([])}</div>
    </div>`;
  return tile("Translated · 30 d") + tile("Spend · 30 d") +
         tile("Failure rate") + tile("Last tested");
}

function _renderStatsSlot(card, stats) {
  const slot = card.querySelector("[data-slot=stats]");
  if (!slot) return;
  if (!stats || stats.chapters_translated_30d === 0) {
    // S2: empty-state copy. A fresh DB has no usage data, and showing the
    // 4-cell grid with "Awaiting first translation / $0.00 / — / never"
    // ate 200px of vertical for no real information. Collapse to a single
    // line that names the actionable next step.
    const lastTested = stats?.last_tested_at ? fmtRel(stats.last_tested_at) : null;
    slot.innerHTML = `
      <div class="prov-stat-empty">
        <div class="lbl">No translations yet</div>
        <div class="v">Set this provider as default, or assign it to a novel, to start using it.</div>
        ${lastTested ? `<div class="meta">Last tested ${lastTested}.</div>` : ""}
      </div>
    `;
    return;
  }
  const chBuckets = stats.chapters_translated_buckets || [];
  const spBuckets = stats.spend_30d_buckets || [];
  const failPct = (stats.failure_rate_30d * 100).toFixed(stats.failure_rate_30d < 0.01 ? 2 : 1);
  slot.innerHTML = `
    <div class="prov-stat">
      <div class="lbl">Translated · 30 d</div>
      <div class="v">${stats.chapters_translated_30d.toLocaleString()} <small>chapters</small></div>
      <div class="sparkline">${_sparkSpans(chBuckets)}</div>
    </div>
    <div class="prov-stat">
      <div class="lbl">Spend · 30 d</div>
      <div class="v">${fmtMoney(stats.spend_30d_usd)}</div>
      <div class="sparkline cin">${_sparkSpans(spBuckets, "")}</div>
    </div>
    <div class="prov-stat">
      <div class="lbl">Failure rate</div>
      <div class="v">${failPct}% <small>(${stats.failure_count_30d} of ${stats.attempts_30d})</small></div>
    </div>
    <div class="prov-stat">
      <div class="lbl">Last tested</div>
      <div class="v" style="font-size:14px;">${stats.last_tested_at ? fmtRel(stats.last_tested_at) : "never"}</div>
    </div>
  `;
}

function _renderRoutesSlot(card, routesPayload) {
  const slot = card.querySelector("[data-slot=routes]");
  if (!slot) return;
  if (!routesPayload || !routesPayload.novels || routesPayload.novels.length === 0) {
    slot.innerHTML = `
      <span class="lbl">Routes</span>
      <span class="empty">No novels currently route through this provider.</span>
    `;
    return;
  }
  const chips = routesPayload.novels.map(n => {
    const hue = novelChipHue(n.id);
    return `<a class="novel" href="/reader?novel=${n.id}" title="${escapeHtml(n.title)} · ${escapeHtml(n.role)}">` +
           `<span class="nc${hue ? ` ${hue}` : ""}">${escapeHtml(novelChipInitial(n.title))}</span>` +
           `${escapeHtml(n.title)}</a>`;
  }).join("");
  const overflow = routesPayload.total > routesPayload.novels.length
    ? `<a class="more" href="/library">+ ${routesPayload.total - routesPayload.novels.length} more · all ${routesPayload.total} →</a>`
    : "";
  slot.innerHTML = `
    <span class="lbl">Routes</span>
    <div class="novels">${chips}</div>
    ${overflow}
  `;
}

function _renderLogSlot(card, activity) {
  const slot = card.querySelector("[data-slot=log]");
  if (!slot) return;
  const events = activity?.events || [];
  if (events.length === 0) {
    slot.innerHTML = `
      <div class="l-head">Recent activity</div>
      <div class="empty">No activity yet. First translation will show here.</div>
    `;
    return;
  }
  const _icon = { ok: "○", warn: "△", err: "×" };
  const rows = events.map(ev => {
    const ico = _icon[ev.status] || "·";
    const ms = ev.duration_ms != null
      ? (ev.duration_ms >= 1000
          ? `${(ev.duration_ms / 1000).toFixed(1)} s`
          : `${ev.duration_ms} ms`)
      : "…";
    return `<div class="l-row">
      <span class="t">${escapeHtml(fmtRel(ev.when_iso))}</span>
      <span class="ico ${ev.status}">${ico}</span>
      <span class="msg">${escapeHtml(ev.msg)}</span>
      <span class="ms">${ms}</span>
    </div>`;
  }).join("");
  slot.innerHTML = `<div class="l-head">Recent activity</div>${rows}`;
}

async function renderProviders(providers) {
  if (!providers || providers.length === 0) {
    els.list.innerHTML = `
      <div class="empty-state">
        <p>No providers configured yet.</p>
        <p class="muted">Add one to start translating. The first provider you add becomes the default automatically.</p>
      </div>`;
    _setTocCount("providers", "");
    return;
  }
  els.list.innerHTML = providers.map(renderProviderCardSkeleton).join("");
  _setTocCount("providers", String(providers.length));

  // Fire the three feeds per provider in parallel; render each slot
  // independently so a single failure doesn't break the card.
  providers.forEach(p => {
    const card = els.list.querySelector(`.prov-card[data-id="${p.id}"]`);
    if (!card) return;
    api.providerStats(p.id).then(s => _renderStatsSlot(card, s))
      .catch(() => _renderStatsSlot(card, null));
    api.providerRoutedNovels(p.id, 8).then(r => _renderRoutesSlot(card, r))
      .catch(() => _renderRoutesSlot(card, { novels: [], total: 0 }));
    api.providerActivity(p.id, 6).then(a => _renderLogSlot(card, a))
      .catch(() => _renderLogSlot(card, { events: [] }));
  });
}

async function refresh() {
  try {
    const providers = await api.providers();
    await renderProviders(providers);
  } catch (e) {
    els.list.innerHTML = `<div class="empty-state">Failed to load providers: ${escapeHtml(e.message)}</div>`;
  }
}

async function openCreateDialog() {
  els.dialogTitle.textContent = "Add provider";
  els.form.reset();
  els.fId.value = "";
  await loadCatalog();
  // Default to the first option in the Type dropdown (claude_agent if the
  // catalog is intact — the most accessible option for a fresh user).
  if (!els.fType.value && els.fType.options.length > 0) {
    els.fType.selectedIndex = 0;
  }
  applyTypeDefaults(els.fType.value);
  populateModelSelect(els.fType.value, null);
  els.dialog.showModal();
}

async function openEditDialog(provider) {
  els.dialogTitle.textContent = "Edit provider";
  await loadCatalog();
  els.fId.value = provider.id;
  els.fName.value = provider.name;
  els.fType.value = provider.provider_type;
  els.fBaseUrl.value = provider.base_url || "";
  els.fSecret.value = provider.secret_ref || "";
  // is_default toggled per-card via "Set default" — no form field for it (S3).
  // keepUserValues: preserve the existing base_url / secret_ref that came
  // from the row, even if the catalog has a different default.
  applyTypeDefaults(provider.provider_type, { keepUserValues: true });
  populateModelSelect(provider.provider_type, provider.model_id);
  els.dialog.showModal();
}

// confirmDialog lives in frontend/js/utils.js (C7). settings.js used to
// expose a positional shim — the one remaining caller below now uses the
// canonical named-arg form.

async function handleListClick(e) {
  const card = e.target.closest(".prov-card");
  if (!card) return;
  const id = Number(card.dataset.id);
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (!act) return;

  if (act === "test") {
    const resultEl = card.querySelector("[data-test-result]");
    resultEl.hidden = false;
    resultEl.className = "test-result";
    resultEl.textContent = "Testing…";
    try {
      const r = await api.testProvider(id);
      resultEl.className = `test-result ${r.ok ? "ok" : "err"}`;
      resultEl.textContent = r.message;
      if (r.ok) {
        // Refresh the stats slot so "Last tested" updates without a full reload.
        api.providerStats(id).then(s => _renderStatsSlot(card, s)).catch(() => {});
      }
    } catch (err) {
      resultEl.className = "test-result err";
      resultEl.textContent = err.message;
    }
    return;
  }
  if (act === "set-secret") {
    const secretRef = card.dataset.secretRef || "(secret_ref)";
    await openSecretDialog(id, secretRef);
    return;
  }
  if (act === "set-default") {
    try {
      await api.setDefaultProvider(id);
      await refresh();
    } catch (err) { alert(`Failed to set default: ${err.message}`); }
    return;
  }
  if (act === "edit") {
    try {
      const provider = await api.provider(id);
      openEditDialog(provider);
    } catch (err) { alert(`Failed to load provider: ${err.message}`); }
    return;
  }
  if (act === "delete") {
    const name = card.querySelector(".prov-name")?.textContent?.trim() || "this provider";
    const ok = await confirmDialog({
      title: "Delete provider?",
      body: `<p>${escapeHtml(name)} will be removed. Novels currently set to this provider will fall back to the default (or to legacy env routing if this is the only provider).</p>`,
      okText: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteProvider(id);
      await refresh();
    } catch (err) { alert(`Failed to delete: ${err.message}`); }
    return;
  }
}

async function handleFormSubmit(e) {
  e.preventDefault();
  const id = els.fId.value ? Number(els.fId.value) : null;
  // model_id resolution: if the user picked a curated option, fModelSelect
  // holds the actual ID. If they picked "Other (custom ID)…", the sentinel
  // value is in fModelSelect and the real ID is in the free-text fModel
  // input that became visible. fModel is kept in sync by the select-change
  // handler, so reading it works in both cases.
  const modelId = (els.fModel.value || els.fModelSelect.value || "").trim();
  const fields = {
    name: els.fName.value.trim(),
    provider_type: els.fType.value,
    model_id: modelId,
    base_url: els.fBaseUrl.value.trim() || null,
    secret_ref: els.fSecret.value.trim() || null,
  };
  try {
    if (id == null) {
      // First provider on a fresh install auto-elects as default so
      // chapters can route. After that, default is a list-level choice.
      const isFirstProvider = (els.list.querySelectorAll(".prov-card").length === 0);
      const created = await api.createProvider({ ...fields, is_default: isFirstProvider });
      const keyVal = (els.fSecretValue.value || "").trim();
      if (keyVal && created && created.id != null) {
        try { await api.setProviderSecret(created.id, keyVal); }
        catch (secretErr) {
          alert(`Provider created, but storing the API key failed: ${secretErr.message}\nUse the per-row "Set API key" button to retry.`);
        }
      }
    } else {
      await api.updateProvider(id, fields);
    }
    els.dialog.close();
    els.fSecretValue.value = "";
    await refresh();
  } catch (err) {
    alert(`Save failed: ${err.message}`);
  }
}

// ============================================================
// §03 · Theme miniatures
// ============================================================
const THEMES = [
  { key: "rice",     name: "Rice",     han: "米", meta: "light · jade", swatch: ["#2f6a5e", "#c8423a"] },
  { key: "vellum",   name: "Vellum",   han: "皮", meta: "parchment",    swatch: ["#5a7a4a", "#8a6a2a"] },
  { key: "inkstone", name: "Ink",      han: "墨", meta: "dark · jade",  swatch: ["#6dbfa9", "#b7a878"] },
  { key: "cinnabar", name: "Cinnabar", han: "朱", meta: "dark · red",   swatch: ["#d96962", "#d9b262"] },
  { key: "celadon",  name: "Celadon",  han: "青", meta: "sage",         swatch: ["#2f6a5e", "#8a6a2a"] },
];

function _themeMiniPreview() {
  return `
    <div class="t-preview">
      <div class="mast-mini"><span class="han">籍</span>Your <em>shelf</em></div>
      <div class="body-line">
        He stepped onto the cold marsh <span class="ck-han">凡人</span> · the wind cutting through the reeds.
      </div>
      <div class="pcontrols">
        <span class="chip-mini">Reading</span>
        <span class="chip-mini ghost">Finished</span>
        <span class="badge-locked">鎖 locked</span>
        <span style="flex:1;"></span>
        <span class="btn-mini">＋ Import</span>
      </div>
    </div>`;
}

function renderThemes() {
  const list = document.getElementById("themes-list");
  if (!list) return;
  const current = document.documentElement.dataset.theme;
  const followingSystem =
    typeof window.__themeIsFollowingSystem === "function"
      ? window.__themeIsFollowingSystem()
      : false;

  const cards = THEMES.map(t => {
    const isOn = !followingSystem && t.key === current;
    return `
      <div class="theme-card${isOn ? " on" : ""}" data-theme-preview="${t.key}" data-theme-key="${t.key}">
        <div class="t-head">
          <div class="t-name">
            <span class="han">${t.han}</span>
            ${escapeHtml(t.name)}
            <span class="swatch"><span></span><span></span></span>
          </div>
          ${isOn ? `<span class="t-sel">選</span>` : `<span class="t-meta">${escapeHtml(t.meta)}</span>`}
        </div>
        ${_themeMiniPreview()}
      </div>`;
  });

  cards.push(`
    <div class="theme-card${followingSystem ? " on" : ""}" data-theme-preview="follow-system" data-theme-key="__system__">
      <div class="t-head">
        <div class="t-name">
          <span class="han">隨</span>
          Follow system
          <span class="swatch"><span></span><span></span></span>
        </div>
        ${followingSystem ? `<span class="t-sel">系</span>` : `<span class="t-meta">auto · OS pref</span>`}
      </div>
      ${_themeMiniPreview()}
    </div>`);

  list.innerHTML = cards.join("");
  _setTocCount("themes", String(THEMES.length));
  list.querySelectorAll(".theme-card").forEach(card => {
    card.addEventListener("click", () => {
      const k = card.dataset.themeKey;
      if (k === "__system__") {
        if (window.__followSystemTheme) window.__followSystemTheme();
      } else if (window.__setTheme) {
        window.__setTheme(k);
      }
      renderThemes();
    });
  });
}

// ============================================================
// §04 · Sticky TOC + J/K/Enter navigation
// ============================================================
const _TOC_SECTIONS = ["providers", "opus-mt", "themes", "keyboard", "about"];

function _setTocCount(key, value) {
  const el = els.tocList?.querySelector(`[data-cnt="${key}"]`);
  if (el) el.textContent = value || "";
}

function _wireSectionNav() {
  const sections = _TOC_SECTIONS
    .map(k => document.getElementById(`sec-${k}`))
    .filter(Boolean);
  const links = new Map();
  els.tocList?.querySelectorAll("a[data-sec]").forEach(a => links.set(a.dataset.sec, a));
  const setActive = (key) => {
    links.forEach((a, k) => a.classList.toggle("on", k === key));
  };
  const io = new IntersectionObserver((entries) => {
    const visible = entries.filter(e => e.isIntersecting)
      .sort((a, b) => b.intersectionRatio - a.intersectionRatio);
    if (visible.length === 0) return;
    const key = visible[0].target.id.replace(/^sec-/, "");
    setActive(key);
  }, { rootMargin: "-20% 0px -65% 0px", threshold: [0, 0.25, 0.5] });
  sections.forEach(s => io.observe(s));

  // Keyboard nav: J / K cycle active; Enter jumps to active section.
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const t = document.activeElement;
    if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    if (!["j", "k", "Enter"].includes(e.key)) return;
    const order = _TOC_SECTIONS.filter(k => links.has(k));
    const current = order.find(k => links.get(k).classList.contains("on")) || order[0];
    if (e.key === "Enter") {
      const target = document.getElementById(`sec-${current}`);
      if (target) {
        e.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
      return;
    }
    const idx = order.indexOf(current);
    const next = e.key === "j" ? order[Math.min(order.length - 1, idx + 1)]
                               : order[Math.max(0, idx - 1)];
    if (next && next !== current) {
      e.preventDefault();
      setActive(next);
      const target = document.getElementById(`sec-${next}`);
      target?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });
}

// ============================================================
// §05b · Keyboard reference card (static)
// ============================================================
const _KEYBOARD_REF = [
  { keys: ["⌘", "K"], desc: "Command palette · find anything" },
  { keys: ["G", "L"], desc: "Go to Library" },
  { keys: ["G", "R"], desc: "Open the Reader for the current novel" },
  { keys: ["G", "G"], desc: "Open the Glossary for the current novel" },
  { keys: ["G", "N"], desc: "Open the novel overview page" },
  { keys: ["G", "I"], desc: "Open the Import screen" },
  { keys: ["G", "Q"], desc: "Open the translation Queue" },
  { keys: ["G", "S"], desc: "Open App Settings (this page)" },
  { keys: ["J"], desc: "Next chapter (in reader)" },
  { keys: ["K"], desc: "Previous chapter (in reader)" },
  { keys: ["E"], desc: "Toggle Read / Edit mode (in reader)" },
  { keys: ["B"], desc: "Toggle bilingual view (in reader, read mode)" },
  { keys: ["?"], desc: "Open the command palette inline" },
];

function _renderKeyboardRef() {
  const el = document.getElementById("keyboard-ref");
  if (!el) return;
  const rows = _KEYBOARD_REF.map(({ keys, desc }) => {
    const keyHtml = keys.map((k, i) => {
      const sep = i > 0 ? `<span class="plus">${keys[0] === "G" && k !== keys[0] ? "→" : "+"}</span>` : "";
      return `${sep}<span class="kbd">${escapeHtml(k)}</span>`;
    }).join("");
    return `<div class="keys">${keyHtml}</div><div class="desc">${escapeHtml(desc)}</div>`;
  }).join("");
  el.innerHTML = rows;
}

// ============================================================
// §05c · About / diagnostics card
// ============================================================
let _lastDiagnostics = null;

async function _renderAbout() {
  const list = document.getElementById("about-list");
  if (!list) return;
  try {
    const d = await api.diagnostics();
    _lastDiagnostics = d;
    list.innerHTML = `
      <div class="about-row"><span class="k">Version</span><span class="v">${escapeHtml(d.version)} <span class="ok">${d.frozen ? "· packaged" : "· dev"}</span></span></div>
      <div class="about-row"><span class="k">Python</span><span class="v">${escapeHtml(d.python)}</span></div>
      <div class="about-row"><span class="k">Platform</span><span class="v">${escapeHtml(d.platform)}</span></div>
      <div class="about-row"><span class="k">Data root</span><span class="v">${escapeHtml(d.data_root)}</span></div>
      <div class="about-row"><span class="k">Database</span><span class="v">${escapeHtml(d.db_path)} <span class="ok">· ${fmtBytes(d.db_bytes)}</span></span></div>
      <div class="about-row"><span class="k">Cache</span><span class="v">${fmtBytes(d.cache_bytes)} <span class="ok">· ${d.cache_files.toLocaleString()} files</span></span></div>
      <div class="about-row"><span class="k">Covers</span><span class="v">${fmtBytes(d.covers_bytes)}</span></div>
      <div class="about-row"><span class="k">Library</span><span class="v">${fmtBytes(d.library_bytes)} <span class="ok">total on disk</span></span></div>
      <div class="about-row"><span class="k">Telemetry</span><span class="v serif">${escapeHtml(d.telemetry)} · none collected</span></div>
    `;
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Failed to load diagnostics: ${escapeHtml(e.message)}</div>`;
  }
}

async function _copyDiagnostics() {
  if (!_lastDiagnostics) await _renderAbout();
  if (!_lastDiagnostics) {
    showToast("Diagnostics not loaded yet.");
    return;
  }
  try {
    await navigator.clipboard.writeText(JSON.stringify(_lastDiagnostics, null, 2));
    showToast("Diagnostics copied to clipboard.");
  } catch {
    showToast("Clipboard blocked. Open browser console to copy manually.");
    console.log("LN-Translator diagnostics:", _lastDiagnostics);
  }
}

async function _openLogFolder() {
  try {
    const r = await api.diagnosticsLogFolder();
    // The frozen pywebview build can shell-out to the OS file explorer
    // via window.__openPath if exposed; otherwise we just show the path.
    if (typeof window.__openPath === "function") {
      window.__openPath(r.path);
      showToast(`Opened ${r.path}`);
    } else {
      showToast(`Log folder: ${r.path}`);
    }
  } catch (e) {
    showToast(`Couldn't resolve log folder: ${e.message}`);
  }
}

// ============================================================
// Boot
// ============================================================
document.addEventListener("DOMContentLoaded", () => {
  els.addBtn?.addEventListener("click", openCreateDialog);
  els.dialog?.addEventListener("click", (e) => {
    if (e.target.dataset.act === "cancel") {
      e.preventDefault();
      els.dialog.close();
    }
  });
  els.form?.addEventListener("submit", handleFormSubmit);
  els.list?.addEventListener("click", handleListClick);
  // Type-change drives base_url / secret_ref / model list / install hint
  // refresh via applyTypeDefaults + populateModelSelect.
  els.fType?.addEventListener("change", onTypeChange);
  els.fModelSelect?.addEventListener("change", onModelSelectChange);

  // Warm the catalog cache so the dialog opens instantly the first time.
  loadCatalog();

  refresh();
  renderThemes();
  document.addEventListener("themechange", renderThemes);

  _renderKeyboardRef();
  _renderAbout();
  _renderOpusMTPairs();

  document.getElementById("about-copy-btn")?.addEventListener("click", _copyDiagnostics);
  document.getElementById("about-log-btn")?.addEventListener("click", _openLogFolder);

  _wireSectionNav();
});


// ---------------------------------------------------------------------------
// OPUS-MT pair management (free tier)
// ---------------------------------------------------------------------------
//
// Lists supported pairs, surfaces install state, drives Download / Remove.
// Downloads stream via SSE on /api/opus-mt/pairs/{pair}/status; the panel
// updates inline as bytes arrive. A new download replaces the panel's
// "Download" button with a progress bar; Remove deletes the on-disk dir.

async function _renderOpusMTPairs() {
  const host = document.getElementById("opus-mt-pairs");
  if (!host) return;
  try {
    const res = await fetch("/api/opus-mt/pairs");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const pairs = await res.json();
    if (!pairs.length) {
      host.innerHTML = `<div class="empty-state">No OPUS-MT pairs available.</div>`;
      return;
    }
    host.innerHTML = pairs.map(p => `
      <div class="provider-card" data-pair="${p.pair}">
        <div class="provider-card-head">
          <div class="pcard-title">${escapeHtml(p.pair)} <span class="muted">${escapeHtml(p.source_language)} → ${escapeHtml(p.target_language)}</span></div>
          <div class="pcard-meta">
            ${p.installed
              ? `<span class="chip ok">Installed · ${p.size_mb_installed} MB</span>`
              : `<span class="chip warn">Not downloaded · ~${p.size_mb_expected} MB</span>`}
          </div>
        </div>
        <div class="provider-card-foot">
          <div class="opus-mt-progress" id="opus-mt-progress-${p.pair}"></div>
          ${p.installed
            ? `<button type="button" class="btn-secondary" data-action="remove" data-pair="${p.pair}">Remove</button>`
            : `<button type="button" class="btn-primary" data-action="download" data-pair="${p.pair}">↓ Download</button>`}
        </div>
      </div>
    `).join("");
    host.querySelectorAll("button[data-action]").forEach(btn => {
      btn.addEventListener("click", _onOpusMTAction);
    });
  } catch (e) {
    host.innerHTML = `<div class="empty-state err">Failed to load OPUS-MT pairs: ${escapeHtml(String(e))}</div>`;
  }
}

async function _onOpusMTAction(ev) {
  const btn = ev.currentTarget;
  const pair = btn.dataset.pair;
  const action = btn.dataset.action;
  btn.disabled = true;
  if (action === "remove") {
    btn.textContent = "Removing…";
    try {
      const res = await fetch(`/api/opus-mt/pairs/${pair}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) {
      alert(`Remove failed: ${e}`);
    }
    _renderOpusMTPairs();
    return;
  }
  // action === "download"
  btn.textContent = "Starting…";
  const progEl = document.getElementById(`opus-mt-progress-${pair}`);
  try {
    const res = await fetch(`/api/opus-mt/pairs/${pair}/download`, { method: "POST" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (e) {
    alert(`Download failed to start: ${e}`);
    btn.disabled = false;
    btn.textContent = "↓ Download";
    return;
  }
  // Subscribe to the SSE progress stream.
  const stream = new EventSource(`/api/opus-mt/pairs/${pair}/status`);
  stream.onmessage = (msg) => {
    let data;
    try { data = JSON.parse(msg.data); } catch { return; }
    const total = data.bytes_total || 0;
    const done = data.bytes_done || 0;
    const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
    if (data.phase === "downloading") {
      progEl.innerHTML = `<span class="muted">Downloading · ${pct}% (${_humanBytes(done)} / ${_humanBytes(total)})</span>`;
    } else if (data.phase === "verifying") {
      progEl.innerHTML = `<span class="muted">Verifying checksum…</span>`;
    } else if (data.phase === "extracting") {
      progEl.innerHTML = `<span class="muted">Extracting…</span>`;
    } else if (data.phase === "done") {
      progEl.innerHTML = `<span class="chip ok">Done</span>`;
      stream.close();
      _renderOpusMTPairs();
    } else if (data.phase === "error") {
      progEl.innerHTML = `<span class="chip err">Failed: ${escapeHtml(data.detail || "unknown error")}</span>`;
      stream.close();
      btn.disabled = false;
      btn.textContent = "↻ Retry";
    }
  };
  stream.onerror = () => {
    stream.close();
    progEl.innerHTML = `<span class="chip err">Connection lost. Reload to retry.</span>`;
  };
}

function _humanBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}
