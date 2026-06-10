# TODO

Roadmap for upcoming work on LN-Translator.

## IN PROGRESS: translation workflow review (prompt load + model sustainability)

Checkpoint 2026-06-10, saved before a conversation clear. Continue from here.

### Verdicts and directives (binding)

- **Sonnet is a FAILURE** (user ruling 2026-06-10): disqualified on translation latency and failure rate. Full-size calls through the claude_agent SDK timed out at 600s repeatedly (4+ attempts across 3 battery runs) while tiny probes and direct CLI calls passed; best theory is the saturated five-hour subscription window queuing large requests, but even passing runs were too slow. Do NOT re-attempt Sonnet benchmarking.
- **Target model is now Opus** (`claude-opus-4-8`). NEVER benchmark prompt changes on fable (user directive: "not sustainable"); fable is allowed only as a one-run live-lane regression sanity.
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
6. **Phase D — ship (BLOCKED ON USER READ of the A/B memo)**: full pytest, EXE rebuild + smoke (close the running EXE first, it locks dist/), `gh release upload v0.1.0-beta.1 ... --clobber` both assets, relaunch EXE, memory updates, restore dev provider wiring (novel 3 translator_provider_id -> NULL). If v2 loses a category, iterate once; if Opus and fable demand conflicting text, surface the fork decision, don't choose silently.

### Rescrape (user finding 2026-06-10, in flight)

69shuba scrape left the LIVE DB novel 2 with 23 missing chapters (53,96,187,233,237,249,392,396,405,413,422,426,428,429,492,522,542,546,550,586,614,619,1322) + 5 short ones. Authoritative source now twkan.com/book/78813.html (1,454 numbered chapters + 5 extras beyond DB's 1,449; traditional script converted locally to simplified + 「」->“” to match existing data). Tool: data/rescrape_twkan.py (scrape = resumable cache in data/twkan_cache/; apply = gzip backup then replace original_text/title_zh + insert missing as pending; translations untouched). Scrape running; then apply --db live and apply --db dev (dev was held until the fable sanity finished — done). After dev apply, battery sources change: future scorer runs reflect the twkan edition (it HAS trail-off dots the old edition stripped, so re-measure the invented-ellipsis category).

### Loose ends

- `data/` scratch scripts are local-only by design (gitignored): setup_opus_ab.py, rescrape_twkan.py, save_battery_texts.py, ww_metrics.py, the memos, plus older sonnet probes.
- Glossary-data cleanup surfaced by the A/B (apply per casing-lock policy, both DBs): 巅峰 "Peak" casing ambiguity, dead traditional-script row 萬眾一心 "One Mind", 香火 "incense" image-pull vs idiom-sense rule.
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
