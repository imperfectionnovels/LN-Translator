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

## 2026-07-31: CAT-pivot Phase 2, segment store + read-only editor

- **Lazy backfill on GET.** The segment rows for a chapter are built on the
  first `GET .../segments` (the editor open), not by a boot-time migration or
  a queue hook. Rationale: a local single-user app has no concurrency pressure
  on the read path, the build is a pure function of stored text (re-runnable
  any time), and building only what the user actually opens keeps a
  2,000-chapter back catalog from paying alignment cost it may never need.
  The route commits before returning; `backend/scripts/backfill_segments.py`
  is the optional bulk pre-warmer (same service, no duplicate logic).
- **Wholesale rebuild this phase, preservation seam for Phase 3.** Rebuild
  (version bump, self-heal, first build) is DELETE+INSERT of the whole
  chapter. That is correct now because every row this phase writes is
  status='machine' / origin='aligned_backfill'; nothing user-authored exists
  to lose. Phase 3 adds row preservation at the marked `_preserve_human_rows`
  seam in `segments.build_segments_from_alignment` (snapshot non-machine rows
  before the DELETE, merge back by seg_index/source_hash), rather than
  spreading merge logic across callers.
- **Positional mapping when counts match; full DP path otherwise.** When
  `effective_source_paragraphs` and the target split agree on count, position
  IS the alignment (the Phase 1 contract's whole point) and every row is a
  confident anchor; the length-DP would only add noise. Only mismatched
  counts go through `tm.full_alignment_path`, which extends `_length_align`'s
  DP to a total mapping: merged targets join onto their source with a blank
  line, sources with no plausible target get "" (aligned=0), inserted targets
  attach to the preceding source on the path, and outlier anchors are demoted
  to aligned=0 instead of dropped (the full path must place every target
  somewhere; hiding the text would break join(targets) == body). Below the
  existing <50% anchor gate the chapter is 'unaligned' with zero rows and a
  retranslate CTA.
- **Segments key off the COMMITTED displayed text, never a validated count**
  (the Phase 1 review lesson: post-validation fixups can change the committed
  paragraph count). Consequence: the self-heal check compares the stored
  targets against the NORMALIZED split of the displayed body (empty targets
  skipped) rather than raw string equality with the column, so CRLF bodies
  and 'partial' chapters do not force a rebuild on every read; the check
  still catches every out-of-band content edit.
- **Unaligned verdicts are rev-gated (review follow-up, same day).** A
  zero-row 'unaligned' chapter originally re-ran the aligner on every editor
  open. `chapters.segments_rev` (the displayed body's 16-hex rev, stamped at
  build time) now gates it: while version and rev both match, the verdict
  stands and the read is free; a retranslate changes the rev so the next
  open re-attempts and picks the chapter up. Phase 3 wants body revs for its
  stale-tab guard anyway, so the column pays twice. Also dropped the
  redundant (chapter_id, seg_index) index (the UNIQUE constraint already
  provides it) via an appended DROP INDEX migration.

## 2026-07-31: CAT-pivot Phase 3, editing loop + single write path

- **One write path; edit-paragraph becomes an adapter.** The reader's
  `/edit-paragraph` now routes through `segments.update_segment` whenever the
  chapter has a clean store (`segments_state='ok'` for the SAME variant the
  reader edited). The display-paragraph index maps onto seg_index by walking
  the non-empty targets in order (empty-target rows do not render as reader
  paragraphs, per the I1 join's empty-skip); the mapping is used only when
  the non-empty row count equals the body's raw split count, no stored
  target spans multiple paragraphs, and the stored target at the slot equals
  the client's `before_md` verbatim. Anything else falls back to the legacy
  direct splice, and the store self-heals on the next editor open. Falling
  back (rather than 500ing or force-rebuilding inline) keeps the reader's
  contract byte-identical and makes the reroute purely additive.
- **Rebuilds are text-authoritative; "preserve" means status, not text.**
  When a rebuild or self-heal runs, the body already changed out of band, so
  the body wins on TEXT while human rows keep their status / origin /
  timestamps / machine_text and re-anchor by source_hash (their own
  seg_index is the first candidate, so an unchanged source maps
  positionally). Keeping human target TEXT verbatim instead would break I1
  (join(targets) == body) and put the store in a permanent rebuild loop.
  The opposite direction (segments win, body regenerated) is exactly the
  Phase 4 worker/refiner merge; rebuild deliberately does not do its job.
- **I3 implemented literally: human rows are never DELETEd.** The
  preservation rebuild deletes machine rows only, shifts the surviving human
  rows out of the seg_index space (offset re-key under the UNIQUE
  constraint), then moves each onto its anchored slot; the DB primary keys
  survive, which the tests pin. When alignment fails (below the <50% gate,
  empty splits, or a human row's source paragraph vanished) on a chapter
  that HAS human rows, every row is retained untouched under a rev-gated
  'unaligned' verdict; the editor renders them read-only with a
  preservation banner. Only human-row-free chapters keep the Phase 2
  zero-row unaligned behavior.
- **Writes to an 'unaligned' chapter 409 as `stale_chapter`.** A segment
  write against retained rows would rematerialize a stale body over the new
  one. Rather than mint a fourth error kind, the guard reuses
  `stale_chapter` because its recovery (re-GET) is exactly right: the
  reload renders the read-only unaligned banner.
- **reproject_from_body fast path is positional and conservative.** In-place
  target updates (status preserved, machine_text refreshed only for machine
  rows) require the body's paragraph count to equal the non-empty row count
  AND every stored target to be single-paragraph; any merged row or count
  drift falls back to the preservation-aware rebuild. Wired inside the same
  transaction of `find_replace.commit_preview`,
  `apply_in_place_for_glossary_term`, and `fr_snapshots.restore_snapshot`;
  chapters with zero segment rows are left alone (the lazy read builds them
  later against whatever body then exists).
- **revert_machine stamps origin='aligned_backfill'.** The pre-edit origin
  is not stored, and every machine_text in this phase came from the
  aligned backfill; the Phase 4 worker merge re-stamps real provenance when
  it refreshes machine rows. (Corrected same-day by spec review: reverting
  a hand-filled empty slot is REFUSED, see the addendum below; originally
  it re-opened the slot.)
- **Editor writes are serialized client-side.** Every PATCH rides one
  promise chain and resolves `chapter_rev` / `before_target_hash` at send
  time from the freshest payload, so rapid-fire edits cannot race each
  other into spurious 409s, and a retry after a conflict reload reuses the
  reloaded guards instead of the stale ones captured at focus time.

### Addendum (same day, spec review round)

- **The store is only an authority for the body it was built against
  (freshness predicate).** The edit-paragraph reroute's preconditions
  (state 'ok', variant match, positional count match, stored target ==
  before_md) all pass on a STALE store when the displayed body moved after
  the build but kept the edited paragraph byte-identical: store built on
  the draft, refinement lands (the refiner does not touch segments, so
  segments_state stays 'ok'), user edits an unchanged paragraph, and the
  rematerialization would write refined_text := join(draft targets + edit),
  silently reverting the whole refinement with a self-consistent
  segments_rev that self-heal can never catch. Same shape post-retranslate.
  The reroute now also requires `chapters.segments_rev ==
  chapter_rev(displayed body)`; on mismatch it falls back to the legacy
  splice and the store self-heals on the next editor open. (The editor's
  own PATCH path was already safe: its client rev is checked against the
  CURRENT displayed body, so a stale tab 409s.)
- **revert_machine refuses empty machine_text ("" rejects like NULL).** A
  revert on a hand-filled empty slot would drop the paragraph from the
  body again, and on a refined chapter whose only paragraph it was, empty
  refined_text entirely, flipping displayed_body back to the draft under a
  segments_rev stamped against "". There is nothing to revert TO; the 400
  message points the user at editing instead.
- **Optimistic-confirm rollback is chapter-guarded.** The confirm PATCH's
  response handlers previously read the live currentCh, so a late-draining
  rejection after chapter navigation could paint the old chapter's fields
  onto the new chapter's same-index row. All segment actions now capture
  the chapter at call time and guard rollback, conflict reload, and
  response application on it (matching applyPatchResponse's existing
  guard).

## 2026-07-31: CAT-pivot Phase 4, retranslation durability + pre-fill + assist rail

- **The worker merge is segments-authoritative for human rows (closes the
  Phase 3 interim defect).** `apply_machine_translation` runs INSIDE the
  claim-guarded success transaction of both the translate and the refine
  commit: human rows (edited|confirmed) keep `target_text` verbatim and
  only refresh `machine_text` with the new AI rendering; machine rows
  regenerate with origin `llm` / `llm_refined`; the COMMITTED body is the
  merged join. The claim UPDATE opens the write transaction BEFORE the
  merge reads human rows, so an editor write cannot interleave between the
  read and the commit; a lost claim or a failed translation rolls the
  transaction back, so an errored run never touches the store.
- **Merge fallback keeps everything (I3).** When the machine output cannot
  be aligned to the source list (below the DP confidence gate) or a human
  row's source paragraph vanished (stored rows predating
  SEGMENTATION_VERSION, or a source edit), the merge retains EVERY row,
  demotes them to aligned=0, and stamps `unaligned`; the machine output
  still becomes the displayed body. Deliberate deviation from a per-row
  "retain just the unanchorable rows" reading of the spec: a retained
  extra row with text would break invariant I1 (join == body), so
  retention is all-or-nothing, matching the Phase 3 rebuild seam.
- **Committed bodies are normalized.** The merged body is the
  paragraph-stripped blank-line join, so I1 holds as exact string equality
  (a body with trailing whitespace or triple newlines comes back
  normalized). One test fixture with a trailing space was updated; the
  reader renders identically.
- **Prefill semantics.** `prefill_confirmed_exact` pre-fills machine rows
  from CONFIRMED rows of other chapters sharing the source hash: full
  source-text equality guards the 16-hex prefix against collisions,
  most-recently-confirmed wins, status stays `machine` with
  origin='tm_exact' (a TM chip, not silent authority: the user still
  confirms it in the new chapter), and `machine_text` keeps the fresh AI
  rendering so revert-to-AI shows the model's own take.
- **APPROVED TRANSLATIONS is a coherence aid; the merge is the
  enforcement.** The block (human rows + exact confirmed matches, cap 30
  pairs / 8k chars, `PROMPT_INCLUDE_APPROVED_TRANSLATIONS` default true)
  exists so the model writes surrounding prose that flows into the kept
  lines; disobedience is harmless. Because it rides the prompt body it
  folds into the llm_cache key: confirming a segment intentionally busts
  the cache for a later retranslate (the approved text must reach the
  model and the merge must run against fresh output).
- **Refiner interplay: the store always mirrors the DISPLAYED body.** The
  refinement commit flips the displayed variant to refined, so the merge
  runs at that commit with kind='llm_refined' and `refined_text` is
  materialized from the merged set; the refiner's rewrites of protected
  rows are discarded from the body but recorded in `machine_text`.
  `retry_refinement` no longer nulls `refined_text`: confirmed work lives
  in the refined body and must not vanish (or desync the store) during
  the retry window; the fresh refinement commit overwrites it anyway.
  Refinement-stage `paragraph_count_drift` observations are now REPLACED
  on every clean re-refinement (they describe a refined_text that no
  longer exists); translation-stage rows are untouched.
  (CORRECTED same day, see the spec-review addendum below: keeping
  refined_text alone did NOT keep it displayed, and the retry window could
  still destroy confirmed work through an editor-open rebuild.)
- **Assist rail thresholds.** Exact tier = same-novel `chapter_segments`
  hash matches (full-text verified), ranked confirmed > edited > machine
  then recency, cap 5. Fuzzy tier = rapidfuzz over other chapters' segment
  sources at 0.80 (vs the consistency rail's 0.90: the rail SUGGESTS,
  consistency FLAGS drift), same canonical_zh folding / length-band /
  tiny-source conventions, cap 5. The fuzzy scorer lives in segments.py
  rather than importing consistency.py (which imports segments.py:
  ownership rule "only segments.py touches chapter_segments" wins over
  code reuse of ~20 lines).
- **"kept" chip keys on machine_differs, which is true for ANY human row
  whose stored AI rendering differs, including fresh edits.** Accepted per
  spec: the affordance ("view the AI's suggestion, apply = revert") is
  equally useful on a fresh edit, and distinguishing "refreshed by a later
  retranslate" would need extra state for no behavioral gain. The dialog
  fetches `machine_text` via the assist endpoint (one endpoint, cached by
  the rail) instead of a dedicated fetch or shipping it in the list
  payload.

### Addendum (same day, spec-review round 2)

- **CORRECTION: the retry-refinement window could destroy confirmed work,
  and the original Phase 4 entry mischaracterized the fix.** Retaining
  refined_text did not keep it displayed: displayed_body still keyed on
  refinement_status=='done', so retry_refinement's status flip to
  'pending' moved the DISPLAYED body to the draft for the whole window
  (and permanently on a failed retry). An editor GET during that window
  self-healed the store against the draft, text-authoritatively
  overwriting confirmed rows' target_text with draft text, and the
  subsequent refine merge then preserved that draft text as if the user
  had written it. The earlier claim that "the fresh refinement commit
  overwrites it anyway" was wrong about the store: the merge preserves
  whatever the human rows carry at commit time, including the clobbered
  text. The display flip also violated the standing no-draft-preview
  product rule.
- **Fix: displayed_body keys on refined_text PRESENCE, everywhere.**
  Refined text is canonical whenever it is non-empty, regardless of
  refinement_status: retained polish stays displayed through pending /
  in_progress and after a failed retry; first-ever refinements (refined
  NULL until commit) and retranslates (translate commit nulls refined)
  still display the draft. The rule now matches the FTS index
  (COALESCE(refined_text, translated_text)) exactly. Updated in one
  sweep: segments.displayed_body (store + consistency), the reader's
  _displayedEnglish / _paragraphTextAt / _editVariant, download_novel,
  and epub_export, so no consumer drifts. Belt-and-suspenders:
  get_segments now refuses to rebuild the store while refinement_status
  is pending/in_progress (serves stored rows as-is; the refine commit
  re-stamps them), mirroring the non-done chapter-status guard.
- **revert_machine now reaches the AI rendering behind a TM prefill.** A
  machine-status row whose target diverged from machine_text (tm_exact)
  previously 400ed ("already shows the AI translation"), making the fresh
  AI rendering unreachable. The swap is now allowed whenever machine_text
  is non-empty and differs from the target; origin lands on 'llm' (the
  worker merge is machine_text's producer and re-stamps on the next
  merge). The editor's TM chip is now clickable (same AI-suggests dialog
  as the kept chip, contextual note) and the Revert action covers these
  rows.
- **_anchor_human_rows verifies full source_text, not just the 16-hex
  hash.** prefill_confirmed_exact already did the collision check; the
  anchor path now applies the same verification, so a forged or colliding
  prefix can never silently anchor a human row to the wrong source
  paragraph (it retains-all and stamps 'unaligned' instead).

### Addendum (same day, quality-review round)

- **One canonical source recipe: segmentation.chapter_source_paragraphs,
  SEGMENTATION_VERSION 2.** The worker merges keyed on
  effective_source_paragraphs(strip_heading_update_marker(text)) while the
  lazy backfill / self-heal / reproject used the bare split, so a first
  line matching parser's broad title prefix (spaced 第 N 章, or Chapter N)
  but not segmentation's tight heading regex, while carrying an author
  update marker, produced different seg-0 sources per writer; the
  anchor's full-text check then failed and the whole chapter went
  retain-all unaligned over a cosmetic marker. All writers (both queue
  merges, build_segments_from_alignment, and any Phase 5+ addition, per
  the docstring) now call chapter_source_paragraphs, which composes the
  marker strip. Dependency direction: segmentation imports parser (parser
  is pure text with zero service imports; the reverse would cycle through
  tm's re-exports). The recipe change is a behavior change for that
  definable class, so the frozen-module contract required the version
  bump; the bump-triggered rebuild re-anchors human rows by hash plus
  full text for unaffected chapters and retains-all for the rare
  affected ones (both pinned by tests).
- **Prefill now rides the REFINE merge too.** Without re-passing
  prefill_confirmed_exact at the refinement commit, the refiner's wording
  replaced tm_exact renderings on machine rows, defeating the consistency
  point of prefill; the confirmed cross-chapter rendering now survives
  refinement and the refiner's own wording lands in machine_text.
- **Segment saves are paragraph-safe.** update_segment collapses
  blank-line runs in after_text to a single newline (a pasted \n\n would
  otherwise split the one-paragraph segment under split_target_paragraphs
  and desync the join==body count); the editor cell intercepts
  Shift+Enter to insert a single line break. SegmentPatch.after_text
  gained the EditParagraphRequest length ceiling.
- **Assist fuzzy tier bounded.** The candidate fetch carries a generous
  SQL LENGTH band (0.7x..1.6x raw source length, wider than the canonical
  0.85..1.15 band so folding slack cannot exclude a real candidate) and
  the fold+score runs in run_in_threadpool, so an uncached assist fetch
  on a large novel cannot stutter the event loop mid-translate.
- **Displayed-body restatements collapsed.** download_novel and
  epub_export call segments.displayed_body directly; the frontend keying
  lives in one reader-core _displayedVariant helper consumed by
  _displayedEnglish / _paragraphTextAt / _editVariant, and the "refined
  by X" pane chip is presence-keyed so it does not vanish mid-retry.

## 2026-08-03: CAT-pivot Phase 5, feed-the-AI + provenance TM surfaces + navigation

- **Confirmed exemplars are a separate block from approved translations.**
  APPROVED TRANSLATION EXAMPLES carries the 5 most recently confirmed
  segment pairs from OTHER chapters (recency-selected, deduped by source,
  400 chars/side) as voice precedent; APPROVED TRANSLATIONS stays the
  same-chapter verbatim-reuse feed. Both can appear in one prompt; the
  queue comments the distinction at the fetch site. Recency over
  chapter-relevance is deliberate: the block teaches voice, not
  vocabulary, so no relevance filter.
- **No PROMPT_TEMPLATE_VERSION bump for the exemplar block.** Same
  cache-safety argument as Phase 4's approved block: the block is absent
  when no exemplars exist, so a no-exemplar prompt is byte-identical to
  the pre-phase shape (pinned by test), and a present block folds into
  the llm_cache key by riding the prompt body. Confirming a segment
  therefore busts the cache for future translates of other chapters,
  which is the desired behavior.
- **Exemplar SQL lives in segments.py, the gate in prompt_inputs.**
  fetch_confirmed_exemplar_pairs is the single-owner query (segments.py
  is the only module with chapter_segments SQL, binding);
  prompt_inputs.fetch_confirmed_exemplars owns the
  PROMPT_INCLUDE_CONFIRMED_EXEMPLARS flag gate + limit at the fetch site
  like its sibling fetchers. prompt_inputs importing segments creates no
  cycle (segments never imports prompt_inputs).
- **TM read surfaces demoted tm_segments to chapter-level fallback.** The
  consistency corpus and concordance read chapter_segments first (status
  ranked confirmed > edited > machine > legacy) and use tm_segments ONLY
  for chapters with zero segment rows: a covered chapter's tm rows are
  suppressed outright (chapter_ids_with_segments), because the segment
  store is rematerialized on every editor write while tm rows go stale
  between translate commits. Suppression is chapter-level, not row-level,
  so a partial chapter with empty targets cannot leak stale tm text. The
  concordance merge lives in segments.py::concordance_search (segments
  already imports tm; tm.py importing segments would cycle). Response
  shapes stay backward-compatible: `status` is additive on
  OtherRendering and ConcordanceHit. tm_segments is still written at
  every translate commit; removing the write is future work.
- **editor-next semantics: "needs work" not "has unconfirmed rows".** The
  continue card's next chapter is the first (forward from `after`, then
  wrapped) that is untranslated OR storeless OR carries any non-confirmed
  segment; a storeless done chapter counts because its lazy build would
  be all-machine. Simple two-query forward/wrap implementation; the card
  memoizes per chapter and stale-guards so repeated updateActions calls
  at 100% do not refetch.
- **g e chord + spine 校 Editor entry.** Editor slots between Reader and
  Glossary in the spine (novel-scoped href with /library fallback), `g e`
  was a free chord slot, quality's worst-chapter worklist now deep-links
  to /editor (the reader's mode=edit worklist target retires with Phase
  6), and the reader util-menu carries an Open-in-CAT-editor item
  re-pointed on every chapter change. The reader edit-mode hint now
  pitches the CAT editor as the deep-editing surface (transition copy;
  full edit-mode retirement is Phase 6).

## 2026-08-03: CAT-pivot Phase 6, reader edit-mode retirement (redundancy directive)

- **The reader is a pure reading surface; the CAT editor is the single
  editing surface.** Deleted from the reader: the Read/Edit toggle,
  localStorage readerMode, body[data-reader-mode] CSS + the whole
  .edit-only scope, contenteditable paragraph editing (+ retry chips +
  style_edits capture UI), the select-to-add-glossary popover, the
  term-edit popover, the terms rail, the consistency rail, the aligned
  paragraph grid, and the QA/attempts/last-prompt/insert-chapter/
  style-note/learn-from-edits/concordance dialogs. reader-edit.js,
  reader-glossary.js and reader-consistency.js are gone; the survivors
  (nav, retranslate + pre-check confirm gate, copy chapter, shortcuts,
  type settings, keyboard, reading rail, boot, bookmarks) live in
  reader-chapter.js. Legacy `?mode=edit` URLs redirect client-side to
  /editor?novel=N&ch=M (reader-core.js top).
- **What moved where.** QA observations, attempts, last prompt,
  pre-check report, refresh-free-draft, insert-chapter, concordance
  (now search-box driven, selection prefills), learn-from-edits and the
  style-note dialog all live in the editor's new util menu
  (editor-tools.js; same <details> util-menu pattern, styles hoisted to
  base.css). The terms rail became a "Terms in this chapter" tier in the
  editor's assist rail (click a card to revise; + Add term for the blank
  form) and the consistency rail's locked-term tier became the
  "Missing locked terms" tier (client-computed over glossary x segments,
  click jumps to the offending row); the fuzzy-match half of the old rail
  was already covered by the assist rail's TM tiers. The select-to-add
  popover was REIMPLEMENTED clean on the segment grid (Add / Revise /
  Concordance) rather than porting the reader's selection machinery,
  and the add/revise forms are one term-form dialog (revise with an EN
  change keeps the reader's apply-in-place behavior and reloads the
  grid, since the backend reprojects segments in the same transaction).
- **Kept in the reader**: bilingual/source toggles (incl. free-draft
  display), TOC, bookmarks (a reading aid; wiring moved into
  reader-chapter.js), quality badge (now un-gated from edit mode; its
  popover links to the editor for triage instead of the retired QA
  panel/rail), retranslate + retry-refinement, downloads, copy chapter,
  append link, Open-in-CAT-editor link.
- **The aligned bilingual grid was deleted, not kept as reading chrome.**
  Bilingual read mode returns to the two independent panes (the grid's
  own fallback). Judgment call: the grid's row pairing served line-level
  comparison for EDITING (the editor's server-computed segment grid is
  that surface now), the 1:1 pipeline makes new translations align by
  construction anyway, and keeping it meant keeping the duplicate
  client-side _alignParas aligner the plan explicitly retired.
- **style_edits producer sunset.** POST /edit-paragraph (+ its segment
  reroute and observations-refresh helpers, EditParagraphRequest, the
  api.js wrapper, segments.seg_index_for_display_paragraph) is deleted;
  the editor's segment PATCH is the only paragraph write.
  learn_from_edits now derives proposals from the ledger
  (segments.edited_pairs_for_chapter: machine_text vs target_text on
  edited|confirmed rows, same proposal shape). style_edits KEEPS its
  historical rows: fetch_style_edits still feeds the USER STYLE
  PREFERENCES prompt block: but gains no new rows in-app (the CLI
  ingest_edited_chapter path can still stage rows for out-of-app edits);
  dropping the table is future work via the dead-column path.
  quality_dashboard's cache token swapped its style_edits aggregate for
  segments.novel_segment_edit_stamp (chapter_segments.updated_at bumps
  on every editor write). Observation rows now refresh only at translate
  commits; a hand-fixed observation is dismissed from the editor's QA
  panel (the per-edit observer re-run died with the endpoint).
- **Free-draft lane deliberately kept as-is** (generation on chapter
  open, reader source toggle, refresh action in the editor util menu).
  Its long-term fate: reference column in the editor vs removal: is
  explicitly the user's call; do not remove it as "dead" cleanup.
- **CI red since 2026-07-31 was ruff version drift, not code.** The dev
  extra had unpinned "ruff>=0.6"; CI began installing ruff 0.16.1 whose
  widened default rule set flags ~386 pre-existing findings (S110,
  BLE001, ...) in untouched files, while local 0.15.x stayed green
  (every Phase 2-5 push shows the same failure). Fixed by pinning
  "ruff>=0.6,<0.16" so CI matches the local toolchain; a deliberate
  ruff-upgrade sweep (fix or ignore the new rules, then lift the pin) is
  follow-up work, not a lint-churn side effect of Phase 6.
- **Missing-locked tier consumes the server, not a reimplementation
  (review fix).** The first Phase 6 cut computed the editor's
  missing-locked warnings client-side over ALL locked entries with naive
  substring matching, silently re-widening what 2026-06-23 deliberately
  narrowed (atomic-only via glossary.missing_translator_terms; the naive
  all-locked matcher class was measured ~96% false-fire). The tier now
  fetches GET .../consistency (api.getChapterConsistency, dead since the
  reader rail retired, is live again) and renders its glossary_flags;
  click-to-jump locates the segment by term_zh aliases client-side with
  the server's paragraph_index as a hint. The route's fuzzy `matches`
  tier is deliberately IGNORED rather than the route slimmed: the assist
  rail's per-segment TM tiers already cover fuzzy reuse, and keeping the
  route whole keeps services/consistency.py the single owner of both
  detectors with zero churn. Refetch is debounced (600ms) on segment
  edits. The terms-in-chapter listing stays client-computed on purpose:
  it is a neutral inventory, not a warning, so naive matching is fine.
