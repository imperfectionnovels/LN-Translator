// Shared frontend utilities: deliberate single home for generic helpers that
// otherwise drift across page-specific JS files. Add new utilities here when
// the second page needs them; do NOT inline-copy a helper into a page script.
//
// Loaded as a plain <script> before the page-specific JS on every HTML page,
// so the exported names live on `window` and are available to the IIFEs in
// reader.js / glossary.js / library.js / home.js.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[c]);
}

// Clipboard copy that survives non-secure origins. navigator.clipboard is only
// exposed in a SECURE CONTEXT (https, localhost, file://). On the desktop the
// app is localhost (or the pywebview WebView2 host), so it works there. But
// when the reader is opened from a phone over the LAN the origin is plain
// http://<pc-ip>:<port>, which is NOT secure, so navigator.clipboard is
// undefined and writeText is a no-op. Every copy button relied on it, with
// `?.` swallowing the absence and a success toast firing anyway, so on the
// phone the button looked like it worked but copied nothing.
//
// Strategy: use the async Clipboard API when it is genuinely available, else
// fall back to a temporary <textarea> + document.execCommand("copy"), which
// still works on http and on mobile (iOS needs the contentEditable+range
// dance below). Returns true only on a real success so callers can show an
// honest toast.
function _legacyCopyText(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  // Keep it on-screen-but-invisible: position:fixed + tiny size avoids the
  // page scrolling to it, and 16px font stops iOS from zooming on focus.
  ta.style.cssText =
    "position:fixed;top:0;left:0;width:1px;height:1px;padding:0;border:0;" +
    "outline:0;box-shadow:none;background:transparent;font-size:16px;opacity:0;";
  document.body.appendChild(ta);

  // Preserve whatever the user had selected (the selection-popover copy path
  // operates on a live selection) so we can restore it afterwards.
  const sel = document.getSelection();
  const savedRange = sel && sel.rangeCount > 0 ? sel.getRangeAt(0) : null;

  const isIOS = /ipad|iphone|ipod/i.test(navigator.userAgent);
  if (isIOS) {
    // iOS Safari ignores textarea.select(); it needs an editable element with
    // a real Range selection plus setSelectionRange.
    ta.contentEditable = "true";
    ta.readOnly = false;
    const range = document.createRange();
    range.selectNodeContents(ta);
    sel.removeAllRanges();
    sel.addRange(range);
    ta.setSelectionRange(0, text.length);
  } else {
    ta.select();
  }

  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }

  document.body.removeChild(ta);
  if (savedRange && sel) {
    sel.removeAllRanges();
    sel.addRange(savedRange);
  }
  return ok;
}

async function copyText(text) {
  text = String(text ?? "");
  if (!text) return false;
  // Only trust the async API in a secure context; outside one, writeText
  // either is missing or rejects, so go straight to the legacy path.
  if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      // Permission denied / lost focus: fall through to the legacy path.
    }
  }
  return _legacyCopyText(text);
}
window.copyText = copyText;

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

// Audit 6.6: single canonical toast. Replaces six per-page copies that had
// drifted (durations 4s to 6s, three different element ids). Fixed position
// (bottom center), fixed 5s duration, optional Undo slot.
let _appToastTimer = null;
function _ensureToastEl() {
  let el = document.getElementById("app-toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "app-toast";
    el.className = "app-toast";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.hidden = true;
    document.body.appendChild(el);
  }
  return el;
}
function showToast(message, kind = "info", undoAction = null) {
  const el = _ensureToastEl();
  el.dataset.kind = kind;
  el.innerHTML = `<span>${escapeHtml(message)}</span>` +
    (undoAction ? `<button type="button" data-toast-undo>Undo</button>` : "");
  el.hidden = false;
  if (_appToastTimer) clearTimeout(_appToastTimer);
  _appToastTimer = setTimeout(() => { el.hidden = true; _appToastTimer = null; }, 5000);
  if (undoAction) {
    el.querySelector("[data-toast-undo]")?.addEventListener("click", () => {
      try { undoAction(); } finally {
        if (_appToastTimer) clearTimeout(_appToastTimer);
        el.hidden = true;
        _appToastTimer = null;
      }
    }, { once: true });
  }
}
window.showToast = showToast;

// Block 2 (2026-08-07): shared cross-tab bus over the BroadcastChannel
// "novel-changes". Replaces three hand-rolled copies (home.js, editor-tools.js
// insert-chapter, reader-chapter.js's raw onmessage) that had each drifted
// into a slightly different message shape.
//
// Message shape: {novel_id: Number|null, type}. novel_id === null means "all
// novels" (the global glossary page has no single novel to scope to, but a
// global-entry edit can still change any novel's rendered text). Known types
// in use: "appended" / "inserted" (the chapter list changed) and "glossary"
// (glossary data changed, and chapter text may have been rewritten in place).
//
// Loop-free by construction: per the BroadcastChannel spec, the channel
// object that calls postMessage never receives its own message (delivery
// only reaches OTHER BroadcastChannel objects of the same name/origin), so
// no sender-id bookkeeping is needed to avoid a self-triggered refresh.
let _novelBus = null;
try { _novelBus = new BroadcastChannel("novel-changes"); } catch (_) { _novelBus = null; }

function broadcastNovelChange(novelId, type) {
  if (!_novelBus) return;
  try {
    _novelBus.postMessage({ novel_id: novelId == null ? null : Number(novelId), type });
  } catch (_) { /* ignore */ }
}
window.broadcastNovelChange = broadcastNovelChange;

// Registers fn(data) for "novel-changes" messages that match novelId. A
// listener's own novelId filter only rejects a message when BOTH the
// listener's novelId and the message's novel_id are non-null and differ.
// A null message novel_id (a global-glossary change) matches every listener,
// and a listener registered with novelId=null receives everything.
function onNovelChange(novelId, fn) {
  if (!_novelBus) return;
  const wanted = novelId == null ? null : Number(novelId);
  _novelBus.addEventListener("message", (e) => {
    const data = e.data || {};
    const msgNovelId = data.novel_id == null ? null : Number(data.novel_id);
    if (wanted != null && msgNovelId != null && wanted !== msgNovelId) return;
    fn(data);
  });
}
window.onNovelChange = onNovelChange;

// Shared toast copy for a glossary apply-in-place response (Block 2,
// 2026-08-07). Every apply surface (glossary page, editor term form, reader
// mini form) renders the same honest sentence instead of drifting into three
// different phrasings. Only claims a restore point when snapshot_ids is
// actually non-empty (a novel's payload can exceed the snapshot size cap),
// never promise an undo the backend didn't record.
function glossaryApplyToastText(res) {
  res = res || {};
  const n = res.chapters_updated || 0;
  let text = `Renamed in ${n} chapter${n === 1 ? "" : "s"}.`;
  const skipped = (res.skipped_translating || 0) + (res.skipped_refining || 0);
  if (skipped > 0) {
    text += ` ${skipped} skipped (in flight), re-apply after they finish.`;
  }
  if (res.snapshot_ids && res.snapshot_ids.length) {
    text += " Restore point saved to Find and Replace history.";
  }
  return text;
}
window.glossaryApplyToastText = glossaryApplyToastText;

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
