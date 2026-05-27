// Shared frontend utilities — deliberate single home for generic helpers that
// otherwise drift across page-specific JS files. Add new utilities here when
// the second page needs them; do NOT inline-copy a helper into a page script.
//
// Loaded as a plain <script> before the page-specific JS on every HTML page,
// so the exported names live on `window` and are available to the IIFEs in
// reader.js / glossary.js / library.js / home.js.

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[c]);
}

// C7: single canonical confirm dialog. Replaces six local copies that had
// drifted into three different signatures (named-args with/without meta, a
// positional `confirmDialog(title, body, okLabel)` in settings.js, and a
// no-arg `askConfirm()` in queue-panel.js).
//
// Lazily inserts a <dialog id="confirm-dialog"> if the page doesn't ship
// one. That lets the import page, find-replace, stats and the global
// queue-panel pill all share this implementation without each carrying
// per-page dialog markup.
//
// Args: {title, body, meta?, okText?, cancelText?, danger?}.
//   body / meta accept HTML strings (callers control escaping).
//   cancelText: "" hides the cancel button (OK-only message dialog).
//   danger:    paints the OK button with the danger-confirm class.
// Returns: Promise<boolean>. True if OK, false if Cancel / Esc.
function _ensureConfirmDialog() {
  let dlg = document.getElementById("confirm-dialog");
  if (!dlg) {
    dlg = document.createElement("dialog");
    dlg.id = "confirm-dialog";
    dlg.className = "dialog";
    dlg.setAttribute("aria-labelledby", "confirm-dialog-title");
    dlg.innerHTML = `
      <h3 id="confirm-dialog-title">Confirm</h3>
      <div class="dialog-body" id="confirm-dialog-body"></div>
      <div class="dialog-meta hidden" id="confirm-dialog-meta"></div>
      <div class="dialog-actions">
        <button type="button" class="btn-ghost" data-act="cancel">Cancel</button>
        <button type="button" class="btn-primary" data-act="ok">Confirm</button>
      </div>`;
    document.body.appendChild(dlg);
  }
  // Tolerate pages whose existing dialog predated the meta slot.
  if (!dlg.querySelector("#confirm-dialog-meta")) {
    const metaDiv = document.createElement("div");
    metaDiv.id = "confirm-dialog-meta";
    metaDiv.className = "dialog-meta hidden";
    const body = dlg.querySelector("#confirm-dialog-body");
    body?.parentNode.insertBefore(metaDiv, body.nextSibling);
  }
  return dlg;
}
function confirmDialog({
  title = "Confirm",
  body = "",
  meta = "",
  okText = "Confirm",
  cancelText = "Cancel",
  danger = false,
} = {}) {
  const dlg = _ensureConfirmDialog();
  const titleEl = dlg.querySelector("#confirm-dialog-title");
  const bodyEl = dlg.querySelector("#confirm-dialog-body");
  const metaEl = dlg.querySelector("#confirm-dialog-meta");
  const okBtn = dlg.querySelector('[data-act="ok"]');
  const cancelBtn = dlg.querySelector('[data-act="cancel"]');
  titleEl.textContent = title;
  bodyEl.innerHTML = body;
  if (metaEl) {
    metaEl.innerHTML = meta || "";
    metaEl.classList.toggle("hidden", !meta);
  }
  okBtn.textContent = okText;
  cancelBtn.textContent = cancelText;
  // Empty okText / cancelText hides that button. Glossary's "More" menu
  // uses `okText: ""` to make this a Close-only dialog whose body buttons
  // own all the actions.
  okBtn.classList.toggle("hidden", !okText);
  cancelBtn.classList.toggle("hidden", !cancelText);
  okBtn.classList.toggle("danger-confirm", !!danger);
  return new Promise(resolve => {
    function cleanup() {
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      dlg.removeEventListener("cancel", onCancelEvt);
      dlg.removeEventListener("keydown", onKey);
    }
    const onOk = () => { cleanup(); dlg.close(); resolve(true); };
    const onCancel = () => { cleanup(); dlg.close(); resolve(false); };
    const onCancelEvt = () => { cleanup(); resolve(false); };
    const onKey = (e) => {
      if (e.key === "Enter" && !e.shiftKey && e.target.tagName !== "TEXTAREA") {
        e.preventDefault();
        onOk();
      }
    };
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    dlg.addEventListener("cancel", onCancelEvt);
    dlg.addEventListener("keydown", onKey);
    dlg.showModal();
    okBtn.focus();
  });
}
window.confirmDialog = confirmDialog;

// C8: <details>-as-menu (reader's util-menu, glossary's download-menu, etc.)
// didn't close when the user clicked outside the menu. The [open] state
// persisted indefinitely, and the bfcache could leave the menu open after a
// back-navigation. Document-level mousedown + Esc handlers close any open
// menu whose toggle wasn't the click target, and a MutationObserver mirrors
// [open] to aria-expanded on the <summary> so screen-readers track the
// disclosure.
const _MENU_DETAILS_SEL = "details.util-menu[open], details.download-menu[open]";
document.addEventListener("mousedown", (e) => {
  document.querySelectorAll(_MENU_DETAILS_SEL).forEach(d => {
    if (!d.contains(e.target)) d.open = false;
  });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    document.querySelectorAll(_MENU_DETAILS_SEL).forEach(d => { d.open = false; });
  }
});
new MutationObserver(muts => {
  for (const m of muts) {
    if (m.attributeName !== "open") continue;
    const d = m.target;
    if (!d.matches || !d.matches("details.util-menu, details.download-menu")) continue;
    const sum = d.querySelector("summary");
    if (sum) sum.setAttribute("aria-expanded", d.open ? "true" : "false");
  }
}).observe(document.documentElement, { attributes: true, subtree: true, attributeFilter: ["open"] });

// C4: the decorative han glyphs (.han / .tab-han) and the eyebrow "h" letter
// (.eyebrow .h) all read out under NVDA/VoiceOver as raw CJK or single-letter
// noise. Anchor labels live in sibling .lbl / aria-label, so the glyph spans
// are purely visual and should be hidden from assistive tech.
//
// The static HTML spans are also patched at the source, but page-specific JS
// (glossary.js, library.js, reader.js, settings.js) renders more spans into
// the DOM over time. A MutationObserver lets us catch those without making
// every emitter remember the attribute.
function _markDecorativeGlyph(el) {
  if (!el || el.getAttribute("aria-hidden") === "true") return;
  el.setAttribute("aria-hidden", "true");
}
// Selector is deliberately narrow. The bare `.han` class is also used for
// CONTENT glyphs (the original-CN chapter title in the reader masthead, the
// glossary term in its han-eyebrow, the library generated-cover stand-in).
// Hiding those would mute real text. So we only mark glyphs in contexts
// where the surrounding text already carries the meaning:
//   - spine nav      (anchor has aria-label)
//   - tab buttons    (button label follows the glyph)
//   - eyebrow icon   (`<span class="h">入</span><span>Import</span>`)
//   - TOC eyebrow    (settings page "On this page")
//   - inventory cell (label lives in `.lbl`)
//   - status badge   (badge text says "locked"/"auto")
const _DECORATIVE_GLYPH_SEL =
  ".ink-spine .han, .tab-han, .eyebrow > .h, .toc-eyebrow .han, " +
  ".inv-cell .han, .badge .han";
function _sweepDecorativeGlyphs(root) {
  (root || document).querySelectorAll(_DECORATIVE_GLYPH_SEL)
    .forEach(_markDecorativeGlyph);
}
_sweepDecorativeGlyphs();
document.addEventListener("DOMContentLoaded", () => _sweepDecorativeGlyphs());
new MutationObserver(records => {
  for (const r of records) {
    for (const n of r.addedNodes) {
      if (n.nodeType !== 1) continue;
      if (n.matches && n.matches(_DECORATIVE_GLYPH_SEL)) _markDecorativeGlyph(n);
      _sweepDecorativeGlyphs(n);
    }
  }
}).observe(document.documentElement, { childList: true, subtree: true });
