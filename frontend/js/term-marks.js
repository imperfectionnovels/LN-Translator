/* ---- Shared glossary term marks (2026-08-07) ----
 *
 * Builds the inline "this phrase is a glossary term" spans that both reading
 * surfaces render: the reader's chapter body / aligned grid and the CAT
 * editor's segment cells. One matcher, one span shape, one title format, so a
 * term looks and behaves the same wherever it appears.
 *
 * SELF-CONTAINED BY CONTRACT. This module owns its own esc() and touches no
 * page global (glossaryCache, currentData, novelId, escapeHtml) and no DOM API
 * whatsoever: it takes strings plus a plain entries array and returns strings.
 * That is why it can load on both pages and why it deliberately sits OUTSIDE
 * the reader boot-safety lint's module concatenation
 * (backend/tests/test_reader_js_boot_safety.py discovers reader*.js only).
 * Keep it that way: the moment this file reads a page global, the two callers
 * stop being interchangeable.
 *
 * EN boundaries are explicit lookarounds, not \b:
 *     (?<![A-Za-z0-9_]) ... (?![A-Za-z0-9_])
 * At a word-character edge that is exactly what \b tests, so ordinary aliases
 * behave identically. At a NON-word edge \b flips its test and demands that
 * the NEIGHBORING character be a word character, which normal prose never
 * supplies: "Su Nü", "talent(s)", or any alias ending in "." could never
 * match, silently, on both surfaces. The lookarounds degrade to "not glued to
 * a word character", which is the intended semantics. Lookbehind is a V8
 * feature; this app runs in Chromium / WebView2 only and targets no other
 * engine.
 *
 * Apostrophes get their own tolerance, because one alias faces three surfaces:
 * glossary rows store the straight ', prose usually carries the curly U+2019,
 * and the sanitized haystack carries the &#39; entity that marked writes for a
 * straight quote. Every apostrophe position in an alias therefore expands to
 * an alternation over those forms, and the byEn key folds all three back onto
 * ' so a hit resolves to the entry it came from. Without it "Saint's Plunder"
 * could never match in the reader, and longest-first ordering handed the mark
 * to a shorter sibling ("Saint"): wrong tooltip, and the revise dialog opened
 * on the wrong glossary row.
 *
 * Two accepted misses in wrapSanitizedHtml (the already-sanitized-HTML path):
 *   1. A term split across inline tags is not matched. Markdown emphasis can
 *      cut a term in half ("Nine *Yang* Art"), leaving no contiguous text run
 *      to match. Rare, and the alternative (matching across tag boundaries)
 *      cannot produce valid nesting.
 *   2. A term containing one of the OTHER HTML-special characters (& < > ")
 *      is not matched. The haystack there is escaped text, so a literal "&"
 *      in a glossary term faces "&amp;" in the stream. Apostrophes are handled
 *      (above); the remaining four do not occur in cultivation terminology in
 *      practice.
 */

const TermMarks = (() => {
  const _ENT = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

  // Private escape: this module never borrows utils.js::escapeHtml, so it can
  // load on a page that has not loaded utils.js yet.
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => _ENT[c]);
  }

  function escRe(s) {
    return String(s).replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&");
  }

  // Word-edge boundaries as lookarounds instead of \b. See the header: \b
  // inverts its test at a non-word edge, so any alias whose first or last
  // character is outside [A-Za-z0-9_] could never fire.
  const EN_LEFT = "(?<![A-Za-z0-9_])";
  const EN_RIGHT = "(?![A-Za-z0-9_])";

  // Apostrophe surfaces an alias position may face. The RAW haystack is real
  // chapter text (straight or curly only); the SANITIZED haystack is escaped
  // text, where a straight quote arrives as "&#39;". They stay two separate
  // alternations on purpose: raw chapter text can legitimately contain the
  // literal characters "&#39;", so folding the entity into the raw pattern
  // would let a term match a piece of prose that is not that term.
  const APOS_RAW = "(?:'|’)";
  const APOS_HTML = "(?:&#39;|'|’)";
  const APOS_SPLIT = /['’]/;

  // One alias to one regex fragment: literal parts escaped, apostrophe
  // positions widened to the alternation for this haystack.
  function aliasFragment(a, aposAlt) {
    return String(a).split(APOS_SPLIT).map(escRe).join(aposAlt);
  }

  // byEn key / matched-surface key. Both sides normalize through here, so a
  // hit spelled with the curly apostrophe or the &#39; entity still resolves
  // to the entry whose term_en used the straight one.
  function enKey(s) {
    return String(s == null ? "" : s)
      .replace(/&#0*39;/g, "'")
      .replace(/’/g, "'")
      .toLowerCase();
  }

  // Slash-split aliases: "筑基 / 築基" matches either surface form. Mirrors
  // reader-glossary.js::_glossAliases, editor-tools.js::zhAliases and the
  // backend's slash-alternative handling.
  function aliases(s) {
    return String(s || "").split("/").map(x => x.trim()).filter(Boolean);
  }

  function termClass(entry) {
    const cat = entry && entry.category;
    return (cat === "technique" || cat === "item") ? "term alt" : "term";
  }

  function titleFor(entry) {
    if (!entry) return "";
    const cat = entry.category ? ` · ${entry.category}` : "";
    return `${entry.term_zh || ""} ↔ ${entry.term_en || ""}${cat}`;
  }

  /* Cached on the entries ARRAY REFERENCE identity. The old reader rebuilt two
   * ~2000-alternative unions on every render (each chapter nav, each toggle).
   * Every producer replaces the array wholesale (loadGlossary, create, update),
   * so a stale pattern cannot outlive the data it was built from; invalidate()
   * covers any future site that mutates an array in place. */
  let _cache = null;
  let _cacheKey = null;

  function invalidate() {
    _cache = null;
    _cacheKey = null;
  }

  function buildPattern(entries) {
    if (!Array.isArray(entries) || !entries.length) return null;
    if (_cacheKey === entries) return _cache;

    // Flat alias lists across ALL entries, so longest-first ordering can let a
    // long alias of one entry beat a short alias of another ("Master Yan"
    // beats "Yan"), which per-entry sorting could not do.
    const enList = [];
    const zhList = [];
    const byEn = new Map();
    const byZh = new Map();

    for (const g of entries) {
      if (!g) continue;
      for (const a of aliases(g.term_en)) {
        // Single characters match far too much English prose to be useful.
        if (a.length < 2) continue;
        enList.push(a);
        const k = enKey(a);
        if (!byEn.has(k)) byEn.set(k, g);
      }
      for (const a of aliases(g.term_zh)) {
        // Chinese has no word boundaries the regex could anchor on, so a
        // single-character alias fires inside unrelated compounds (tian inside
        // idioms, dao inside verbs), pure noise in the reading view; also inert
        // for prompt purposes.
        if (a.length < 2) continue;
        zhList.push(a);
        if (!byZh.has(a)) byZh.set(a, g);
      }
    }

    enList.sort((a, b) => b.length - a.length);
    zhList.sort((a, b) => b.length - a.length);

    // Case-insensitive on the English side: a term stored lowercase may appear
    // title-cased at a sentence start (and the reverse). Only MATCHING is
    // case-folded; the rendered span always keeps the prose's own casing.
    //
    // TWO compiled EN variants, both built from the SAME sorted alias list so
    // longest-first ordering is identical on either haystack: `en` runs over
    // raw text (wrapText), `enHtml` over already-escaped text
    // (wrapSanitizedHtml). They differ only in the apostrophe alternation.
    const enRegex = aposAlt => new RegExp(
      EN_LEFT + "(" + enList.map(a => aliasFragment(a, aposAlt)).join("|") + ")" + EN_RIGHT,
      "gi",
    );
    const en = enList.length ? enRegex(APOS_RAW) : null;
    const enHtml = enList.length ? enRegex(APOS_HTML) : null;
    // No boundaries on the Chinese side: the ASCII word-character class the
    // lookarounds test would never fire between two Han characters.
    const zh = zhList.length
      ? new RegExp("(" + zhList.map(escRe).join("|") + ")", "g")
      : null;

    const result = (!en && !zh) ? null : { en, enHtml, zh, byEn, byZh };
    _cache = result;
    _cacheKey = entries;
    return result;
  }

  function lookup(pattern, matched, side) {
    if (!pattern) return null;
    return side === "en"
      ? pattern.byEn.get(enKey(matched))
      : pattern.byZh.get(matched);
  }

  function span(entry, matchedHtml) {
    return `<span class="${termClass(entry)}" data-entry-id="${entry.id}"`
      + ` title="${esc(titleFor(entry))}">${matchedHtml}</span>`;
  }

  /* RAW (unescaped) input. Runs the raw-variant regex over the raw text so the
   * boundary lookarounds see real characters (and the apostrophe alternation
   * stays off the &#39; entity, which raw prose can carry literally), then
   * escapes every piece on the way out.
   * Deliberately emits NO <br> and NO <p>: block structure belongs to the
   * caller, which lets one helper serve both the plain reading pane and the
   * aligned grid's source cells. */
  function wrapText(text, side, pattern) {
    const src = String(text == null ? "" : text);
    const re = pattern && pattern[side];
    if (!re) return esc(src);
    const out = [];
    let lastIdx = 0;
    let m;
    re.lastIndex = 0;
    while ((m = re.exec(src))) {
      const matched = m[0];
      // Zero-length match backstop: without this the loop never advances.
      if (!matched) { re.lastIndex += 1; continue; }
      if (m.index > lastIdx) out.push(esc(src.slice(lastIdx, m.index)));
      const entry = lookup(pattern, matched, side);
      out.push(entry ? span(entry, esc(matched)) : esc(matched));
      lastIdx = m.index + matched.length;
    }
    if (lastIdx < src.length) out.push(esc(src.slice(lastIdx)));
    return out.join("");
  }

  /* ALREADY-SANITIZED HTML (marked + DOMPurify output). Split on tags, leave
   * every tag piece untouched, run the English regex over the text pieces
   * only, so <strong>/<em>/<br> markup cannot be corrupted.
   *
   * The matched text is inserted RAW here, unlike wrapText. That asymmetry is
   * deliberate and load-bearing: these pieces are ALREADY escaped, so passing
   * them through esc() again would double-escape entities ("&amp;" becoming
   * "&amp;amp;"). The title attribute still goes through esc() because it is
   * built from glossary data, not from the sanitized stream.
   *
   * Runs the `enHtml` variant, not `en`: this haystack spells a straight
   * apostrophe "&#39;". */
  function wrapSanitizedHtml(html, pattern) {
    const src = String(html == null ? "" : html);
    const re = pattern && (pattern.enHtml || pattern.en);
    if (!re) return src;
    return src.split(/(<[^>]+>)/).map(seg => {
      if (!seg) return "";
      if (seg.startsWith("<")) return seg;
      const out = [];
      let lastIdx = 0;
      let m;
      re.lastIndex = 0;
      while ((m = re.exec(seg))) {
        const matched = m[0];
        if (!matched) { re.lastIndex += 1; continue; }
        if (m.index > lastIdx) out.push(seg.slice(lastIdx, m.index));
        const entry = lookup(pattern, matched, "en");
        out.push(entry ? span(entry, matched) : matched);
        lastIdx = m.index + matched.length;
      }
      if (lastIdx < seg.length) out.push(seg.slice(lastIdx));
      return out.join("");
    }).join("");
  }

  return { buildPattern, wrapText, wrapSanitizedHtml, invalidate };
})();
