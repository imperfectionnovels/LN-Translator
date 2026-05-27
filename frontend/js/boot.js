// Single-source FOUC bootstrap. Loaded synchronously in <head> before the
// stylesheet on every HTML page. Replaces 11 copies of an inline IIFE that
// had drifted out of sync (reader.html alone carried the focus-mode branch).
//
// Also publishes the canonical theme list on window.__THEMES so theme.js,
// the Settings palette, and the Cmd-K palette don't each maintain their own.
(function () {
  var THEMES = ["rice", "vellum", "inkstone", "cinnabar", "celadon"];
  window.__THEMES = THEMES;

  var stored;
  try { stored = localStorage.getItem("theme"); } catch (e) { stored = null; }
  var t;
  if (THEMES.indexOf(stored) >= 0) {
    t = stored;
  } else if (window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches) {
    t = "inkstone";
  } else {
    t = "rice";
  }
  document.documentElement.setAttribute("data-theme", t);

  // Reader focus-mode pre-paint. Previously only reader.html ran this, but
  // routing the constant through one bootstrap means we can stop maintaining
  // a bespoke <head> script there.
  try {
    if (localStorage.getItem("readerFocusMode") === "1") {
      document.documentElement.setAttribute("data-focus-mode", "1");
    }
  } catch (e) { /* ignore */ }

  // R7: pin the mobile-TOC default before the stylesheet renders so the
  // drawer doesn't flash open for a frame on phones. Reader CSS keys off
  // [data-toc-init]; desktop promotes it back to "on" inside a media query.
  if (window.matchMedia && matchMedia("(max-width: 900px)").matches) {
    document.documentElement.setAttribute("data-toc-init", "off");
  } else {
    document.documentElement.setAttribute("data-toc-init", "on");
  }
})();
