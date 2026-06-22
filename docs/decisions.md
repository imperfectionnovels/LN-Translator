# Decisions & lessons log

The **why** behind non-obvious choices, what is settled (so it is not
re-litigated), what was ruled out and why, and mistakes-and-corrections so they
are not repeated. Read this before proposing a structural change or re-opening a
"shouldn't we just..." question.

How this differs from its neighbors:
- `docs/gotchas.md` = mechanical CODE traps + their fix (newline translation,
  FTS5 corruption, subprocess kill tree, element-ID drift). Reproducible
  pitfalls.
- This file = judgment-level DECISIONS and LESSONS (the rationale, the
  ruled-out alternatives, the mistakes). Prevents re-deriving settled calls.
- `CLAUDE.md` = how the system works now (current-state reference).

**Maintenance:** when a non-obvious decision is made, an alternative is measured
out, or a mistake is caught and corrected, add a dated bullet here as part of
"done". Keep entries short; link commits. Newest first within each section.

---

## Standing decisions (settled; do not re-litigate without new evidence)

- **A/B is measured, never eyeballed on one chapter.** Single-chapter A/Bs are
  noisy and have caused bad default flips. Run `python -m
  backend.scripts.quality_report --novel N [--chapters LO-HI]` (per-category
  matrix + observation harvest + grouping by `prompt_config_snapshot`; `--diff`
  for two arms with bootstrap CIs). Ship new flags at parity; flip a default
  only after a single-variable A/B vs a ground-truth fixture, cited in the
  commit. (2026-06-21)
- **Don't fortify what works.** If a code-read / data-check can't reproduce a
  reported defect, say so. Verify a damage vector is *live* before changing
  behavior that is currently correct. Measurement/visibility is the safe move;
  silent suppression is not. (standing)
- **The consistency mechanism works (TCR ~92%).** A full CAT rebuild,
  segment-reuse translation, a new-term enforcer, and soft-anchors were all
  measured out for this corpus. Do not re-propose them. The wins are the
  termbase (glossary) + deterministic casing fixups. (2026-06)
- **Translator is strictly serial** (one process-global `asyncio.Lock`). Never
  replace with `Semaphore(N)` and never `--workers > 1`: parallel calls burn the
  subscription window / token budget.
- **Single LLM call per chapter.** The refiner is an opt-in second provider
  (`novels.refinement_provider_id`), not a default stage.
- **Prompt fixes apply forward-only.** The back catalog is deliberately left on
  its original prompt version (user's choice). On any "fixed defect reappeared"
  report, check `chapters.prompt_config_snapshot` template version first.
- **No JS framework, no build step, no drop caps, no em-dashes** (the last is
  lint-gated for `backend/prompts`).
- **Glossary fixes are non-destructive.** Fix in place, never delete a row,
  never clobber a locked entry. Casing escape hatch for a generic forced-cased
  term: lock the row, lowercase `term_en`, add `lowercase` to notes.
- **Frontend element-ID drift is gated** (`scripts/check_element_ids.py` in CI +
  pre-commit). A big `reader.js` decomposition was deliberately descoped (high
  churn, no behavioral payoff, drift risk); the gate protects it far cheaper.

## EXE / release workflow

- Build with `python -m PyInstaller LN-Translator.spec` (capital P; the
  lowercase `pyinstaller` shim fails on this machine).
- When the app is running it holds `dist/`; rebuild to an alternate
  `--distpath build_new` so the running app is not disturbed.
- The live DB (`%APPDATA%\LN-Translator`) picks up additive schema migrations on
  the next EXE launch via `init_db` (no manual live-DB migration needed).
- After a code-touching change, rebuild the EXE and refresh the GitHub release
  assets (`gh release upload ... --clobber`). Skip for docs/tests/CI-only.

## Lessons from mistakes

- **2026-06-21: Don't trust a noisy detector's raw count as a bug count.**
  Claimed "13 live element-ID drifts"; on inspection 11 were dynamically-created
  IDs (false positives) and 2 were intentional legacy-cache cleanup. There were
  zero real bugs. Rule: ground a reported count in the actual code before
  asserting it; an inaccurate detector that needs manual filtering is itself the
  thing to fix (that is why it had never been automated).
- **2026-06-21: Check the data before "hardening" a documented damage vector.**
  Before adding a behavioral gate to the casing fixup, queried the glossary: the
  force-case collision vector was already clean (live collisions escape-hatched).
  Shipped visibility (`chapters.fixup_audit` + a detector), not a behavior
  change. "Gate" meant *never silently*, not *never rewrite*.
- **2026-06-15/16: A re-attached `secret_ref` can 401-break all translation.**
  An Agent-SDK credit-pool OAuth token, re-attached by a Settings save, expired
  and hard-failed every `claude_agent` chapter mid-run (output_tokens NULL at
  handshake). Fix was config-only on the live server (clear keyring token +
  secret_ref). It was not harmless. (See agent memory for the full unwind.)
- **Fixup-damage triage: replay first.** On any "translation reads broken"
  report, replay `_apply_text_fixups` on the raw `llm_cache` body vs the
  committed body before suspecting the model. A replay mismatch points to an
  out-of-band writer. `data/fixup_replay_audit.py` is that tool; per-chapter
  `chapters.fixup_audit` now records it forward.
