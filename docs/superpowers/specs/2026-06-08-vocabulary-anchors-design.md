# Cross-chapter vocabulary anchors

Design doc. 2026-06-08.

## Problem (the root, not the symptom)

Every chapter is translated as an independent, stateless call. The glossary
gives the model memory for named terms (people, techniques, places), so those
never drift. But for the connective vocabulary, the recurring verbs and
adjectives that are not plot-important enough to be glossary terms, the model
has no memory of how it rendered a word in earlier chapters. So a word like
`虚幻` is re-rolled between "illusory" and "phantasmal" each time it appears,
with zero knowledge that earlier chapters settled on "illusory."

`illusory` vs `phantasmal` is one symptom. The root is: **no cross-chapter
rendering memory for non-glossary recurring vocabulary.** Pinning individual
words is a point-fix; we build the mechanism instead.

Note on scope, established empirically this session: most variation is
*correct* (`神色` renders as "expression" consistently; its modifiers vary
because the source varies). The drift worth fixing is the narrow set of
multi-option words that coin-flip between near-synonyms. The mechanism must
target those without flattening the correct variation.

## Goals / non-goals

- Goal: a recurring non-glossary word stays consistent across chapters, even
  across large gaps (occurrence in ch 311, next in ch 330).
- Goal: zero routine effort; self-maintaining with manual override available.
- Non-goal: forcing identity. The mechanism is soft. It never rewrites output
  and never overrides the model's contextual judgment. A word that genuinely
  shifts meaning is free to render differently.
- Non-goal: a perfect detector. Softness covers detector noise (see below).

## Mechanism

A persistent, per-novel **vocabulary anchor ledger**: `源词 -> canonical
English`, stored and injected into every chapter's prompt that contains the
source word. It is gap-immune for the same reason the glossary is: it is a
stored table consulted every time, not a context window.

Critically, it is **not** the glossary and has **softer** semantics. The
glossary enforces (prompt-pins plus `enforce_locked_term_casing`); these words
change meaning by context, so the anchor is injected as a default suggestion
that explicitly grants permission to vary, never as a fixed term and never
post-enforced.

### Three phases, measure before committing

**Phase 1, build engine + drift report (this deliverable, read-only).**
Deterministic offline analysis over the now-corrected TM (`tm_segments`,
paragraph-aligned source<->target). No schema change, no prompt change.

- Candidates: Chinese character bigrams/trigrams whose chapter-frequency is
  >= K, excluding existing glossary `term_zh`, `【...】` spans, and a small
  function-word stoplist. Character n-grams avoid a segmenter dependency
  (`jieba`/spaCy), keeping the EXE lean; 2-char content words like `虚幻`
  fall out naturally. Overlapping n-gram noise is filtered by the next step.
- Rendering extraction: for each candidate `W`, take the TM target paragraphs
  whose source contains `W`; score English content-words (lowercased, minus an
  English stoplist, glossary renderings, and capitalized proper nouns) by
  association (PMI / smoothed Dice) with `W` across the parallel corpus. The
  short one-line-per-sentence paragraphs make this signal clean. Top-associated
  English token(s) above a min count are `W`'s rendering(s).
- Drift + canonical: `W` drifts if it has >= 2 distinct rendering tokens, each
  used in >= 2 chapters. Canonical = most-used (auto-dominant). Output is a
  human-readable report: `W`, its renderings with counts and chapter spans,
  the proposed canonical, and sample sentence pairs.

We run Phase 1 on novel 2 and review it together. If it surfaces real drift
(illusory/phantasmal and siblings) with few false positives, proceed. If it
finds almost nothing, stop: the problem was not real and no prompt budget was
spent.

**Phase 2, storage + override (after review).** A dedicated per-novel
`vocab_anchors` store (NOT the glossary, to keep glossary enforcement away
from polysemous words). Auto-created from the Phase 1 dominant pick; every
entry visible and user-editable (change canonical, or disable an anchor whose
word genuinely varies). Chapter-scoped lookup mirrors
`filter_glossary_for_chapter`.

**Phase 3, soft injection (after Phase 2).** A compact, chapter-scoped prompt
block listing only anchors whose source appears in the current chapter, as
data with explicit variation permission, e.g. "Preferred renderings (use
unless the context calls for otherwise): 虚幻 -> illusory". Gated by a flag,
shipped OFF by default, A/B'd against a ground-truth chapter before any
default flip, per the binding graduation rule in CLAUDE.md.

## Why this respects the guardrails

- Data, not instructions (a `源 -> en` list, like the glossary block).
- Soft: never flattens correct variation, never force-corrects. Resolves the
  "too obvious / harmful" concern.
- Gap-immune: stored and injected every chapter, not a context window.
- Auto with override: zero effort by default, manual control of the few
  canonicals that matter.
- Detector noise is tolerable precisely because injection is soft: a wrong
  anchor only nudges, it cannot force a bad rendering.

## Invariants / risks

- The anchor is never enforced in post-processing and never casing-stamped.
  (Distinguishes it from the glossary.)
- Verify-before-inject: Phase 1 must show real drift before Phase 3 is built.
- v1 association captures single-word adjective/noun renderings best (the
  illusory/phantasmal case). Multi-word renderings ("in one fell swoop") and
  heavy morphological variation are weaker; documented, not silently dropped.
- False drift (a candidate whose two top renderings are different real
  concepts, not synonyms) is filtered by the human review gate and, failing
  that, harmlessly absorbed by soft injection.
- No default flip without a single-variable A/B vs a ground-truth chapter.

## Phase 1 deliverable

A read-only script (`backend/scripts/`) that runs the build engine on a novel
and prints the drift report. No DB writes, no prompt changes. Output reviewed
with the user to decide whether Phases 2-3 are warranted.
