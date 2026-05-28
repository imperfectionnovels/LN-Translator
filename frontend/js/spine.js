(function () {
  // Canonical spine markup — stamped into every page's <aside class="ink-spine">
  // (C2). Was duplicated across 10 HTML pages with byte-level drift: glossary
  // and queue used "—" in the seal aria-label, settings dropped aria-label on
  // nav links, three pages pre-set aria-current="page" by hand and the others
  // relied on this script to compute it. One source kills that drift.
  //
  // The script still owns the runtime concerns it used to: marking the active
  // glyph for the current page, and resolving per-novel Reader / Glossary
  // links from the novel currently in the URL (or the last one opened).
  // C14: a 5-swatch theme picker in the foot was previously only reachable
  // via Settings → Themes. Surfacing it here makes the five-theme system
  // visible/discoverable without dominating the rail. theme.js owns the
  // click delegation (data-theme-pick / data-theme-val) and the .on sync,
  // so this is markup-only — no extra JS in spine.js.
  const THEME_SWATCHES = (window.__THEMES || ["rice", "vellum", "inkstone", "cinnabar", "celadon"])
    .map(t => `<button type="button" data-theme-val="${t}" title="${t[0].toUpperCase() + t.slice(1)} theme" aria-label="${t[0].toUpperCase() + t.slice(1)} theme"></button>`)
    .join("");

  const SPINE_HTML = `
  <a class="seal" href="/library" title="LN Translator" aria-label="LN Translator · Library">譯</a>
  <nav class="spine-nav" aria-label="Primary">
    <a data-nav="library" href="/library" title="Library" aria-label="Library"><span class="han">籍</span><span class="lbl">Library</span></a>
    <a data-nav="reader" id="reader-link" href="/library" title="Reader" aria-label="Reader"><span class="han">讀</span><span class="lbl">Reader</span></a>
    <a data-nav="glossary" id="glossary-link" href="/library" title="Glossary" aria-label="Glossary"><span class="han">詞</span><span class="lbl">Glossary</span></a>
    <a data-nav="import" href="/" title="Import" aria-label="Import"><span class="han">入</span><span class="lbl">Import</span></a>
    <a data-nav="queue" href="/queue" title="Queue" aria-label="Queue"><span class="han">列</span><span class="lbl">Queue</span></a>
  </nav>
  <div class="spine-foot">
    <div class="spine-theme-picker" data-theme-pick role="group" aria-label="Theme">${THEME_SWATCHES}</div>
    <div class="studio-rule" aria-hidden="true"></div>
    <div class="studio-label">Studio</div>
    <nav class="spine-nav" aria-label="Studio">
      <a data-nav="settings" href="/settings" title="App Settings" aria-label="App Settings"><span class="han">設</span><span class="lbl">App Settings</span></a>
    </nav>
  </div>`;

  const aside = document.querySelector("aside.ink-spine");
  // Onboarding intentionally has no spine container — bail there. On every
  // other page, fill the (now-empty) container with the canonical markup.
  // We also tolerate legacy inline markup: if the aside has any children we
  // wipe them first, so the canonical block always wins.
  if (!aside) return;
  aside.innerHTML = SPINE_HTML;

  const params = new URLSearchParams(location.search);
  const urlNovel = params.get("novel");
  if (urlNovel) {
    try { localStorage.setItem("ink:lastNovel", urlNovel); } catch (e) { /* ignore */ }
  }
  let novelId = urlNovel;
  if (!novelId) {
    try { novelId = localStorage.getItem("ink:lastNovel"); } catch (e) { /* ignore */ }
  }

  const path = location.pathname;
  const page =
    path.indexOf("/reader") === 0   ? "reader"   :
    path.indexOf("/glossary") === 0 ? "glossary" :
    path.indexOf("/library") === 0  ? "library"  :
    path.indexOf("/queue") === 0    ? "queue"    :
    path.indexOf("/settings") === 0 ? "settings" : "import";

  // theme.js's syncButtons() ran before our markup existed, so re-mark the
  // active theme swatch here. Future clicks are handled by theme.js's
  // delegated listener.
  const currentTheme = document.documentElement.getAttribute("data-theme");
  aside.querySelectorAll(".spine-theme-picker button").forEach(b => {
    b.classList.toggle("on", b.dataset.themeVal === currentTheme);
  });

  aside.querySelectorAll("[data-nav]").forEach((el) => {
    const nav = el.dataset.nav;
    const active = nav === page;
    el.classList.toggle("on", active);
    if (active) el.setAttribute("aria-current", "page");

    if (el.tagName === "A" && (nav === "reader" || nav === "glossary")) {
      if (novelId) {
        // Omit &ch so the reader resumes on its persisted last-read chapter
        // (lastRead:<novelId>) instead of always forcing chapter 1; the
        // reader treats a missing ch param as "no explicit chapter".
        el.href = nav === "reader"
          ? "/reader?novel=" + novelId
          : "/glossary?novel=" + novelId;
        el.classList.remove("disabled");
      } else {
        el.href = "/library";
        el.classList.add("disabled");
        el.title = "Open a novel first";
      }
    }
  });
})();
