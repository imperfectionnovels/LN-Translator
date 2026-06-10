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

1. **Opus arm**: dev DB provider id=5 "Claude Agent opus-4-5 (scratch AB)" — update model to `claude-opus-4-8` (or add a row), point novel 3 `translator_provider_id` at it (see `data/setup_sonnet_ab.py` pattern; cleanup pattern in `data/cleanup_sonnet_ab.py`).
2. **Rule-category compliance scorer**: extend `data/ww_metrics.py`. Mechanical categories: glossary exactness/casing/predicates, epithet frequency vs source, thought formatting (brief: protagonist roman, others italic) + thought subjects (no pro-drop chains), splices/stranded stubs/semicolons/exclamations/ellipsis carry, period-word + AI-tell bans, costume constructions (pseudo-cleft/what-cleft/absolute/inversion), S-V backstory cut-ins + stacked openers + trailing pileups, stock-phrase single rendering (下一秒/此刻/不仅如此), formatting/envelope, unit conversion (里→miles). Judgment categories (side-by-side read): fidelity boundary, recomposition quality, intensity/register tracking, genre conventions.
3. **Phase A — Opus baseline, current stack** (`phase11-novel-voice-precedence-3`): retranslate dev novel 3 ch427/437/414/401 (`python -m backend.scripts.retranslate_chapter 3 <ch> --yes`); fable backups already at `data/pre_sonnet_fable_ch{427,437,414,401}.txt`. Save outputs as `data/opus_baseline_ch*.txt`. Produce the category x chapter compliance matrix memo.
4. **Phase B — compiled v2 stack** (user chose ground-up rewrite over incremental diet): base.md <=1,400 words, xianxia overlay <=650, examples <=500 (~8 pairs that each isolate one feature). Every logged policy survives: full epithets, casing + address-form exception, idiom-sense policy, WW register bar, pro-drop thought subjects, technique-name coinage convention, unconditional precedence ladder + correctness-beats-style. One rule, one layer. Emphasis guided by which categories Opus actually violates in Phase A. Bump PROMPT_TEMPLATE_VERSION to a phase12 token (must contain "novel-voice", pinned by test_pemt_prompt.py).
5. **Phase C — A/B on Opus**: same 4 chapters on v2, identical rubric matrix, plus ONE fable ch427 sanity run. THE USER'S READ IS THE ACCEPTANCE GATE.
6. **Phase D — ship**: commit/push, full pytest, EXE rebuild + smoke (close the running EXE first, it locks dist/), `gh release upload v0.1.0-beta.1 ... --clobber` both assets, relaunch EXE, memory updates, restore dev provider wiring. If v2 loses a category, iterate once; if Opus and fable demand conflicting text, surface the fork decision, don't choose silently.

### Loose ends

- Dev DB scratch provider id=6 (sonnet) is unused: delete or ignore.
- `data/` scratch scripts are local-only by design (gitignored): probe_sonnet_sdk.py, probe_sonnet_bisect.py, setup_sonnet_ab.py, cleanup_sonnet_ab.py, save_battery_texts.py, plus the fix_* repair scripts.
- Deferred watch items: TERMS-extraction split-out as a future workflow lever; colon density; divergent 正法 renders (《三玄钦天正法》 "Art", 天宪正法 "Law of Heavenly Mandate") left alone deliberately.

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
