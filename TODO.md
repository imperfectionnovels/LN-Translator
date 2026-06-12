# TODO

Roadmap for upcoming work on LN-Translator.

History note (2026-06-11): completed phase logs (workflow review + phase12-14, ch392/phase14 residue sessions, twkan rescrape, phase15 adversarial WW audit, phase16 thought-italics, phase17 flow-seams) were pruned from this file; the full records live in this file's git history, the data/ memos (opus_ab_phase15_memo.md, opus_ab_phase17_memo.md, battery_phase17_results.md, rules_ledger.md, flow_awkward_inventory.md), and the project memory files.

## Follow-up the user may pick up: stale back-catalog wrong words

User report 2026-06-11: the translator "doesn't use the most appropriate word" (off-register everyday diction + wrong-sense words), seen in chapters up to ~423. Diagnosis: those chapters are stale phase3-phase6 era translations predating the phase7/8/14/15/17 diction and register fixes; no prompt lever (style anchor included) changes already-committed text, only retranslation applies the current stack.

- Saved plan: `C:\Users\Roych\.claude\plans\stale-catalog-wrong-words-verification.md`. One-chapter verification: retranslate a stale chapter (default ch400, or one the user names) on the DEV DB under phase17, word-diff vs the stale live text, report quoted before/after pairs.
- If verified fixed: decide on selective back-catalog retranslation (user's call, subscription cost; forward-only stance otherwise stands).
- If wrong words persist under phase17, lever ladder in cost order: populate the EMPTY novel-2 `style_note` (zero code), add sense-trap brief lines from concrete examples, glossary `usage_note`s; the exemplar-prose style-anchor feature (paste admired published prose into every prompt as a register sample) is the LAST resort, single-variable A/B-gated with an exemplar-leakage check.

## Open user decisions / watch items (do not act without the user)

- RESTART the running app on a current build: live ch428-435 still translated under phase14 (the running EXE bundles the old prompt stack; phase15+17 fixes only reach new translations after restart). Fresh builds were uploaded to v0.1.0-beta.1 and parked at `%LOCALAPPDATA%\Temp\ln-dist-phase17\` (dist\ was locked by the running app).
- GLOS-TITLELEN further lever: a glossary short-handle field (feature work, user's call). Phase15 already moved pronoun:title from 11 to 4 on ch437; pros run 3.6-6.1:1.
- Dev battery watch: ch414 coined name 天语馄烨龙章 romanized inconsistently; pin with a glossary entry if it recurs.
- Phase17 watch items (data/opus_ab_phase17_memo.md): "as the words fell" calque (single instance); ch414 unmarked interior panic rendered as spoken quotes once; COLD-ABUT moved 9/18, room remains if the user still hears flat seams.
- Scorer note for future batteries: sources are now the twkan edition (it HAS trail-off dots the old edition stripped), so re-measure the invented-ellipsis category before trusting it.
- Deferred workflow lever: split TERMS extraction out of the translate call; colon density; fable invented-ellipsis trait (model-level, not stack).

## Translation quality

- [ ] Optimize translator and refinement prompts
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
