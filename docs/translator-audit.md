# Translator audit — root cause of "the output feels off"

Status: diagnosis (2026-06-14). No pipeline behavior is changed by this audit.
Two artifacts ship with it:

- this document — the component-by-component audit;
- `backend/scripts/glossary_register_audit.py` — a read-only, corpus-scale
  instrument that measures the root cause against the live install.

The audit was seeded by one chapter (ch.421 "Sword Art": the translator output
beside a known-good revision) but its claims are scoped to **the whole novel and
the whole corpus** — ch.421 is the symptom that exposed the mechanism, not the
sample the conclusions rest on.

---

## 1. Executive summary

Two surface complaints were raised: **the deterministic fixups mangle correct
model output**, and **the English prose register feels off** (ornate, passive,
fragmented, archaic). They look like two problems. They are not. Tracing both to
ground, they share a single root:

> **The per-novel glossary is auto-grown, never pruned, Title-Cases generic
> common nouns at write time, and is then re-injected into every prompt *and*
> used to drive the deterministic casing layer. One polluted artifact therefore
> corrupts prose mechanically *and* trains the model's register — and the
> previous-chapter tail compounds the drift across the whole novel.**

```
model TERMS envelope (casing as-emitted)        base.py::_parse_new_terms_block
        │  no re-casing at parse
        ▼
ADMISSION: zh in 【】 OR recurs ≥2×              glossary_filters.py::filter_glossary_candidates
        │  ✗ NO abstract / common-noun stop-list  → 圆满, 境界, 金性 all admitted
        ▼
WRITE CASING: keep emitted casing unless in a    glossary_casing.py::_normalize_extracted_casing
  hardcoded ~20-word GENERIC_LOWERCASE set;       GENERIC_LOWERCASE (:27-42)
  named categories arriving lowercase are
  force-Title-Cased                               → "Perfection" / "Realm" / "Golden Nature" persist Title-Cased
        │  ✗ no demotion / re-evaluation, ever
        ▼
   ┌──────────── the SAME stored entry feeds both legs ────────────┐
   ▼ PROSE LEG (mechanical)                       ▼ REGISTER LEG (training)
enforce_locked_term_casing stamps the stored      format_glossary injects the FULL glossary
casing onto every body occurrence  → D1           into GLOSSARY MASTER every chapter, with no
text_fixups.py:533                                casing-interpretation guidance  → D4 / D7
                                                  base.py::format_glossary
        └───────────────────────────┬───────────────────────────┘
                                     ▼
   PREVIOUS CHAPTER TAIL: chapter N's translated_text becomes chapter N+1's
   voice anchor (positioned just before the source; only a "brief wins"
   disclaimer). No de-entrenchment → register drift COMPOUNDS across the novel.
   prompt_inputs.py::fetch_previous_chapter_tail  (PREVIOUS_CONTEXT_ENABLED default true)
```

The consequences for remediation are concrete:

- The register defects (ornament, passive, fragmentation, archaic diction) are
  **violations of `base.md` rules that already exist and are already correct**
  (the prompt's own phase-history comments show these exact tensions were found
  and fixed in earlier passes). **Rewriting `base.md` will not fix them** — that
  would be treating a symptom of the training pressure coming from the glossary
  and the tail.
- Guarding `enforce_locked_term_casing` fixes only the *prose leg* of the casing
  defect — also a symptom.
- A one-novel glossary cleanup is also symptom-level: it leaves the lifecycle in
  place to re-pollute that novel and every other.

The root fix is therefore **system-wide**: change admission + write-time casing
so no novel accumulates the pollution, migrate the glossaries that already carry
it, validate idiom entries store senses not images, and break/dampen the tail
feedback loop. See §4.

---

## 2. Evidence — ch.421 defects, attributed

`D#` are the distinct defect classes seen in the translator-vs-revision pair.
"Leg" ties each to the root in §1.

| # | Defect (translator → revision) | Attribution | Component / file:line |
|---|---|---|---|
| D1 | `Perfection`→`perfection`, `Realm`→`realm`, `Golden Nature`→`golden nature` | root: stored Title-Cased; prose leg when category enforces it, else register leg (model copies casing) | `glossary_casing.py::_normalize_extracted_casing`; `text_fixups.py:533 enforce_locked_term_casing` |
| D4 | added ornament: "chives flourishing beautifully… beyond price" vs "a particularly fine leek… rare treasure" | register leg; violates `base.md:5,12` | glossary/tail training (rules already correct) |
| D7 | archaic/proverb diction: "what flourishes to its peak must decline" vs "what goes up must come down" | register leg; violates `base.md:1,26,28,31,33`, `xianxia.md:1` | same |
| D8 | idiom as literal image: 韭菜→"chives", 登堂入室→"entered the hall"/"mastery" | root: idiom entry stores image, OR model amplifies | glossary idiom rows; `base.md:7,40` |
| D5 | passive inflation: "the mark had been left by Sword Intent" vs "had left the mark with his sword intent" | register leg; violates `base.md:16`, `xianxia.md:9` | rules already correct |
| D6 | over-fragmentation: 3 one-line paragraphs vs 1 flowing | **model output, NOT a fixup** — the first paragraph ends in `.`, and `enforce_mid_sentence_comma_break` only joins when the previous line ends in a comma/semicolon; violates `base.md:20` | model |
| D2 | proper name lowercased: "spirit-rhinoceros bright jade" vs "Spiritual Rhinoceros Bright Jade" | root (a lowercase-noted entry) OR model | `text_fixups.py:765 enforce_lowercase_locked_terms`; instrument decides |
| D3 | space→hyphen: "spirit-rhinoceros" | **model output, NOT a fixup** — no fixup inserts hyphens (`enforce_spaced_hyphen_dash` only removes spaced hyphens) | model |
| D9 | mid-scene divider dropped before "Extreme Heaven Cliff, the Jade Pivot." | uncertain; likely model — the preceding line ends in `…`, not a joinable comma, so the comma-break join would not fire here; also **no `base.md` rule mandates scene dividers** | `text_fixups.py:954`; rules gap |

**Net:** D4/D5/D7 are register violations of correct rules → the driver is the
glossary/tail training, not the prompt text. D1/D8/D2 trace to the glossary
lifecycle. D6/D3 are the model's own output. D9 is most likely the model plus a
small rules gap.

**Scope guard.** ch.421 is the seed, not the sample. Every class above is
re-checked across all of this novel's chapters and across every novel in the DB
via the instrument (§5); anything that does not reproduce beyond the one chapter
is an anecdote, not a finding.

---

## 3. Component-by-component

Pipeline order, with the **glossary lifecycle as the spine**. Each entry:
*Role · Degradation surface · Findings (→D#) · Confirm.*

### 3.1 Glossary write path — the root

`base.py::_parse_new_terms_block` → `glossary_filters.py::filter_glossary_candidates`
→ `glossary.py::merge_new_terms` → `glossary_casing.py::_normalize_extracted_casing`.

- **Role:** turn the model's TERMS envelope into stored glossary rows.
- **Degradation surface:** what is admitted, and what casing it is stored with,
  becomes load-bearing for every future chapter.
- **Findings:**
  - **Admission has no genericness filter** (`filter_glossary_candidates`): a
    term is admitted if its `zh` is bracketed in 【】 *or* recurs ≥2× in the
    chapter. Abstract common nouns (圆满 "perfection", 境界 "realm") recur
    constantly and are admitted unconditionally. (→ D1, D4/D7 training.)
  - **Write-time casing Title-Cases by default** (`_normalize_extracted_casing`):
    the only down-pressure is the hardcoded ~20-word `GENERIC_LOWERCASE` set
    (`glossary_casing.py:27-42`); anything outside it keeps the model's casing,
    and named-category terms arriving lowercase are *force* Title-Cased. So
    generic abstracts persist Title-Cased. (→ D1.)
  - **No demotion / re-evaluation:** once auto-inserted (`auto_detected=1,
    locked=0`), an entry is never reconsidered; a manual edit only locks it.
  - **Idioms ride the same path** with no sense-vs-image validation: a
    first-occurrence literal rendering is captured and then reused every chapter.
    (→ D8.)
- **Confirm:** instrument Report 1 (per-novel pollution counts + corpus
  cross-novel frequency ranking).

### 3.2 Glossary read path — the register leg

`base.py::build_prompt` → `format_glossary`, with a per-chapter relevance filter.

- **Role:** render the glossary into the prompt's `GLOSSARY MASTER` /
  `GLOSSARY THIS CHAPTER` blocks.
- **Degradation surface:** the model sees `term_zh → term_en` with the stored
  (possibly Title-Cased) casing and **no instruction on how to interpret that
  casing in prose**. Title-Cased abstractions read as proper/weighty nouns and
  are echoed in that register.
- **Findings:** the full per-novel + global glossary (minus shadowed globals) is
  fetched (`global_glossary.py::list_for_novel_with_globals`) and filtered to
  chapter relevance only at build time; a long-running novel injects a large,
  Title-Case-heavy block every call. (→ D4/D7.)
- **Confirm:** instrument Report 1 "Title-Cased entries injected into prompts"
  per novel.

### 3.3 Continuity tail — the compounding loop

`prompt_inputs.py::fetch_previous_chapter_tail` → `build_prompt` context block.

- **Role:** carry voice/names/honorifics across chapters.
- **Degradation surface:** the tail is the **previous chapter's own English
  output** (`translated_text`, last 4 paragraphs, default on), placed
  immediately before the source text. Whatever register chapter N drifted into
  is fed to chapter N+1 as the thing to match.
- **Findings:** there is no de-entrenchment. The "the brief and overlay win over
  this tail" disclaimer is the only counter-pressure, and it sits far from the
  generation point. This is the mechanism that turns a per-chapter wobble into a
  novel-wide drift. (→ D4/D5/D7 amplification.)
- **Confirm:** single-variable A/B with `PREVIOUS_CONTEXT_ENABLED` off (§6).

### 3.4 Deterministic fixups — the prose leg

`queue.py::_apply_text_fixups` over `text_fixups.py` (re-run identically on
refiner output).

- **Role:** mechanical post-translation cleanup (casing, dashes, brackets,
  paragraph joins).
- **Findings:**
  - `enforce_locked_term_casing` (`:533`) stamps each *locked atomic* term's
    stored casing onto every whole-word body occurrence, case-insensitively —
    no common-word guard. A locked atomic generic ("Golden Nature", or a
    single-word `character/place/technique/item` term) is force-cased into prose
    regardless of sense. (→ D1.) Note the gate `is_atomic_case_locked_term`
    returns `True` *unconditionally* for single-word named-category terms
    (`glossary_casing.py:129-132`).
  - `enforce_lowercase_locked_terms` (`:765`) down-cases lowercase-noted entries
    — the only fixup that could produce D2, and only if such an entry exists.
  - `enforce_mid_sentence_comma_break` (`:954`) joins a paragraph onto the next
    when the previous ends in `,;，；、` and the next does not open dialogue;
    candidate for D9 only when a divider follows a comma-ending line (not the
    case in ch.421).
  - No fixup inserts hyphens (D3 is model) and the chain is **re-run on refiner
    output**, doubling exposure.
- **Confirm:** instrument Report 2 (pre-fixup body vs each transform).

### 3.5 System-instruction composition — already correct

`base.py::build_system_instruction` + `base.md` + `genres/xianxia.md` +
`examples/xianxia.md`.

- **Finding:** rules governing D4–D8 exist and are clear, and the prompt's own
  phase-history comments record these exact tensions (literary vs. wuxiaworld
  register; "flatness ok" licensing one-line restarts; idiom image vs. sense)
  being found and **already resolved**. The register defects are the model
  regressing against correct rules under the glossary/tail training pressure.
  **Do not add or rewrite rules to chase them.**

### 3.6 Observers — log-only blind spot

`text_observers.py` via `queue.py` (`body_correctness_observations`).

- **Finding:** the register violations D4–D8 are exactly what observers could
  flag, but observers neither retry nor surface to the reader (INFO logs only).
  The single-pass thesis leaves register regressions invisible in production.

### 3.7 Refiner — secondary regression source

`refiner.py`; English-only second pass, re-runs the fixup chain.

- **Finding:** when configured, a second model polishing without the source can
  flatten/drift; its system instruction is ignored by 3/4 backends in
  `_complete_plain` (folded into the user prompt as a documented workaround).
  Candidate contributor to D4–D8 on novels that use a refiner.

---

## 4. Remediation — root vs symptom, at corpus scale

Diagnosis only; nothing here is applied in this pass. Each **root** item is
system-wide: a code change that protects *every* novel and all future
extraction, **plus** a one-time migration that cleans the glossaries already
polluted — not a manual per-novel edit.

**ROOT (system-wide):**

1. **Admission stop-list / abstractness gate** so generic abstracts never enter
   any novel's glossary (`filter_glossary_candidates`). Seed it objectively from
   the instrument's cross-novel frequency ranking (§5), not by hand.
2. **Write-time casing** (`_normalize_extracted_casing`): drop the blanket
   Title-Casing of named categories; widen down-pressure beyond the ~20-word
   `GENERIC_LOWERCASE` set, again seeded corpus-wide.
3. **Migration / re-evaluation pass** that demotes or down-cases the polluted
   entries already sitting in every novel's glossary (additive, idempotent).
4. **Idiom sense-not-image validation** across the corpus (flag/repair idiom
   rows whose `term_en` is a literal image or Title-Cased).
5. **Dampen/break the tail feedback loop:** source it post-refiner, strip
   Title-Cased generics from the tail before injection, or A/B
   `PREVIOUS_CONTEXT_ENABLED` off and measure.

**SYMPTOM (necessary but insufficient):**

- Common-word guard in `enforce_locked_term_casing` / `is_atomic_case_locked_term`
  (stops the prose leg, leaves the prompt training and the polluted store).
- A scene-divider preservation rule (D9 gap).
- A one-novel glossary cleanup (leaves the lifecycle intact).

**NOT a fix here:** editing `base.md` for D4–D8 — those rules are already
correct (§3.5).

Ranked: **1 → 3 → 2 → 5 → 4**, then the symptom guards. (1 and 3 stop new and
clean old pollution; 2 removes the casing pressure; 5 stops the compounding; 4
addresses idioms; the guards backstop the prose leg.)

---

## 5. The instrument

`backend/scripts/glossary_register_audit.py` — read-only, no LLM calls, defaults
to the whole corpus.

```
python -m backend.scripts.glossary_register_audit              # whole corpus
python -m backend.scripts.glossary_register_audit --novel 7    # one novel
python -m backend.scripts.glossary_register_audit --limit 50   # chapters/novel, fixup pass
python -m backend.scripts.glossary_register_audit --no-fixup-delta
```

**Report 1 — glossary root signal (always runs, cache-independent).**
Per novel: count of entries that would be *force-cased into prose* (locked
atomic, non-generic), count of *Title-Cased entries injected into prompts*, and
*idiom rows stored Title-Cased* (stored-image suspects). Then the **corpus
rollup**: every `term_en` ranked by cross-novel document frequency. A term
auto-extracted + Title-Cased across many distinct novels is generic by
definition — this ranked list is the objective seed for remediation items 1 and
2, and it is the audit's shipped appendix.

**Report 2 — fixup delta (best-effort, needs the on-disk `llm_cache`).**
Reconstructs the translator cache key exactly as the worker does
(`backend._begin_chapter` over the same fetched inputs), recovers the
**pre-fixup** model body, and runs each `enforce_*` in isolation over it,
reporting per-transform fire counts and sample rewrites. This settles D1/D2/D9 —
*did the fixup change correct output, or did the model produce it?* Coverage is
partial by design: a chapter only resolves while its glossary/prompt still hash
to the cached key (the glossary grows over time), so misses are reported, not
hidden. Report 1 carries the root argument; Report 2 corroborates the prose leg
where the cache allows.

---

## 6. Verification

- `python -m backend.scripts.glossary_register_audit` on the live install →
  confirm the root reproduces **beyond ch.421**: the per-novel pollution metric
  is non-trivial for this novel across its chapters *and* for other novels, and
  the corpus rollup shows 圆满/境界/金性-class `term_en` Title-Cased across
  multiple novels (lifecycle, not a one-off model slip).
- Confirm D1 origin per chapter via Report 2: if a chapter's pre-fixup body
  already reads "Perfection", the glossary/model trained it; if it reads
  "perfection" and the transform up-cases it, the fixup did. (In ch.421 the
  single-word `other` term "Perfection" is *not* force-cased — so its appearance
  in prose is model-side, which still indicts the glossary's prompt injection.)
- D1 unit reproduction anchoring a future fix: a locked atomic
  `GlossaryEntry(category='technique', term_en='Perfection')` through
  `enforce_locked_term_casing` over "closer to perfection" → expect the wrongful
  up-case; land as a (currently failing) test under `backend/tests/`.
- Feedback-loop hypothesis: single-variable A/B with `PREVIOUS_CONTEXT_ENABLED`
  off vs on, diffed against the ch.421 revision via `scripts/diff_against_edit.py`.
- `pytest backend/tests` stays green — this audit adds only a read-only script
  and (optionally) a test; it changes no pipeline behavior.
```
