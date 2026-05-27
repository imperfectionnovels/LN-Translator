(function () {
  // Reader typography preferences (font size + line height). Saved as numbers
  // in localStorage; applied as inline CSS vars on :root so every page picks
  // them up (other pages have less text, so the effect is subtle, but body
  // copy stays consistent). The reader's settings dialog updates these.
  const FS_BODY_MIN = 13, FS_BODY_MAX = 24;
  const LH_MIN = 1.3, LH_MAX = 2.1;
  function applyReaderType() {
    const fs = parseFloat(localStorage.getItem("readerFsBody") || "");
    const lh = parseFloat(localStorage.getItem("readerFsLh") || "");
    if (Number.isFinite(fs) && fs >= FS_BODY_MIN && fs <= FS_BODY_MAX) {
      document.documentElement.style.setProperty("--fs-body", fs + "px");
    }
    if (Number.isFinite(lh) && lh >= LH_MIN && lh <= LH_MAX) {
      document.documentElement.style.setProperty("--fs-body-lh", String(lh));
    }
  }
  applyReaderType();
  window.__applyReaderType = applyReaderType;
  window.__readerTypeRanges = { FS_BODY_MIN, FS_BODY_MAX, LH_MIN, LH_MAX };

  // Read the canonical theme list from boot.js (C19). The fallback array
  // exists only for the (unreachable) case where theme.js loads without
  // boot.js — keep it byte-identical to boot.js so the two never diverge.
  const VALID = window.__THEMES || ["rice", "vellum", "inkstone", "cinnabar", "celadon"];
  // System-preference fallback: if the user hasn't explicitly chosen a theme,
  // map (prefers-color-scheme: dark) → "inkstone" so a dark-OS user lands on
  // the dark theme on first visit instead of always defaulting to "rice".
  function pickDefault() {
    try {
      if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
        return "inkstone";
      }
    } catch { /* matchMedia missing — fall through */ }
    return "rice";
  }
  const stored = localStorage.getItem("theme");
  const initial = VALID.includes(stored) ? stored : pickDefault();
  document.documentElement.setAttribute("data-theme", initial);

  function syncButtons() {
    document.querySelectorAll("[data-theme-pick] button").forEach(b => {
      b.classList.toggle("on", b.dataset.themeVal === document.documentElement.dataset.theme);
    });
  }

  // Reusable setter for any future theme picker (Phase 5 Settings/Themes
  // panel calls this from a click handler). Returns true on a valid change,
  // false when the value isn't one of the supported themes. The legacy
  // delegated click handler below still works for any existing
  // [data-theme-pick] markup — this is an additive escape hatch, not a
  // replacement.
  window.__setTheme = function (v) {
    if (!VALID.includes(v)) return false;
    document.documentElement.setAttribute("data-theme", v);
    localStorage.setItem("theme", v);
    syncButtons();
    document.dispatchEvent(new CustomEvent("themechange", { detail: { theme: v } }));
    return true;
  };
  // 2026-05-25: Follow-system option. Clears the explicit pick and
  // re-applies the OS-derived theme via pickDefault(); the existing
  // matchMedia change-listener below then auto-follows future OS theme
  // changes. The settings Themes panel surfaces this as a sixth card
  // labelled "Follow system" and marks it active when localStorage.theme
  // is absent. Returns the theme key that's now in effect.
  window.__followSystemTheme = function () {
    localStorage.removeItem("theme");
    const t = pickDefault();
    document.documentElement.setAttribute("data-theme", t);
    syncButtons();
    document.dispatchEvent(new CustomEvent("themechange", { detail: { theme: t, follow_system: true } }));
    return t;
  };
  window.__themeIsFollowingSystem = function () {
    return !localStorage.getItem("theme");
  };
  window.__themes = VALID;

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-theme-pick] button");
    if (!btn) return;
    const v = btn.dataset.themeVal;
    if (!VALID.includes(v)) return;
    document.documentElement.setAttribute("data-theme", v);
    localStorage.setItem("theme", v);
    syncButtons();
  });

  // Follow OS theme changes as long as the user hasn't explicitly picked.
  try {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener?.("change", () => {
      if (localStorage.getItem("theme")) return;
      const t = mq.matches ? "inkstone" : "rice";
      document.documentElement.setAttribute("data-theme", t);
      syncButtons();
    });
  } catch { /* ignore */ }

  document.addEventListener("DOMContentLoaded", syncButtons);
  syncButtons();
})();
