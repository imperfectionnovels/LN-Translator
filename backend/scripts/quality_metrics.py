"""Rule-category compliance scorers for a chapter translation (tracked port).

Promoted out of the gitignored ``data/ww_metrics.py`` one-off so the measurement
IP is version-controlled and importable. The heuristics are transcribed
verbatim: the point is a STABLE scorer whose numbers stay comparable across
prompt versions, not a better scorer. Tune here only with the same discipline as
a prompt change (single-variable, ground-truth diff) -- otherwise old reports
become incomparable.

The scorers are pure: they take chapter text + source + a glossary list and
return dataclasses / dicts. No DB, no stdout. ``quality_report.py`` is the
orchestrator that loads data, calls these, aggregates, and persists. The old
``data/ww_metrics.py`` can become a thin CLI shim importing ``score_text``.

Three families, mirroring the original sections:
  - ``surface_metrics``  : register tells (semicolons, contractions, archaisms)
  - ``flow_metrics``     : cohesion descriptors (anchors, opener variety, CV)
  - ``rule_category_scores`` : the 12 compliance categories (violations /
    reviews / opportunities + quoted examples), the matrix the report scores on.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from backend.models import GlossaryEntry

# ---------------------------------------------------------------------------
# constants (verbatim from data/ww_metrics.py)
# ---------------------------------------------------------------------------

ARCHAIC = [
    "not one whit", "one whit", " he who ", " she who ", "though it was,",
    "though it were", "better that it", "dared not", "glad of it",
    "i should think", "scarcely", "naught", "whence", "thrice",
]
ANCHORS = [
    "the next second", "a moment later", "the next moment",
    "an instant later", "in the next second", "a second later",
    "the next instant", "in the next instant",
]
CONTR = re.compile(
    r"\b\w+n't\b|\b\w+['’](?:ll|re|ve|d|m)\b|\b(?:it|that|there|what|he|she|who)['’]s\b",
    re.IGNORECASE,
)

PERIOD_BANS = ["slew", "suffused", "beheld", "amidst", "whilst"]
AI_TELL_BANS = [
    "delve", "delved", "delving", "tapestry", "myriad",
    "navigate", "navigated", "harness", "harnessed",
]

STOCK_PHRASES = {
    "话音刚落": [
        "the words were barely out", "as he finished speaking",
        "had barely finished", "the words had barely", "as soon as he finished",
        "as his words fell", "as the words fell",
    ],
    "话音未落": [
        "the words were barely out", "before he had finished",
        "had not yet finished", "as his words fell", "as the words fell",
    ],
}

FLOW_ANCHORS = [
    "at this", "at these words", "at the thought", "at that moment",
    "at this moment", "just then", "in a flash", "in an instant",
    "the next second", "the next moment", "the next instant", "suddenly",
    "a moment later", "an instant later", "hearing this", "at the sight",
]
CALQUE_TELLS = ["as his words fell", "as the words fell", "as her words fell"]

TITLE_KEYWORDS = [
    "True Person", "True Monarch", "True Lord", "True Venerable", "Dao Lord",
    "Heavenly Immortal", "Sect Master", "Hall Master", "Palace Master",
    "Patriarch", "Ancestor", "True Immortal", "Demon Lord", "Holy Maiden",
]

PRO_DROP_OPENERS = re.compile(
    r"^(?:Might as well|Better to|Best to|No use|No point|Time to|Had better|"
    r"Can't have|Couldn't very well|Have to admit|Got to)\b"
)

SUBORDINATORS = {
    "and", "but", "or", "so", "for", "yet", "nor", "because", "when", "while",
    "as", "if", "though", "although", "that", "which", "who", "whom", "whose",
    "where", "after", "before", "until", "unless", "since", "then", "once",
    "whether", "than", "like",
}

PRONOUNS = {"he", "she", "it", "they", "i", "we", "you", "his", "her", "its", "their"}
SCENE_HEADER_RE = re.compile(r"^[A-Z][\w' -]{0,30}\.$")


# ---------------------------------------------------------------------------
# shared text helpers (verbatim)
# ---------------------------------------------------------------------------


def sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[\"'“‘*]?[A-Z])", flat)
    return [p for p in (s.strip() for s in parts) if p]


def quote_split_semicolons(text: str) -> tuple[int, int]:
    inq = outq = 0
    open_q = False
    for ch in text:
        if ch == '"':
            open_q = not open_q
        elif ch == ";":
            if open_q:
                inq += 1
            else:
                outq += 1
    return inq, outq


def _norm_token(tok: str) -> str:
    t = tok.strip("\"'“”‘’*").lower()
    if not t:
        return ""
    if t in PRONOUNS:
        return "PRON"
    if tok[:1].isupper() and t not in ("the", "a", "an", "but", "and", "so", "then"):
        return "NAME"
    return t


def _aliases(entry: GlossaryEntry) -> list[tuple[str, str]]:
    from backend.services.glossary import split_aliases  # noqa: PLC0415
    return [
        (zh.strip(), en.strip())
        for zh, en in split_aliases(entry.term_zh or "", entry.term_en or "")
        if zh.strip() and en.strip()
    ]


def _context(text: str, pos: int, width: int = 70) -> str:
    lo = max(0, pos - width // 2)
    return text[lo:lo + width].replace("\n", " / ")


def _en_variants(en: str) -> list[str]:
    return [v.strip() for v in en.split("/") if v.strip()]


def _variant_re(variant: str) -> re.Pattern:
    words = [re.escape(w) for w in variant.split()]
    return re.compile(r"\b" + r"[ -]".join(words) + r"(?:e?s)?\b", re.IGNORECASE)


def _find_variant(en: str, text: str) -> tuple[str, re.Match | None]:
    for v in _en_variants(en):
        m = _variant_re(v).search(text)
        if m:
            return v, m
    return en, None


# ---------------------------------------------------------------------------
# category accumulator (the Cat class, minus emit())
# ---------------------------------------------------------------------------


@dataclass
class CategoryScore:
    name: str
    violations: int = 0
    reviews: int = 0
    opportunities: int = 0
    examples: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def hit(self, example: str, n: int = 1) -> None:
        self.violations += n
        if len(self.examples) < 8:
            self.examples.append(example.strip()[:180])

    def review(self, example: str, n: int = 1) -> None:
        self.reviews += n
        if len(self.examples) < 8:
            self.examples.append("REVIEW: " + example.strip()[:170])

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "violations": self.violations,
            "reviews": self.reviews,
            "opportunities": self.opportunities,
            "examples": list(self.examples),
            "detail": dict(self.detail),
        }


# ---------------------------------------------------------------------------
# surface + flow descriptors
# ---------------------------------------------------------------------------


def surface_metrics(text: str, source: str | None = None) -> dict:
    words = len(text.split())
    k = words / 1000.0
    sents = sentences(text)
    lens = [len(s.split()) for s in sents]
    semis = text.count(";")
    colons = text.count(":")
    contr = CONTR.findall(text)
    low = text.lower()
    arch = {a: low.count(a) for a in ARCHAIC if low.count(a)}
    over40 = sum(1 for n in lens if n > 40)
    under6 = sum(1 for n in lens if n < 6)
    inq, outq = quote_split_semicolons(text)
    out = {
        "words": words,
        "sentences": len(sents),
        "mean_words_per_sentence": sum(lens) / max(len(lens), 1),
        "semicolons": semis,
        "semicolons_per_1k": semis / max(k, 0.001),
        "colons": colons,
        "colons_per_1k": colons / max(k, 0.001),
        "contractions": len(contr),
        "contractions_per_1k": len(contr) / max(k, 0.001),
        "archaic_tells": sum(arch.values()),
        "archaic_detail": arch,
        "sentences_over_40w": over40,
        "sentences_under_6w": under6,
        "semicolons_in_quotes": inq,
        "semicolons_out_quotes": outq,
    }
    if source:
        zh_sents = len(re.findall(r"[。！？]", source))
        out["source_sentences"] = zh_sents
        out["en_zh_sentence_ratio"] = len(sents) / max(zh_sents, 1)
    out["longest_sentences"] = [
        {"words": n, "text": s[:140]}
        for n, s in sorted(zip(lens, sents), reverse=True)[:3]
    ]
    return out


def flow_metrics(text: str) -> dict:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    all_sents = sentences(text)
    low_paras = [p.lower() for p in paras]

    anchored = []
    for lp in low_paras:
        head = lp.lstrip("\"'“”‘’*")[:30]
        for a in FLOW_ANCHORS:
            if head.startswith(a):
                anchored.append(a)
                break
    rate = len(anchored) / max(len(paras), 1)
    variety = len(set(anchored)) / max(len(anchored), 1)

    openers = [_norm_token(s.split()[0]) if s.split() else "" for s in all_sents]
    max_run = run = 1
    for a, b in zip(openers, openers[1:]):
        run = run + 1 if a == b and a else 1
        max_run = max(max_run, run)

    bigrams = []
    for s in all_sents:
        ws = s.split()
        if len(ws) >= 2:
            bigrams.append((_norm_token(ws[0]), ws[1].lower().strip(',."')))
    bg_variety = len(set(bigrams)) / max(len(bigrams), 1)

    cvs = []
    for p in paras:
        ls = [len(s.split()) for s in sentences(p)]
        if len(ls) >= 3:
            mean = sum(ls) / len(ls)
            var = sum((x - mean) ** 2 for x in ls) / len(ls)
            if mean:
                cvs.append((var ** 0.5) / mean)

    linked = eligible = 0
    for i in range(1, len(paras)):
        if SCENE_HEADER_RE.match(paras[i]) or SCENE_HEADER_RE.match(paras[i - 1]):
            continue
        eligible += 1
        head = [w.strip(',."\'“”‘’*!?').lower() for w in paras[i].split()[:8]]
        if any(w in ("this", "that", "it", "these", "those", "at", "then", "but", "so", "and") for w in head[:3]):
            linked += 1
            continue
        prev_tail = {w.strip(',."\'“”‘’*!?').lower() for w in paras[i - 1].split()[-12:] if len(w) > 3}
        if any(w in prev_tail for w in head):
            linked += 1

    return {
        "paragraphs": len(paras),
        "anchor_rate": rate,
        "anchored_count": len(anchored),
        "anchor_variety": variety,
        "anchors_seen": sorted(set(anchored))[:8],
        "max_same_opener_run": max_run,
        "opening_bigram_variety": bg_variety,
        "sentence_len_cv": sum(cvs) / max(len(cvs), 1),
        "sentence_len_cv_paras": len(cvs),
        "given_link_rate": linked / max(eligible, 1),
        "given_link_eligible": eligible,
    }


# ---------------------------------------------------------------------------
# rule-category compliance (the 12 categories)
# ---------------------------------------------------------------------------


def rule_category_scores(
    text: str, source: str, glossary: list[GlossaryEntry]
) -> list[CategoryScore]:
    from backend.services.text_observers import body_correctness_observations  # noqa: PLC0415

    low = text.lower()
    sents = sentences(text)
    out: list[CategoryScore] = []

    live: list[tuple] = []
    for g in glossary:
        for zh, en in _aliases(g):
            if len(zh) >= 2 and zh in source:
                live.append((zh, en, g))

    # 1. glossary observers
    c = CategoryScore("glossary_observers")
    c.opportunities = len(live)
    for hit in body_correctness_observations(source, text, glossary):
        c.hit(hit)
    out.append(c)

    # 2. glossary presence
    c = CategoryScore("glossary_presence")
    seen_zh = set()
    for zh, en, g in live:
        if g.category == "idiom" or zh in seen_zh:
            continue
        seen_zh.add(zh)
        c.opportunities += 1
        _, m = _find_variant(en, text)
        if m is None:
            msg = f"{zh} -> {en!r} absent (zh x{source.count(zh)}, cat={g.category}, locked={int(g.locked)})"
            if g.category == "other" and en == en.lower():
                c.review(msg)
            else:
                c.hit(msg)
    out.append(c)

    # 3. glossary casing
    c = CategoryScore("glossary_casing")
    for zh, en, g in live:
        if g.category == "idiom":
            continue
        variant, m = _find_variant(en, text)
        if m is None:
            continue
        c.opportunities += 1
        note_txt = ((g.notes or "") + " " + (g.usage_note or "")).lower()
        if variant != variant.lower():
            if "lowercase" in note_txt:
                continue
            cs = re.compile(r"\b" + r"[ -]".join(re.escape(w) for w in variant.split()) + r"(?:e?s)?\b")
            if not cs.search(text):
                c.hit(f"{variant!r} never appears with its given casing: ...{_context(text, m.start())}...")
        else:
            cap = re.compile(r"\b" + r"[ -]".join(re.escape(w) for w in (variant[0].upper() + variant[1:]).split()) + r"(?:e?s)?\b")
            for mm in cap.finditer(text):
                pre = text[max(0, mm.start() - 2):mm.start()]
                if not re.search(r'(?:^|[.!?“"‘\'\n:]\s*$|^\s*$)', pre):
                    c.hit(f"lowercase entry {variant!r} force-cased mid-sentence: ...{_context(text, mm.start())}...")
    out.append(c)

    # 4. epithet / full-title frequency
    c = CategoryScore("epithet_frequency")
    for zh, en, g in live:
        if not any(k in en for k in TITLE_KEYWORDS):
            continue
        longer_zh = [z2 for z2, _, _ in live if z2 != zh and zh in z2]
        longer_en = [e2 for _, e2, _ in live if e2 != en and en in e2]
        zh_n = source.count(zh) - sum(source.count(z2) for z2 in set(longer_zh))
        en_n = text.count(en) - sum(text.count(e2) for e2 in set(longer_en))
        if zh_n < 2:
            continue
        c.opportunities += 1
        if en_n > zh_n:
            c.hit(f"{en!r} sounded MORE than source: en={en_n} zh={zh_n}")
        elif zh_n >= 4 and en_n >= zh_n:
            c.review(f"{en!r} never pronominalized: en={en_n} zh={zh_n}")
    out.append(c)

    # 5. thought formatting + thought subjects
    c = CategoryScore("thought_format")
    zh_thoughts = len(re.findall(
        r"心想|暗道|心道|暗想|寻思|腹诽|心中暗",
        source,
    ))
    c.opportunities = zh_thoughts + source.count("‘")
    italic_spans = re.findall(r"\*([^*\n]{2,200})\*", text)
    if zh_thoughts >= 2 and not italic_spans:
        c.review(
            f"source marks {zh_thoughts} thoughts but output has zero italic spans "
            "(italics-for-all-thought policy)"
        )
    for span in italic_spans + sents:
        s = span.lstrip('*"“‘ ')
        if PRO_DROP_OPENERS.match(s):
            c.review(f"subjectless thought opener: {s[:120]}")
    out.append(c)

    # 6. sentence shape
    c = CategoryScore("sentence_shape")
    c.opportunities = len(sents)
    for s in sents:
        w = s.rstrip(".!?…").split()
        if len(w) <= 2 and s.endswith(".") and not s.isupper() and re.match(r"^[A-Za-z' ]+\.$", s):
            c.hit(f"stranded stub: {s}")
    splice_re = re.compile(
        r"(\w+),\s+(?:he|she|it|they|we|you|I)\s+"
        r"(?:was|were|had|did|is|are|would|could|didn't|wasn't|couldn't|seemed|knew|felt)\b"
    )
    sub_lead = re.compile(
        r"^(?:If|When|While|As|After|Before|Because|Though|Although|Since|"
        r"Unless|Even|Without|Once|Now that|Whatever|However|No matter)\b"
    )
    for s in sents:
        for m in splice_re.finditer(s):
            pre = s[:m.start(1)]
            if (m.group(1).lower() not in SUBORDINATORS
                    and not sub_lead.match(s)
                    and not re.search(r"\b(?:if|though|when|while|because|after|before|unless|until|matter)\b", pre, re.IGNORECASE)):
                c.review(f"splice: {s[:150]}")
                break
    for s in sents:
        if re.search(r"^[A-Z][\w'’ -]{0,40}, (?:who|which)\b[^.?!]{0,90},", s):
            c.hit(f"S-V backstory cut-in: {s[:150]}")
    opener = r"(?:(?:At|In|On|After|Before|When|As|With|By|From|Inside|Beyond|Beneath|Under|Amid)\b[^,.!?]{2,40}|[A-Z][a-z]+ing\b[^,.!?]{2,40})"
    for s in sents:
        if re.match(opener + r", (?:(?:at|in|on|after|before|when|as|with|by|from|inside|beyond|beneath|under|amid)\b[^,.!?]{2,40}|[a-z]+ing\b[^,.!?]{2,40}), ", s):
            c.hit(f"stacked openers: {s[:150]}")
    for s in sents:
        if len(s.split()) > 35 and s.count(",") >= 4:
            c.review(f"trailing pileup: {s[:150]}")
    out.append(c)

    # 7. punctuation carry
    c = CategoryScore("punctuation_carry")
    semis = text.count(";")
    zh_excl = source.count("！") + source.count("!")
    en_excl = text.count("!")
    zh_ell = len(re.findall(r"…|\.{3}", source))
    en_ell = len(re.findall(r"…|\.{3}", text))
    c.opportunities = zh_excl + zh_ell + 3
    if semis > 3:
        c.hit(f"semicolons={semis} (prompt: a few per chapter at most)", n=semis - 3)
    if en_excl > zh_excl:
        c.hit(f"exclamations EN={en_excl} > ZH={zh_excl} (mirroring/invention)", n=en_excl - zh_excl)
    zh_ell_norm = len(re.findall(r"……|…|\.{3}", source))
    if en_ell < zh_ell_norm:
        c.hit(f"ellipses dropped: ZH~{zh_ell_norm} EN={en_ell}", n=zh_ell_norm - en_ell)
    elif en_ell > zh_ell_norm + 2:
        c.review(f"ellipses invented: EN={en_ell} vs ZH~{zh_ell_norm}", n=en_ell - zh_ell_norm)
    c.detail = {"semicolons": semis, "excl_en": en_excl, "excl_zh": zh_excl,
                "ellipsis_en": en_ell, "ellipsis_zh": zh_ell_norm}
    out.append(c)

    # 8. banned words
    c = CategoryScore("banned_words")
    gloss_en_lower = {en.lower() for _, en, _ in live} | {
        en.lower() for g in glossary for _, en in _aliases(g)
    }
    words_total = len(text.split())
    c.opportunities = max(1, words_total // 100)

    def in_glossary_span(word_pos: int, word: str) -> bool:
        for gen in gloss_en_lower:
            if word in gen:
                i = low.find(gen)
                while i != -1:
                    if i <= word_pos < i + len(gen):
                        return True
                    i = low.find(gen, i + 1)
        return False

    for ban in PERIOD_BANS + AI_TELL_BANS:
        for m in re.finditer(r"\b" + ban + r"\b", low):
            if not in_glossary_span(m.start(), ban):
                c.hit(f"banned {ban!r}: ...{_context(text, m.start())}...")
    out.append(c)

    # 9. costume constructions
    c = CategoryScore("costume_constructions")
    c.opportunities = len(sents)
    for s in sents:
        if re.match(r"^What\b[^?]{8,80}\b(?:was|were)\b", s) and not s.endswith("?"):
            c.hit(f"pseudo/what-cleft: {s[:150]}")
        if re.search(r"\bIn (?:them|it|his eyes|her eyes|that gaze) (?:was|were|lay)\b", s):
            c.hit(f"inverted presentation: {s[:150]}")
        if re.search(r", [a-z]+ in (?:his|her|their) (?:eyes|voice|face)[.,]", s):
            c.hit(f"abstract-noun absolute: {s[:150]}")
        if re.search(r"\b(?:said|asked|replied|answered),\s+(?:mild|low|calm|quiet|flat|soft|gentle|cool)\b", s):
            c.hit(f"bare-adjective tag: {s[:150]}")
    out.append(c)

    # 10. stock-phrase single rendering + invented anchors
    c = CategoryScore("stock_phrases")
    zh_anchor_total = sum(source.count(z) for z in (
        "下一秒", "此刻", "此时", "闻言", "下一刻",
    ))
    for zh, variants in STOCK_PHRASES.items():
        zh_n = source.count(zh)
        if zh_n < 2:
            continue
        c.opportunities += 1
        found = {v: low.count(v) for v in variants if low.count(v)}
        if len(found) > 1:
            c.hit(f"{zh} x{zh_n} rendered {len(found)} ways: {found}")
    for tell in CALQUE_TELLS:
        n = low.count(tell)
        if n:
            c.opportunities += n
            c.hit(f"calque {tell!r} x{n}")
    en_anchor_total = sum(low.count(a) for a in ANCHORS) + low.count("at this moment")
    if en_anchor_total > zh_anchor_total:
        c.opportunities += 1
        c.hit(f"time-anchors invented: EN~{en_anchor_total} vs ZH~{zh_anchor_total}")
    out.append(c)

    # 11. envelope / formatting
    c = CategoryScore("envelope_format")
    zh_status = source.count("【")
    en_bold_status = len(re.findall(r"\*\*【", text))
    en_stray = text.count("【") - en_bold_status
    c.opportunities = zh_status + 2
    if en_stray > 0:
        c.hit(f"unbolded brackets x{en_stray}", n=en_stray)
    if text.count("*") % 2 == 1:
        c.hit("odd asterisk count (unbalanced italic/bold)")
    if re.search(r"^#{1,6} ", text, re.MULTILINE):
        c.hit("markdown header leaked")
    body_after_line1 = text.split("\n", 1)[1] if "\n" in text else ""
    if re.search(r"^\s*Chapter \d+", body_after_line1, re.MULTILINE):
        c.hit("title line leaked into body (beyond line 1)")
    c.detail = {"status_lines_zh": zh_status, "status_lines_en_bold": en_bold_status}
    out.append(c)

    # 12. unit conversion
    c = CategoryScore("unit_conversion")
    unit_re = re.compile(r"[一二两三四五六七八九十百千万亿0-9]+(?<!公)(里|丈|尺|寸)")
    zh_units = unit_re.findall(source)
    c.opportunities = len(zh_units)
    for unit, _en_unit in (("li", "miles"), ("zhang", "meters"), ("chi", "feet"), ("cun", "inches")):
        for m in re.finditer(r"\b\d*\s?" + unit + r"\b", low):
            c.hit(f"untranslated unit {unit!r}: ...{_context(text, m.start())}...")
    if zh_units:
        en_units = {u: low.count(u) for u in ("miles", "mile", "meters", "feet", "inches") if low.count(u)}
        c.detail = {"zh_units": dict(Counter(zh_units)), "en_units": en_units}
    out.append(c)

    return out


def score_text(text: str, source: str | None, glossary: list[GlossaryEntry]) -> dict:
    """Full scorecard for one chapter body. ``source``/``glossary`` may be
    empty for a surface-only score (no rule categories)."""
    result = {
        "surface": surface_metrics(text, source),
        "flow": flow_metrics(text),
    }
    if source:
        result["categories"] = [c.to_dict() for c in rule_category_scores(text, source, glossary or [])]
    else:
        result["categories"] = []
    return result
