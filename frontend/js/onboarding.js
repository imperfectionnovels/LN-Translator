/* Design v2 Phase G — Welcome wizard.
 *
 * Three steps: pick translator, add key (skippable for Claude Agent),
 * land. Creates ONE provider row through the existing /api/providers
 * endpoint and optionally stores its secret via /api/providers/{id}
 * /set-secret. Stamps config_kv.first_run_complete=1 on completion so
 * app_entry.py routes to / on next launch.
 *
 * Skip-for-now (top-right) does NOT mark first_run_complete — the user
 * is leaving without finishing, so a follow-up launch should drop them
 * back into the wizard. To actually leave the wizard for good, the
 * user has to either complete it or click into Settings and configure
 * a provider there (the legacy path).
 */

(function () {
  const panels = document.querySelectorAll(".ob-panel");
  const steps = document.querySelectorAll(".ob-step");
  let activeStep = 1;
  let selected = null; // {providerType, model, secretRef, name, hue, glyph}
  let createdProviderId = null;
  let createdSignature = null; // "type::model" of the row we created, to detect a changed selection

  const PROVIDER_LABEL = {
    claude_agent: { name: "Claude Agent SDK", glyph: "C", hue: "#c8423a" },
    claude_cli:   { name: "Claude CLI",        glyph: "$", hue: "#a83a32" },
    gemini:       { name: "Google Gemini",     glyph: "G", hue: "#4a6da8" },
    deepseek:     { name: "DeepSeek",          glyph: "D", hue: "#2f6a5e" },
    openai_compatible: { name: "OpenAI-compatible", glyph: "O", hue: "#3a6e60" },
    google_translate_free: { name: "Google Translate (free)", glyph: "免", hue: "#8a6a3a" },
  };

  // Curated one-liners shown under each provider card. Falls back to a
  // generic line if a future provider type isn't recognized — keeps the
  // wizard self-explaining when /api/providers/catalog grows.
  const PROVIDER_NOTE = {
    claude_agent: "No key needed. Runs through your installed <code>claude</code> CLI. Best quality, serial.",
    claude_cli:   "Subprocess wrapper. Useful if the Agent SDK isn't installed but the CLI is.",
    gemini:       "Fast and cheap. Needs a Google AI Studio API key.",
    deepseek:     "Internal translate→revise pass built in. Needs a DeepSeek API key.",
    openai_compatible: "Point at any OpenAI-compatible endpoint (vLLM, Together, etc.). Bring your own URL + key.",
  };

  function applyStep(step) {
    activeStep = step;
    panels.forEach(p => p.classList.toggle("is-active", Number(p.dataset.panel) === step));
    steps.forEach(s => {
      const n = Number(s.dataset.step);
      s.classList.toggle("is-active", n === step);
      s.classList.toggle("is-done", n < step);
    });
  }

  // O1: each step transition pushes a history entry so the browser back
  // button rolls back through the wizard instead of jumping out entirely
  // (or stranding the user mid-step). popstate restores activeStep from
  // the saved state object.
  function goto(step) {
    if (step === activeStep) return;
    const url = step > 1 ? `${location.pathname}#step-${step}` : location.pathname;
    history.pushState({ step }, "", url);
    applyStep(step);
  }
  window.addEventListener("popstate", (e) => {
    const step = (e.state && e.state.step) || _stepFromHash() || 1;
    applyStep(step);
  });
  function _stepFromHash() {
    const m = (location.hash || "").match(/step-(\d+)/);
    return m ? Number(m[1]) : null;
  }
  // Seed the initial history entry with the step we booted into (1 by
  // default, or whatever a deep-link hash names) so the first back press
  // doesn't leap to the previous page silently.
  const _initialStep = _stepFromHash() || 1;
  history.replaceState({ step: _initialStep }, "", location.href);
  if (_initialStep !== 1) applyStep(_initialStep);

  /* ---- Step 1 selection ---- */
  document.getElementById("ob-provider-grid")?.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-provider-type]");
    if (btn) _selectFromButton(btn);
  });

  function _selectFromButton(btn) {
    document.querySelectorAll(".ob-provider").forEach(b => b.classList.toggle("is-selected", b === btn));
    const type = btn.dataset.providerType;
    selected = {
      providerType: type,
      model: btn.dataset.model,
      secretRef: btn.dataset.secretRef || "",
      baseUrl: btn.dataset.baseUrl || "",
      authCommand: btn.dataset.authCommand || "",
      name: PROVIDER_LABEL[type]?.name || type,
      hue: PROVIDER_LABEL[type]?.hue || "var(--accent)",
      glyph: PROVIDER_LABEL[type]?.glyph || "·",
    };
  }

  // Default selection mirrors the .is-selected anchor in HTML (the no-network
  // fallback). Once the catalog fetch lands we re-render the grid and re-
  // select the first card.
  (function preselect() {
    const initial = document.querySelector(".ob-provider.is-selected");
    if (initial) _selectFromButton(initial);
  })();

  // C18: catalog-driven provider grid. The 3-button HTML stays as a no-
  // network fallback; on successful fetch we replace it so newly-added
  // provider types (e.g. openai_compatible) show up here without an HTML
  // edit. google_translate_free IS shown here — it's the "no API key
  // needed" first-run option, and the user can upgrade to an LLM later.
  async function _hydrateProviderGrid() {
    const grid = document.getElementById("ob-provider-grid");
    if (!grid) return;
    try {
      const res = await fetch("/api/providers/catalog");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const catalog = await res.json();
      if (!Array.isArray(catalog) || catalog.length === 0) return;
      // Only show types the wizard can fully configure: a non-empty curated
      // model list (an empty model_id would 422) and an auth path the wizard
      // handles (subscription, or an API key with a built-in default base
      // URL). The generic openai_compatible type needs a user-supplied base
      // URL the wizard does not collect, so it is omitted here; it stays
      // available on the Settings page.
      const usable = catalog.filter(e => Array.isArray(e.models) && e.models.length > 0);
      if (!usable.length) return;
      grid.innerHTML = "";
      for (const entry of usable) {
        const meta = PROVIDER_LABEL[entry.type] || { name: entry.display || entry.type, glyph: "·", hue: "var(--accent)" };
        const defaultModel = entry.models?.[0]?.id || "";
        const modelDisplay = entry.models?.[0]?.display || defaultModel || entry.display;
        const note = PROVIDER_NOTE[entry.type] || (entry.auth === "subscription"
          ? `No API key required. Uses your local ${meta.name} subscription.`
          : `Cloud API · needs a ${meta.name} key.`);
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ob-provider";
        btn.dataset.providerType = entry.type;
        btn.dataset.model = defaultModel;
        btn.dataset.secretRef = entry.secret_ref_hint || "";
        btn.dataset.baseUrl = entry.base_url_default || "";
        btn.dataset.authCommand = entry.auth_command || "";
        btn.innerHTML = `
          <div class="ob-prov-icon" style="--prov-hue:${meta.hue}">${meta.glyph}</div>
          <div class="ob-prov-name">${meta.name}</div>
          <div class="ob-prov-meta">${escapeHtml(modelDisplay)} · ${escapeHtml(entry.group || "")}</div>
          <div class="ob-prov-note">${note}</div>`;
        grid.appendChild(btn);
      }
      const first = grid.querySelector(".ob-provider");
      if (first) _selectFromButton(first);
    } catch (err) {
      console.warn("provider catalog fetch failed, using hardcoded fallback:", err);
    }
  }
  _hydrateProviderGrid();

  /* ---- Navigation actions ---- */
  document.body.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-act]");
    if (!t) return;
    const act = t.dataset.act;
    if (act === "next") return onContinueFromStep1();
    if (act === "back") return goto(activeStep - 1);
    if (act === "save-key") return onSaveKey();
    if (act === "skip-key") return onSkipKey();
  });

  async function onContinueFromStep1() {
    if (!selected) return;
    const sig = `${selected.providerType}::${selected.model}`;
    // Create the provider row first — needed for both the key step
    // (provider id is required for set-secret) and the summary step.
    if (!createdProviderId) {
      try {
        const created = await api.createProvider({
          name: selected.name,
          provider_type: selected.providerType,
          model_id: selected.model,
          secret_ref: selected.secretRef || null,
          base_url: selected.baseUrl || null,
          is_default: true,
        });
        createdProviderId = created.id;
        createdSignature = sig;
      } catch (err) {
        // The most common failure is the UNIQUE(name) constraint —
        // probably a returning user re-running the wizard. Surface
        // gracefully instead of stranding them.
        await confirmDialog({ title: "Couldn't create the provider", body: `<p>${escapeHtml(err.message)}</p><p>If you've already configured this provider, use Settings to make changes.</p>`, okText: "OK", cancelText: "" });
        return;
      }
    } else if (createdSignature !== sig) {
      // The user went Back and picked a different provider after a row was
      // already created. Update that row to match so the key step and the
      // summary act on the current selection, not the stale one.
      try {
        await api.updateProvider(createdProviderId, {
          name: selected.name,
          provider_type: selected.providerType,
          model_id: selected.model,
          secret_ref: selected.secretRef || null,
          base_url: selected.baseUrl || null,
        });
        createdSignature = sig;
      } catch (err) {
        await confirmDialog({ title: "Couldn't update the provider", body: `<p>${escapeHtml(err.message)}</p><p>Use Settings to make changes.</p>`, okText: "OK", cancelText: "" });
        return;
      }
    }
    populateStep2();
    goto(2);
  }

  function populateStep2() {
    const title = document.getElementById("ob-step2-title");
    const lead = document.getElementById("ob-step2-lead");
    const label = document.getElementById("ob-key-input-label");
    const hint = document.getElementById("ob-key-hint");
    const saveBtn = document.getElementById("ob-save-key");
    const skipBtn = document.getElementById("ob-skip-key");
    const input = document.getElementById("ob-key-input");
    input.value = "";

    if (!selected.secretRef) {
      // Subscription / no-key provider (Claude Agent SDK, a CLI subscription
      // type, or the free tier). Auth happens out-of-band, so there is no key
      // to configure here.
      title.textContent = "No key needed";
      lead.textContent = `You picked ${selected.name}, which authenticates out-of-band (a local subscription or the free tier). There's no API key to configure here.`;
      label.textContent = "API key (not needed for this provider)";
      input.disabled = true;
      input.placeholder = `Not used for ${selected.name}`;
      hint.innerHTML = selected.authCommand
        ? `If you haven't yet, run <code>${escapeHtml(selected.authCommand)}</code> once in a terminal so the subscription is logged in. Then continue.`
        : `If this provider needs a one-time login, do it in a terminal first. Then continue.`;
      saveBtn.textContent = "Continue →";
      saveBtn.dataset.act = "skip-key";
      skipBtn.hidden = true;
    } else {
      title.textContent = `Add your ${selected.name} key`;
      lead.textContent = `Your key is stored in the OS keychain under ${selected.secretRef}. Never written to the database or returned in API responses.`;
      label.textContent = `${selected.name} API key`;
      input.disabled = false;
      input.placeholder = "paste your key here";
      hint.innerHTML = selected.providerType === "gemini"
        ? `Get a free key from <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noopener">aistudio.google.com</a>.`
        : selected.providerType === "deepseek"
        ? `Get a key from <a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noopener">platform.deepseek.com</a>.`
        : "";
      saveBtn.textContent = "Save & continue →";
      saveBtn.dataset.act = "save-key";
      skipBtn.hidden = false;
    }
  }

  async function onSaveKey() {
    const input = document.getElementById("ob-key-input");
    const status = document.getElementById("ob-key-status");
    const skipBtn = document.getElementById("ob-skip-key");
    if (!selected.secretRef) {
      // No-key providers fall through immediately — but still verify the
      // local subscription works via api.testProvider.
      return onSkipKey();
    }
    const val = (input.value || "").trim();
    if (!val) {
      status.classList.remove("hidden", "ok");
      status.classList.add("err");
      status.textContent = "Paste a key, or click Skip to set it later in Settings.";
      return;
    }
    try {
      await api.setProviderSecret(createdProviderId, val);
    } catch (err) {
      status.classList.remove("hidden", "ok");
      status.classList.add("err");
      if (err.status === 503) {
        // OS keychain unavailable (no Secret Service on Linux, a locked
        // keychain, or keyring import failed). The key was NOT stored, and
        // in a packaged app there's no terminal to set an env var in. Keep
        // Skip visible so the user isn't trapped on this step.
        status.textContent = "Your OS keychain isn't available, so the key couldn't be stored. You can finish setup now and add the key later in Settings, or click \"Skip · set the key later\".";
        if (skipBtn) skipBtn.hidden = false;
      } else {
        status.textContent = `Couldn't store key: ${err.message}`;
      }
      return;
    }
    // Validate the provider config before advancing. NOTE: this is a config
    // check (known type, non-empty model, resolvable secret), NOT a live API
    // round-trip, so a present-but-wrong key still passes here and only
    // surfaces at first translate. The copy reflects that.
    status.classList.remove("hidden", "err", "ok");
    status.textContent = "Checking configuration…";
    try {
      const r = await api.testProvider(createdProviderId);
      if (!r || !r.ok) {
        status.classList.remove("ok");
        status.classList.add("err");
        status.textContent = `Configuration check failed: ${r?.message || "unknown error"}. Fix it and try again, or click Skip to continue.`;
        return;
      }
      status.classList.remove("err");
      status.classList.add("ok");
      status.textContent = "Key saved. It will be verified on your first translation.";
      populateStep3();
      setTimeout(() => goto(3), 350);
    } catch (err) {
      status.classList.remove("ok");
      status.classList.add("err");
      status.textContent = `Configuration check failed: ${err.message}. Fix it and try again, or click Skip to continue.`;
    }
  }

  async function onSkipKey() {
    // For no-secret providers (Claude Agent SDK) still test once — verifies
    // the local CLI / subscription is logged in. Failure surfaces inline
    // but offers a "Continue anyway" path (the user might be offline now
    // and intend to fix it later).
    const status = document.getElementById("ob-key-status");
    if (!selected.secretRef) {
      status.classList.remove("hidden", "err", "ok");
      status.textContent = "Checking configuration…";
      try {
        const r = await api.testProvider(createdProviderId);
        if (!r || !r.ok) {
          status.classList.remove("ok");
          status.classList.add("err");
          const loginHint = selected.authCommand
            ? `Run "${selected.authCommand}" once in a terminal to log in, then come back.`
            : "Make sure this provider is logged in,";
          status.textContent = `Configuration check failed: ${r?.message || "unknown error"}. ${loginHint} Or continue anyway and fix it later.`;
          _showContinueAnyway();
          return;
        }
        status.classList.remove("err");
        status.classList.add("ok");
        status.textContent = "Configuration looks valid. It will be verified on your first translation.";
      } catch (err) {
        status.classList.remove("ok");
        status.classList.add("err");
        status.textContent = `Configuration check failed: ${err.message}. You can continue anyway and fix it later in App Settings.`;
        _showContinueAnyway();
        return;
      }
    }
    populateStep3();
    goto(3);
  }

  function _showContinueAnyway() {
    // Promote the existing Skip button into a "Continue anyway" so the
    // user has an explicit escape hatch when the test fails for reasons
    // outside the wizard's control (offline, CLI not installed yet).
    const skipBtn = document.getElementById("ob-skip-key");
    if (!skipBtn) return;
    skipBtn.hidden = false;
    skipBtn.textContent = "Continue anyway →";
    skipBtn.dataset.act = "force-continue";
  }

  // Force-continue is the "Continue anyway" path: bypass the inline test
  // entirely and just advance. Used after a test failure when the user
  // wants to push past it (e.g. offline at install time).
  document.body.addEventListener("click", (e) => {
    const t = e.target.closest("[data-act='force-continue']");
    if (!t) return;
    populateStep3();
    goto(3);
  });

  async function populateStep3() {
    // Stamp first_run_complete so the next EXE launch lands on /
    // directly. Best-effort — a 503 here shouldn't trap the user on
    // this screen.
    try {
      const res = await fetch("/api/config/first_run_complete", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: "1" }),
      });
      if (!res.ok) {
        console.warn("first_run_complete not stamped (HTTP", res.status + ") — the wizard may reappear on next launch.");
      }
    } catch (e) {
      console.warn("first_run_complete stamp failed — the wizard may reappear on next launch.", e);
    }

    const sum = document.getElementById("ob-summary");
    sum.innerHTML = `
      <div class="ob-sum-card">
        <div class="ob-prov-icon" style="--prov-hue:${selected.hue}">${selected.glyph}</div>
        <div>
          <div class="ob-sum-title">${selected.name}</div>
          <div class="ob-sum-sub">model: <code>${selected.model}</code>${selected.secretRef ? ` · key: <code>${selected.secretRef}</code>` : ""}</div>
        </div>
      </div>`;
  }
})();
