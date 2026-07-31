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
- **The missing-term signal is atomic-only (precision over recall).** The
  `missing_glossary_term` / `missing_title_glossary_term` observers and the
  edit-mode consistency rail's glossary tier pass `atomic_only=True` to
  `missing_translator_terms`, so they report misses for hard atomic proper
  terms only (`is_atomic_case_locked_term`). Soft rows (generics, slash,
  idiom, lowercase-note, generic-rank) are vocabulary the translator may vary
  by synonym; flagging their absence was noise, not drift (the body observer
  fired in ~96% of novel-2 chapters, floor of ~41% provably soft). This is a
  visibility change only: observations are log-only (no retry), translation
  output is untouched, and the full-coverage TCR metric
  (`consistency_eval.py`) deliberately keeps `atomic_only` off so its
  per-category picture stays complete. We tightened what counts as a miss; we
  did NOT start enforcing more terms (pinning generic variation is wrong, per
  the 4-axes finding). (2026-06-23)
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

## 2026-06-25: the in-app quality cockpit (leverage moved from the engine to the loop)

- **The engine is mature; the leverage is the loop around it.** Consistency works
  (TCR ~92%) and the prompt arc (phase 6 to 17) hit diminishing returns: a base.md
  diction A/B *failed* and was reverted, single-chapter A/Bs are too noisy to
  graduate flags. So the cockpit invests in the *loop*, not the engine: see quality
  (dashboard + per-chapter badge), fix fast (worklists deep-link into glossary /
  reader edit mode), learn (route the user's edits into glossary + brief, capture
  ground-truth). The prose lever that is NOT exhausted is the user's own edits.
- **reader.js split: contiguous, source-order, concatenation-identical.** The split
  into reader-core/toc/glossary/consistency/chapter/edit/quality is mechanical:
  each module is a contiguous slice, loaded in source order, so concatenating them
  is byte-identical to the old file. That is the only split that is provably
  behavior-preserving. **Gotcha that the "byte-identical" framing hides:** function
  *declarations* hoist within one script but bind only when their own `<script>`
  runs, so a module-top-level call that forward-references a later module throws at
  boot (caught live: `_applyReaderMode` -> `applyTermsRail`). Fix: the two
  forward-referenced rail toggles moved into reader-core (they only touch
  core-owned state). The boot-safety lint now checks the *concatenation in load
  order*, which catches this whole class.
- **Quality service cache: pull-based version token, not invalidation callbacks.**
  A full-novel consistency scan is multi-second; caching it keyed on a hash of
  cheap per-novel aggregates (done count + max translated/refined_at + glossary
  updated_at/count) means any retranslate or glossary edit busts the cache for
  free, with zero hooks wired into the write paths. The heavy build runs in
  `run_in_threadpool` so it never blocks the event loop. Single-process only,
  which `WEB_CONCURRENCY=1` already guarantees.
- **Learn-from-edits sourced from captured style_edits, not a re-diff.** The reader
  already writes a style_edits row per paragraph edit, so the panel derives its
  proposal from those pairs rather than diffing a body the edits already mutated.
  Net-new value over the auto-captured style edits: promoting cross-paragraph voice
  patterns to the brief, and fixing glossary casing so a recased term renders right
  *everywhere*. Glossary casing is detected by matching the full `term_en` in both
  before/after (multi-word safe, maps to one updatable entry), proposed not
  auto-applied (confirm-per-row), and written via `update_entry` (lock-on-edit,
  never clobbers). The MECHANICAL bucket is deliberately **not** applyable: a fixup
  already owns it on retranslate, so re-teaching it as a style edit would be wrong.
- **Ground-truth capture closes the graduation loop.** `ground_truth_edits` stores
  a chapter's user-approved body + its `prompt_config_snapshot`; `diff_against_edit
  --ground-truth` scores prompt arms against it. This is what finally satisfies the
  binding graduation rule's criterion 2 (a recoverable reference to diff against)
  without a stray edited file on disk.
- **Did NOT restructure the architecture.** The owner offered a "large restructure",
  but the data did not support rewriting the engine: routes are thin, the glossary
  split is clean, the serial lock is fundamental, migrations are append-only. The
  reader.js split was justified only as *enabling* the cockpit features, not for its
  own sake (the prior de-proposal stands for cosmetic splits). Translator
  vendor-file collapse and dead `humanizer_*` columns remain low-leverage cleanup,
  not done here. Back-catalog retranslation stays the user's separate, cost-gated
  call; the cockpit only makes the stale chapters *visible* (worst-chapter worklist).

## 2026-06-26: cockpit code-review fixes (score what the reader sees; cache that busts; one scan)

- **The cockpit scores `COALESCE(refined_text, translated_text)` everywhere, not the
  raw draft.** The per-chapter badge already scored the refined body "to match what
  the reader displays", but the novel-level scorecard (`quality_report._load_range`)
  and the consistency scan (`consistency_eval._load`) read `translated_text` only. On
  a refined novel that measured an artifact the reader never sees, disagreed with the
  badge, and diverged from the saved ground-truth (which is also COALESCE). Both
  scorers now COALESCE. The done-gate stays `translated_text IS NOT NULL` (refinement
  always chains off a successful translate, so the draft is the existence signal).
  This also makes the CLI A/B score the *shipped* text; the translator arm is still
  isolable via `prompt_config_snapshot` grouping.
- **The version token must include the inline-edit signal.** `_version_token` hashed
  done-count + max translated/refined_at + glossary aggregates, but `edit_paragraph`
  rewrites the body WITHOUT bumping `translated_at`/`refined_at`, so a cached scorecard
  served pre-edit data right after the user fixed a chapter (the badge dodged this only
  by being uncached). Fix: fold `MAX(style_edits.id) + COUNT` into the token. Every
  inline edit inserts exactly one style_edits row, and the aggregate rides
  `idx_style_edits_novel`, so it stays a sub-millisecond guard. (Not fixed here: a bare
  observation-dismiss still doesn't bust the token; tracked separately.)
- **`scorecard()` reuses the cached `consistency()` result instead of re-running the
  scan.** The quality page loads scorecard + consistency together; scorecard used to
  call `_consistency_load` + `_consistency_report` *privately*, so the multi-second
  full-novel TCR scan (and a second glossary load) ran twice per paint. It now awaits
  `consistency(novel_id)`, which dedupes through the existing `("consistency", id)`
  cache key. The reuse sits *after* the empty-range short-circuit so a narrow empty
  sub-range never triggers a whole-novel scan it would discard. The two per-key locks
  never form a cycle (consistency never calls scorecard), so no deadlock. Regression
  tests pin all three (refined-body TCR, token-busts-on-style-edit, scan-runs-once).

## 2026-07-30: phase-0 hygiene sweep (CAT-pivot prep)

- **Dead HTTP surfaces removed; service cores kept only where actually
  load-bearing.** `GET /api/cache/stats` (whole router), `GET
  /api/novels/{id}/tm/inconsistencies`, the novel-rollup
  `GET /api/novels/{id}/observations`, and both observation bulk-dismiss POSTs had
  zero UI callers (verified by grep over frontend/js). `llm_cache.get_stats` stays:
  it still feeds `/api/diagnostics`.
- **Correction (same day): `services/tm.py::find_inconsistencies` was deleted
  too.** The sweep's KEEP instruction claimed
  queue.py::_emit_tm_inconsistency_observations calls it; that was a wrong
  premise. The queue writes tm_inconsistency observations via its own inline SQL
  and never imported the function, so after the route deletion it had zero
  production callers (test-only coverage). Lesson: verify a claimed caller by
  grep before treating "still feeds X" as a keep reason; the first sweep repeated
  the plan's claim without checking.
- **test_cache_stats.py was NOT deleted wholesale** even though the plan listed it:
  5 of its 6 tests pin the still-live llm_cache counter behavior (the only coverage
  of `get_stats`/`reset_stats`). Only the endpoint test died with the route. Same
  judgment in test_tm_routes.py: the file had no concordance tests to "keep", so it
  was repurposed to pin the kept concordance route instead of being deleted.
- **`GET /api/imports/{id}/status` kept with an honest docstring.** Its claim of
  "used by library card badges" was false (badges render from the /api/novels
  payload); it stays as a scripting/polling surface, test-covered.
- **Nav chords:** Quality cockpit is `g y` (the `g q` the plan suggested collides
  with Queue); Global glossary is `g b`. Spine placement: Quality joins the
  novel-scoped trio (reader/glossary/quality, /library fallback when no lastNovel);
  Global glossary sits in the Studio foot (cross-novel = app-level).

## 2026-07-30: CAT-pivot Phase 1, the 1:1 paragraph contract

- **Count validation, not paragraph numbering.** The alternative (numbering each
  source paragraph and asking the model to echo `[[P7]]` markers) was rejected up
  front: markers leak into prose (the same failure class as the old 【】 bracket
  echoes), every text_fixup and observer would need marker-awareness to avoid
  counting or mangling them, and stripping them adds one more deterministic
  rewrite layer on top of model output. A bare count check is invisible to the
  model on the happy path, needs zero output post-processing, and still gives the
  Phase 2 segment store the invariant it needs (position IS the key when counts
  match and order is preserved).
- **Pre-join is deterministic code, not prompt guidance.** The old base.md rule
  ("join mid-sentence breaks") made paragraph structure a model judgment call,
  which is exactly what a positional segment key cannot tolerate: two runs could
  legitimately join differently and re-key every downstream row.
  `services/segmentation.py::effective_source_paragraphs` now owns the join
  (heading dropped via tm.py's detector, blank-line split via tm.py's splitter,
  terminal-punctuation heuristic frozen under SEGMENTATION_VERSION), so the model
  only ever sees text whose paragraph boundaries are already final. Prompt-time
  only; stored original_text stays verbatim.
- **Ladders differ by cost of discard.** Translator: one corrective retry, then
  ACCEPT the violating body flagged (`count_mismatch_accepted` on the attempt
  row); throwing away a whole translation over a paragraph miscount punishes the
  user, and the flag lets Phase 2 skip unmapped chapters. Refiner: one corrective
  retry, then DISCARD (`refinement_status='error'`, reader falls back to the
  draft); the draft is already good text, so a discarded polish costs nothing.
  Neither path ever caches a violating body (validation sits before every
  llm_cache store, including a violating pre-guard cache entry on the refiner
  read path). Residual seam (spec review): validation runs on the raw model
  body, and post-validation fixups (the enforce_mid_sentence_comma_break weld,
  strip_leading_title_line) can still change the COMMITTED body's paragraph
  count relative to the validated count. Phase 2 segment building must key off
  the committed text, never the validated count.
- **base.md scope.** Only the paragraph-boundary contract changed: the two
  boundary rules were replaced by "one paragraph in, one paragraph out", and the
  adjacent "New paragraph when the speaker changes" clause was deleted with them
  because it mandates splitting, which the 1:1 contract forbids (CJK webnovel
  sources already give each speaker their own paragraph). The in-paragraph
  recomposition license and every WW-register rule are untouched.
  PROMPT_TEMPLATE_VERSION -> phase18-cat-1to1-1.
