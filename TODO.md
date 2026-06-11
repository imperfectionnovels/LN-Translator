# TODO

Roadmap for upcoming work on LN-Translator.

## IN PROGRESS 2026-06-11: adversarial full-stack rule audit vs Wuxiaworld corpus

Trigger: user read live ch424 (a stale phase5 translation) showing the comma-to-period
stamp defect plus the standing "lacking vs a WW pro" gap. Binding directive: every
translation-governing rule is presumed faulty and must justify being kept with evidence
from professional wuxiaworld.com translations; scope covers base.md, the xianxia
overlay + examples, the runtime template (translators/base.py), refiner.py, the novel-2
custom_style_brief, glossary policy/data, and the structural tests that pin them.
Plan file: C:\Users\Roych\.claude\plans\certain-issues-like-period-purring-volcano.md

- WS1 DONE: live ch424/425/427 retranslated onto phase14/claude-opus-4-8 through the
  production queue (backup novels.pre-forward-retranslate.20260611-013543.db.gz; no hand
  edits lost). Stale-inventory root cause confirmed: of 137 done chapters only 15 were
  current-stack; the user chose forward-only scope, back catalog (ch1-423) left alone.
- RESIDUAL LIVE MISS: ch424 opening 剑阁，极天崖。STILL period-splits under phase14
  ("Sword Pavilion. Extreme Heaven Cliff."), while ch429 renders the same shape with a
  comma. The base.md comma-hinge clause ("or its own sentence when the clause stands
  alone") is the suspected hatch; on trial in Phase C with this chapter pair as evidence.
- Artifacts so far: data/ww_corpus/ (10 chapters, 6 novels, 7 translators, ~27k words,
  INDEX.md), data/rules_ledger.md (143 rules, 8 sources, 5 pre-flagged conflicts).
- Next: Phase C adversarial per-rule trials (data/rule_trials/) + Phase D positive-gap
  comparative read (data/positive_gaps.md), then phase15 synthesis and the battery A/B
  ship gate on claude-opus-4-8 (never fable), chapters 401/414/427/437.
- WS1 metrics side-findings to fold into the glossary trial bundle: missing glossary
  renders (mid stage 中期, Moon Star 月星, Dao Body 道身, cultivation technique 功法,
  Faction 势力), predicate losses near Demon-Purging True Person, one banned word
  ("slew"), "Yun Family" lowercased once.

## DONE 2026-06-10: translation workflow review (prompt load + model sustainability)

Shipped 2026-06-10 (phase12+13 compiled v2 stack + phase14 english-cadence pass,
EXE rebuilt and release re-uploaded). Phase D's blocked-on-memo-read gate was
resolved by the user's explicit ship-now decision during the ch392 defect
session; the A/B memo (data/opus_ab_phase_c_memo.md) remains available to read.

### Verdicts and directives (binding)

- **Sonnet is a FAILURE** (user ruling 2026-06-10): disqualified on translation latency and failure rate. Full-size calls through the claude_agent SDK timed out at 600s repeatedly (4+ attempts across 3 battery runs) while tiny probes and direct CLI calls passed; best theory is the saturated five-hour subscription window queuing large requests, but even passing runs were too slow. Do NOT re-attempt Sonnet benchmarking.
- **Target model for prompt benchmarking is Opus** (`claude-opus-4-8`). Don't benchmark prompt changes on fable (user directive: "not sustainable" for benchmark batteries). Clarified 2026-06-10: fable is NOT forbidden as a provider; the user may select it per-novel like any other model.
- **Benchmark method (user-specified)**: score outputs against the PROMPT'S OWN RULE CATEGORIES — violations / checkable opportunities per category, with quoted examples — not just surface register metrics.

### Findings already established (do not re-derive)

- Composed system instruction: ~5,000 words / ~7k tokens (base.md 2,714 + xianxia overlay 1,048 + examples 815 + precedence ladder + ~330-word brief), ~150 constraints, ~3:1 instruction-to-content ratio per call. Dynamic blocks are lean (chapter-filtered glossary was 56 rows / ~1.4KB for ch427).
- Three structural problems: (1) phases 7-11 tuned on fable only; (2) duplication/scar tissue across layers; (3) one call does four jobs (title + prose + glossary compliance + TERMS extraction) — future lever, kept for now.
- `prompt_snapshot` instrumentation fixed (base.py stamps the exact prompt after caching).

### Remaining tasks (in order)

1. ~~Opus arm~~ DONE 2026-06-10: provider id=5 -> claude-opus-4-8, novel 3 pointed at it (data/setup_opus_ab.py); scratch sonnet id=6 deleted.
2. ~~Rule-category compliance scorer~~ DONE: data/ww_metrics.py rules_report (violations/reviews/opportunities + quotes per category; reuses backend body_correctness_observations; respects "lowercase" usage notes).
3. ~~Phase A~~ DONE: outputs data/opus_baseline_ch*.txt, memo data/opus_baseline_matrix.md. Headlines: thought-format conflict (overlay italic beat brief roman via ladder), negative-example contamination (4 banned constructions reproduced verbatim from ch427-mined examples), 35 invented ellipses, locked-term misses.
4. ~~Phase B~~ DONE: phase12-novel-voice-compiled-1 (commit 94f8908). base 1,409 w / overlay 654 / examples 430 (was 4,569 total). Examples genericized; brief owns thought formatting; no-added-trail-off rule.
5. ~~Phase C~~ DONE: v2 outputs data/opus_v2_ch*.txt + fable sanity data/fable_v2_ch427.txt. A/B memo: data/opus_ab_phase_c_memo.md. v2 equal-or-better on 10/11 mechanical categories (protagonist italics 15->3, archaic/cleft/calque fixed); regressions: "slew" x2 near All-Slaying + 3 judgment-grade idiom/phrase instances on ch427 traced to cut novel-mined examples. Fable live-lane sanity PASS.
6. ~~Phase D — ship~~ DONE 2026-06-10: shipped together with the phase14 english-cadence pass after the user's ship-now decision (ch392 defect session). Full pytest (1450+), dev Opus validation retranslate of ch392 (all six checks passed: clean title, dashes preserved, 神道 as "the gods", profanity at force, recomposed cadence, rules_report no-regression), EXE rebuilt + smoked, release assets re-uploaded, dev provider wiring restored.

### Ch392 defect session (2026-06-10, same-day follow-up)

User read of live ch392 surfaced five defects; all fixed and shipped:
- Dash mangling (fixup damage, the worst): enforce_em_dash deleted source —— interruption/suspension dashes ("you, !!!", orphaned "The next instant," paragraphs, comma-welded BOOM paragraphs). Fixed via _dash_protected (keep before punctuation / closing quote / newline / end-of-text; kept runs collapse to one em-dash). Committed chapters repaired via data/repair_dash_damage.py (segment-anchored splice from raw cache replay): live 38/38 applied + ch392 title SQL fix, 2 live skips were already hand-fixed; dev 1 applied + ch392 spot fix, 10 old-epoch skips left alone. Backups: novels.pre-dash-repair.*.db.gz in each data root.
- Author update markers in titles (（第四更！）): stripped at prompt time + zh-gated normalize backstop (parser.strip_title_update_marker).
- 神道 -> "no Dao": missing glossary entry added both DBs with metonymy usage_note + base.md no-sub-entry-capture clause.
- Profanity softening (狗日的 -> "damned"): base.md calibration bullet.
- Chinese comma-chain cadence: phase14 (see above).

### Phase14 residue session (2026-06-10 late evening, same-day follow-up) — DONE

User read tonight's 15 fresh chapters (phase14-english-cadence-1, claude_agent /
claude-opus-4-8) and reported both the cadence pass and the title-marker strip
"didn't totally pass". Full-replay attribution (_apply_text_fixups re-run on raw
llm_cache bodies) reproduced committed text byte-for-byte for 14/15 chapters:
the PROMPT WORKED (raw prose recomposed, fluent); every visible defect was the
deterministic layer or glossary data. Findings and fixes, all shipped:

- Title residue = ONE chapter (ch426): twkan rescrape truncated the source title
  mid-marker (（晚上还有三更, no closing paren), defeating both the closed-paren
  strip and its zh-gate. Fixed: end-anchored _TITLE_NOISE_OPEN_RE (ad982d3);
  title repaired in DB. The 21 pending marker titles are closed-paren shapes the
  existing regex already handles.
- Fixup defects (ad982d3 + 063eb86, all test-first): dash before closing `*`
  now protected ("*This man is, *"); dash after copula joins without dash
  ("Its name was, the X"); one-word beat paragraphs no longer comma-weld
  ("fantasy, BOOM." now period + standalone BOOM); up-caser skips rewrites that
  would down-case inside Title-Case neighborhoods ("...Peak-Moving righteous
  Dharma"); sentence-head capital wins over lowercase-lead canonicals.
- Glossary data class (the volume leader): generic nouns locked Title-Case in
  trusted categories stamped caps mid-sentence ("the Devil is vicious", "every
  Sect", "icy Abyss", "all Creation dimmed", "spiritual Qi of Heaven and
  Earth"). Hatched (lowercase term_en + lowercase note) in BOTH DBs:
  魔/宗门/门派/深渊/造化/昭顯/邪祀/天地之气 + 天人 (term_en only, context-cased), then
  audit-approved 神光(太乙)/地狱/紅塵/天意. Audit method: fire-scan all 1,387
  atomic targets over 40 recent texts; only ~10 real offenders, the single-word
  suspects (Academy/Elder/Faction/...) never fire. data/fix_glossary_generic_casing.py.
- Out-of-band edit found: ch396 carried 3 phrase rewrites no fixup can produce
  ("fruition attainment embryo" -> canonical, leaving "a Embryonic"); a parallel
  glossary-session propagation, not the pipeline. Article fixed.
- Repairs: data/repair_phase14_fixup_damage.py full-replay splice (valid because
  replay==committed held) rewrote 13 live chapters + ch426 title; dev ch392
  repaired the same way; gzip backup novels.pre-phase14-repair.db.gz; idempotent.

### Rescrape (user finding 2026-06-10) — DONE, both DBs applied

69shuba scrape left the LIVE DB novel 2 with 23 missing chapters (53,96,187,233,237,249,392,396,405,413,422,426,428,429,492,522,542,546,550,586,614,619,1322) + 5 short ones. Authoritative source now twkan.com/book/78813.html (1,454 numbered chapters; traditional script converted locally to simplified + 「」->“” to match existing data). Tool: data/rescrape_twkan.py (scrape = resumable cache in data/twkan_cache/; apply = gzip backup then replace original_text/title_zh + insert missing as pending; translations untouched).

Status 2026-06-10: applied wholesale to BOTH DBs (live novel 2: 1451 rows; dev novel 3: 1446 + 5 inserts; ch 101/108/118 kept old text, twkan pages broken-short). The first live apply corrupted structure (whole chapter in title_zh, quad-newline paragraph gaps): write_text/read_text newline translation double-fault, see docs/gotchas.md. Script fixed (LF cache + CR-strip reader + exact keep-same so reruns repair/no-op) and re-applied; verified clean on both DBs. Apply now also strips obfuscated twkan ad paragraphs (math-alphanumeric / circled / squared / small-caps domains, ⊥-interleaved promo, GOOGLE搜索TWKAN) that the scrape-time noise filter missed. Battery sources changed: future scorer runs reflect the twkan edition (it HAS trail-off dots the old edition stripped, so re-measure the invented-ellipsis category).

### Loose ends

- `data/` scratch scripts are local-only by design (gitignored): setup_opus_ab.py, rescrape_twkan.py, save_battery_texts.py, ww_metrics.py, the memos, plus older sonnet probes.
- ~~Glossary-data cleanup surfaced by the A/B~~ DONE 2026-06-10 (data/fix_glossary_shendao.py, both DBs): 巅峰 -> "peak" + lowercase note, 萬眾一心 re-pointed to simplified 万众一心, 香火 idiom-sense usage_note, plus the new 神道 entry.
- Deferred watch items: TERMS-extraction split-out as a future workflow lever; colon density; divergent 正法 renders left alone deliberately; fable invented-ellipsis trait (model-level, not stack).

## Translation quality

- [ ] Optimize translator and refinement prompts
  - [x] Reframe `backend/prompts/base.md` and the per-genre overlays into a concise novelist's brief (2026-05-29); refiner prompt reframed the same way.
  - Expand `backend/prompts/examples/<genre>.md` worked examples where coverage is thin.
  - Re-baseline against a fixture chapter per genre after each prompt revision.

## Cost reduction

- [ ] Keep per-chapter API costs down
  - Trim prompt size: shrink glossary payload (top-N by recency/frequency), shorten previous-chapter tail, drop dead context blocks in `services/translators/base.py::build_prompt`.
  - Cache aggressively: extend `services/llm_cache.py` reuse and verify prompt-prefix stability so provider-side prompt caching hits.
  - Default new novels to cheaper provider tiers where quality is acceptable; surface estimated cost-per-chapter in Settings.
  - Make the refinement pass opt-in per chapter, not just per novel, so users can spot-refine instead of paying for every chapter.

## Platform coverage

- [ ] Ship macOS, Linux, and mobile builds
  - macOS: PyInstaller `--onedir` + `.app` bundle, codesign + notarize, replace WebView2 with `pywebview`'s WKWebView backend.
  - Linux: PyInstaller bundle + AppImage (or Flatpak), GTK WebKit backend for `pywebview`.
  - Mobile: evaluate a thin web client (PWA) pointed at a self-hosted backend vs. a packaged Capacitor/React Native shell; pick one path and prototype.
  - CI: extend the release workflow to build and attach macOS / Linux / mobile artifacts alongside the existing Windows EXE.
