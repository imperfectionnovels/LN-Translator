# Bundle F: Glossary, Glossary-Data, Test-Pin Trial

Generated: 2026-06-11. Adversarial trial: every rule presumed faulty; survives only with evidence.

---

## Claimed Rule IDs

### Glossary (prompt rules)
BASE-GLOS-01, BASE-GLOS-02, BASE-GLOS-03, BASE-GLOS-04, BASE-GLOS-05, BASE-GLOS-06, BASE-GLOS-07

### Glossary-Data (casing and storage policy)
GLOS-01, GLOS-02, GLOS-03, GLOS-04, GLOS-05, GLOS-06, GLOS-07, GLOS-08, GLOS-09, GLOS-10, GLOS-11, GLOS-TITLELEN

### Test-Pin
TEST-01 through TEST-22

### Mis-categorized rows noted
XIA-11, XIA-12 (ledger category: genre-signature; functionally glossary-data: enumerate specific self-reference renderings and noun disambiguation).
BASE-FMT-05 (ledger category: casing; directly governs glossary-term casing in sentence-initial position; cross-bundle dependency flagged below).

---

## Evidence Base Summary

### WW corpus
Ten chapters from WuxiaWorld: ISSTH ch1-2 (Deathblade), AWE ch2 (Deathblade), CD ch1 (RWX), RMJI ch1 (Johnchen/DoubleDD), SOTR ch1 (etvolare), MW ch1 (hyorinmaru), ED ch1, DE ch1, AWE ref ch1, RI ch2.

Key casing findings from corpus scan:
- Realm names Title-Cased throughout: "Foundation Establishment", "Qi Condensation", "Nascent Soul" all Title-Cased in every occurrence.
- Sect names Title-Cased: "Seven Mysteries Sect", "Spirit Stream Sect", "Heng Yue Sect" always Title-Cased.
- Generic cultivation energy always lowercase: "battle qi" (11x in CD ch1 never capitalized), "true qi" (5x in SOTR ch1 never capitalized).
- Stage modifiers: no hyphenated "early-stage / mid-stage / late-stage" forms appear in the corpus sample; the corpus just uses bare realm names without stage modifiers in the chapters sampled.
- Pronoun-to-name ratio: ISSTH ch1 "Meng Hao" 25x, pronouns (he/him/his) 89x, ratio 3.6:1. AWE ch2 "Bai Xiaochun" 38x, he/him/his 71x, ratio 1.9:1.

### Phase14 chapters
Novel 2, chapters where prompt_template_version = 'phase14-english-cadence-1': ch392, 396, 405, 413, 421, 422, 424, 425, 426, 427, 428, 429, 430, 431, 432, 433, 434, 435.

### Glossary DB state (live/dev)
2139 entries for novel_id=2: character 264, idiom 234, item 196, other 745, place 336, technique 364. Locked 2083, unlocked 56.

---

## Per-Rule Trials

### BASE-GLOS-01: Glossary is memory; render each term with its given wording; longest-match wins.

**Evidence:**
- Corpus practice: consistent per-term rendering is the WW norm. Deathblade uses "Foundation Establishment" identically across ISSTH ch1 and AWE ch2. RWX uses "battle qi" identically (lowercase) throughout CD ch1.
- Phase14 output: glossary terms are consistently rendered in the chapters examined. "Demon-Purging True Person" appears 192x across phase14 with no variant spellings. "No-Killing Sword Intent" 9x in ch426 without deviation.
- Longest-match rule: no direct test found in phase14 sample, but the mechanism in base.py correctly sorts entries longest-first by term_zh (TPL-09), so this is enforced at the data layer not just the prompt level.
- The "longest-match wins" clause is the only way to handle overlapping entries like 中期 inside 金丹中期. Without it, the shorter entry would fire on every occurrence of 中期 inside compound terms.

**Reasoning:** Rule matches corpus practice. Longest-match subclause prevents sub-entry decomposition (see also BASE-GLOS-02). No evidence of harm.

**Verdict: keep**

---

### BASE-GLOS-02: A shorter entry never reaches inside a longer word: an unlisted compound reads whole, by sense in context, never through a sub-entry's gloss.

**Evidence:**
- Phase14 ch425: source has 佛门功法 (Buddhist cultivation techniques). Glossary entry is 功法 -> "cultivation technique". The model rendered it as "Buddhist techniques", not "Buddhist cultivation technique". This is BASE-GLOS-02 working correctly: 佛门功法 is a compound; 功法 as sub-entry does not decompose it. The scorer flagged this as a "cultivation technique miss" in ch425 -- that is a FALSE POSITIVE. The rendering "Buddhist techniques" is correct prose-compression of the compound; "Buddhist cultivation techniques" would have been redundant (佛门 already implies the cultivation context).
- Glossary also has 金丹中期 -> "mid-stage Golden Core" and 中期 -> "mid stage". Longest-match (金丹中期) fires when the realm qualifier is present. Without GLOS-02, standalone 中期 could be misread as sub-entry of a longer compound.

**Reasoning:** Rule is load-bearing. Without it the model would decompose compounds using sub-entries, producing calque artifacts. The ch425 scorer false-positive confirms the rule is working.

**Verdict: keep**

---

### BASE-GLOS-03: A name, title, place, technique, or scripture is a fixed label: whole, in its given form and casing, inflected only grammatically, the sentence recomposed around it.

**Evidence:**
- Phase14: technique names like "No-Killing Sword Intent", "Adamant Dao Nirvana Sword Art", "Heaven-Slaying Sword" appear inflected grammatically ("broke the No-Killing Sword Intent", "received the sword art") but never paraphrased or split.
- One exception: "Yun Family" (云家, capital F in glossary) appears 42x as "Yun family" (lowercase f) across all novel_id=2 done chapters, vs. 2x as "Yun Family". The 42 lowercase occurrences are almost all in the possessive compound "Yun family's Old Ancestor" -- the model keeps the proper name but lowercases "family" in the possessive form. This is a casing enforcement gap, not a BASE-GLOS-03 violation per se: the name is kept whole but the second word of the compound loses its capital. See GLOS-DATA fixes section.

**Reasoning:** Rule is load-bearing for term atomicity. The Yun Family casing gap is a fixup/casing issue, not a failure of this rule's core purpose.

**Verdict: keep**

---

### BASE-GLOS-04: The label fixes the form, not the frequency; Chinese re-sounds a full title at every mention; English keeps the weight-bearing uses whole, a pronoun or plain role noun between; never a nickname or clipped label.

**Evidence:**
- Phase14 ch424: "Demon-Purging True Person" 30x, he/him/his 47x, pronoun:full-title ratio 1.6:1. WW ISSTH reference: "Meng Hao" 25x, pronouns 89x, ratio 3.6:1. Our ratio is about half the WW reference.
- Phase14 ch426: "Demon-Purging True Person" 30x, ratio 2.4:1. Still below WW reference.
- Phase14 ch429: "Evil Quelling True Person" 17x, "he" alone 44x, ratio 2.6:1.
- Within-300-char repetitions in ch426: 15 of 29 full-title uses occur within 300 characters of the previous use. Examples: "Demon-Purging True Person held him back ... 'No need.' Demon-Purging True Person..." (gap 103 chars). This is over-frequency compared to WW practice.
- WW corpus: in AWE ch2, "Big Fatty Zhang" 8x, he 49x, ratio 6.1:1. In the WW sample no character name appears twice within 100 chars.

**GLOS-TITLELEN investigation (open question):**
The issue is not the glossary entry length -- "Demon-Purging True Person" is 4 words, not excessively long. The issue is that the model re-sounds the full title on almost every sentence-level reference (ratio 1.6:1 vs. WW's 3.6:1). Two separate problems:
1. Very long titles (e.g. "True Monarch of Adamant Form Who Proclaims the Dao", 9 words): these appear 0x in phase14 chapters (the character is off-screen), so the frequency problem does not apply to them yet. When they appear, full re-use in close succession would be visually heavier.
2. Medium titles (4-5 words: "Demon-Purging True Person", "Evil Quelling True Person"): over-frequency is the live problem now.

**Count: full-title repetitions within 300 chars, ch426: 15 of 29 uses.** The rule's stated intent (weight-bearing uses whole, pronouns between) is correct but not being achieved. The rule's wording is fine; the problem is the model is not obeying it well enough.

**Recommendation (concrete, for GLOS-TITLELEN):** Keep the glossary entry lengths as-is. The fix is at the prompt level, not the data level. The EX-04 example already demonstrates the intended pattern. The open question is user-decision on whether to add a worked example specifically for 4-5 word Daoist titles to reinforce frequency discipline. Current data says the ratio target (pronouns:full-title > 2.5:1) is not met in single-viewpoint action chapters.

**Verdict: keep** (rule text is correct; under-application is a model compliance issue, not a rule defect)
**GLOS-TITLELEN verdict: user-decision** with recommendation: no glossary entry shortening; add an action-chapter pronominalization example to the xianxia worked examples showing a 4-word title with 3+ pronoun uses between full invocations.

---

### BASE-GLOS-05: '/' alternatives: whichever fits. An idiom entry records a sense, not a script: keep its image; a sentence-shaped one recasts to fit. An action entry is a predicate: conjugate it.

**Evidence:**
- Phase14 glossary has slash-alternative entries like "后期 / 後期" -> "late stage", "不受奔 / 不南奔" -> "No Escape South". The "/" alternatives are handled by the prompt's "whichever fits" guidance.
- Action entries: "两面包夹" -> "pincer attack" and similar pattern. No observed violations of noun-wrap vs. conjugation in phase14.
- The tension identified in Section B of the ledger: BASE-FID-03 (flatten idiom image) vs. BASE-GLOS-05 (idiom entry records a sense, keep its image). After close reading: these address different surfaces. BASE-FID-03 governs unlisted idioms in running text. BASE-GLOS-05 governs idiom entries in the glossary. The glossary idiom sense is the translator's resolved rendering; the model is told to use it rather than re-derive the image.

**Reasoning:** Rule is load-bearing for slash-alternative flexibility and prevents action entries from being nominalized.

**Verdict: keep**

---

### BASE-GLOS-06: When the source attaches a verb to a term, keep both term and verb.

**Evidence:**
- Phase14 ch425: source has two 出手 occurrences near "Demon-Purging True Person". First: 荡魔真人又伸出手 (Demon-Purging True Person reached out). Translation: "Demon-Purging True Person reached out, as if to clap him on the shoulder." -- term and action both preserved. Second: 净土和诸真君早已谈妥，所以剑阁的真君们都没有出手 (the True Monarchs of Sword Pavilion did not lift a finger). Translation: "the True Monarchs of the Sword Pavilion did not lift a finger." -- not a Demon-Purging True Person action; it's a group negation. No predicate loss detected.
- The "predicate loss near Demon-Purging True Person" flag from the brief was likely referring to these passages. Both are correctly rendered.

**Reasoning:** Rule is load-bearing. Observer detect_glossary_predicate_loss addresses the failure mode. The ch425 instances are not violations.

**Verdict: keep**

---

### BASE-GLOS-07: A recurring unlisted term: one rendering on first use, kept all chapter, reported among the new terms.

**Evidence:**
- Phase14 TERMS block: chapters produce TERMS arrays with new entries, consistent with this rule.
- WW corpus: consistent in-chapter rendering is universal practice. No evidence against.

**Reasoning:** Rule matches corpus practice and is necessary for within-chapter consistency before glossary auto-merge.

**Verdict: keep**

---

### GLOS-01: Named categories (character/place/technique/item) treated as proper-noun-like; stored with translator-emitted casing.

**Evidence:**
- WW corpus: technique names are Title-Cased throughout. "Foundation Establishment" (realm name) always Title-Cased in ISSTH, AWE, MW. Sect names ("Seven Mysteries Sect", "Spirit Stream Sect") always Title-Cased. This is consistent with named-category Title-Case policy.
- Generic cultivation terms are lowercase in corpus: "battle qi" (11x lowercase in CD ch1), "true qi" (5x lowercase in SOTR ch1). This is consistent with GLOS-04's GENERIC_LOWERCASE list.

**Reasoning:** Rule matches corpus practice. Named techniques/sects/places Title-Cased in all WW source material.

**Verdict: keep**

---

### GLOS-02: Auto-repair of all-lowercase named-category terms on insert.

**Evidence:**
- Unlocked entry "催眠" -> "Hypnosis" (technique, unlocked). This is Title-Cased by GLOS-02. But 催眠 = "hypnosis/hypnotize" -- a common English word. Whether this should be Title-Cased depends on whether it is used as a named technique or a generic description. No live test of whether the model treats "Hypnosis" as a named technique in prose vs. just the verb.
- No false positives from GLOS-02 observed in phase14 output.

**Reasoning:** Rule is needed to catch translators who emit all-lowercase technique names (common with lazy models). The Hypnosis case is borderline but acceptable.

**Verdict: keep**

---

### GLOS-03: Idiom category always lowercase; other category defers to emitted casing.

**Evidence:**
- Phase14 glossary idioms (category=idiom, count=234): sample includes "浅水出真龙" -> "a true dragon emerges from shallow waters" (lowercase, correct per GLOS-03), "借鸡生蛋" -> "borrowing a hen to hatch your own eggs" (lowercase, correct).
- WW corpus: idioms rendered at everyday sense with lowercase in prose.

**Reasoning:** Rule is consistent with corpus practice and user-set casing policy.

**Verdict: keep**

---

### GLOS-04: GENERIC_LOWERCASE list: qi, karma, spiritual power, spiritual energy, etc. never force-Title-Cased.

**Evidence:**
- Live DB violation found: "气 / 氣" -> "Qi" (category=other, locked=1). The stored term_en is "Qi" (capital Q). Per GLOS-04, "qi" is in the GENERIC_LOWERCASE list and should never be force-Title-Cased. The enforce_locked_term_casing fixup would see "Qi" as a casing-locked term and enforce "Qi" in prose, potentially Title-Casing every "qi" in context.
- WW corpus: "battle qi" lowercase throughout CD ch1 (11x); "true qi" lowercase throughout SOTR ch1 (5x). Never capitalized as generic noun in WW prose.
- GLOS-04 correctly exempts this from force-casing. However, the stored entry "Qi" (capital) is itself a data defect -- if the casing-enforcement fixup reads the stored term_en literally, it would still attempt to enforce capital Q at term boundaries.

**Reasoning:** Rule is correct and matches WW corpus. The live DB has a violating entry (气 -> Qi with capital Q) that should be corrected. See glossary-data fixes.

**Verdict: keep**

---

### GLOS-05: Freshly extracted generic rank/tier descriptors forced to lowercase on insert.

**Evidence:**
- Phase14 glossary: "中期" -> "mid stage" (other, locked=1, lowercase), "后期 / 後期" -> "late stage" (other, locked=1, lowercase), "初期" -> "early stage" (other, locked=1, lowercase). All correctly lowercase per GLOS-05.
- Compound form: "金丹中期" -> "mid-stage Golden Core" (place, locked=1). This has a hyphen per XIA-15 but the standalone entry "中期" -> "mid stage" has no hyphen.
- **Key conflict discovered:** XIA-15 mandates "Stage modifiers hyphenate: early-stage, mid-stage, late-stage, peak." The glossary entries use the unhyphenated form (mid stage, late stage, early stage). Phase14 output renders "mid-stage Foundation Establishment True Person" (ch424), "late-stage Foundation Establishment Great True Person" (ch427) -- correctly following XIA-15 overlay over the glossary. The scorer that flagged "mid stage" and "late stage" as missing in phase14 output was a FALSE POSITIVE: the model used the hyphenated form per XIA-15.

**Reasoning:** GLOS-05 is correct (rank descriptors lowercase). The XIA-15 hyphenation creates an apparent conflict with the unhyphenated glossary entries. The current output resolves this correctly (XIA-15 wins via precedence). The glossary entries' unhyphenated form is a data inconsistency but not a production defect because GLOS-06 exempts slash-alternative entries from force-casing and the overlay wins on form.

**Verdict: keep** (rule is correct; the glossary data inconsistency is worth fixing but is not a rule defect)

---

### GLOS-06: Slash-alternative entries exempt from force-casing via atomic-casing path.

**Evidence:**
- Phase14 glossary includes many slash entries: "后期 / 後期" -> "late stage", "不受奔 / 不南奔" -> "No Escape South", etc. These are not run through force-casing, which is correct.

**Reasoning:** Rule prevents double-casing artifacts in slash-alternative forms.

**Verdict: keep**

---

### GLOS-07: All-lowercase stored term_en carries no proper-noun casing to enforce; deliberate down-casing available through 'lowercase' note.

**Evidence:**
- Phase14 glossary: "mid stage", "late stage", "early stage" all stored lowercase. The casing enforcer does not apply to these.
- BASE-FMT-05 tension: "A lowercase glossary term still capitalizes in direct address ('Master,') and at a sentence head." This is a different layer (sentence-initial capitalization fixup) that fires independently of GLOS-07. The two are compatible: GLOS-07 governs force-casing of the stored entry; BASE-FMT-05 governs what happens to the term at the start of a sentence. If a sentence begins with "mid-stage", the sentence-initial capitalization fixup would uppercase the M. This is correct behavior.

**Reasoning:** Rule is load-bearing for preventing generic nouns from being force-capitalized as proper nouns.

**Verdict: keep**

---

### GLOS-08: Auto-merge admission gated by filter_glossary_candidates: system-interface span OR recurs >= 2x.

**Evidence:**
- No direct corpus evidence for or against this threshold. It is an engineering policy, not a prose rule.
- The 2x recurrence threshold is conservative (prevents one-offs from polluting the glossary). The system-interface span gate is needed because 【...】 terms are proper names by design.

**Reasoning:** Rule is sound engineering policy. No evidence it should change.

**Verdict: keep**

---

### GLOS-09: User edit = implicit lock; locked entries immutable by auto-detection.

**Evidence:**
- Phase14 DB: 2083 locked, 56 unlocked entries. The unlock/lock mechanism is working as designed.
- No evidence of locked entries being overwritten.

**Reasoning:** Non-destructive invariant required by user policy.

**Verdict: keep**

---

### GLOS-10: Named cultivation concepts Title-Cased; idiom category lowercase.

**Evidence:**
- Corpus fully supports Title-Case for named concepts: "Foundation Establishment", "Qi Condensation", "Golden Core", "Nascent Soul" all Title-Cased in WW corpus.
- Generic terms are lowercase in corpus: "battle qi", "true qi" never capitalized.
- Rule is user-set policy; conflicts get 'user-decision' per trial instructions.

**Verdict: keep** (user-set policy; corpus-consistent)

---

### GLOS-11: Explicit lowercase override via usage note.

**Evidence:**
- No violations observed. The escape hatch is used for entries like "元磁" -> "primordial magnetism" (lowercase, locked) and "元磁神光" -> "primordial magnetism divine light" (lowercase, locked). These are correctly kept lowercase.

**Reasoning:** Escape hatch is needed for the generic-noun / named-compound boundary cases.

**Verdict: keep**

---

### GLOS-TITLELEN: Long honorific title glossary entry length -- open question.

**Evidence (concrete numbers):**

Phase14 pronoun:full-title ratios:
- ch424: Demon-Purging True Person 30x, pronouns (he+him+his) 47x, ratio 1.6:1
- ch426: Demon-Purging True Person 30x, pronouns 72x, ratio 2.4:1
- ch429: Evil Quelling True Person 17x, he 44x, ratio 2.6:1

WW corpus reference:
- ISSTH ch1: Meng Hao 25x, pronouns 89x, ratio 3.6:1
- AWE ch2: Big Fatty Zhang 8x, he 49x, ratio 6.1:1

Very-long titles (6-9 words, e.g. "True Monarch of Adamant Form Who Proclaims the Dao") appear 0x in phase14 chapters, so frequency is not a current live problem for those specific entries.

Medium titles (4-5 words: "Demon-Purging True Person", "Evil Quelling True Person"): clearly below WW pronominalization level. Ch426 has 15 of 29 full-title uses within 300 characters of the prior use, including back-to-back in the same paragraph ("Demon-Purging True Person held him back... 'No need.' Demon-Purging True Person stepped forward", gap 103 chars).

**Verdict: user-decision**

**Concrete recommendation:** Do not shorten glossary entries. The fix is a worked-example addition to xianxia.md examples, specifically a paragraph where a 4-word Daoist title appears once in full (scene entrance) then uses "he" and "him" for 3-4 beats before the next full invocation. Target ratio: full title < 1 use per 3 sentences in action scenes. The current EX-04 example shows the pattern for narrative distance; a close-action example is missing.

---

## Test-Pin Trials

### TEST-01: Composed instruction contains all three source files verbatim, in base -> overlay -> examples order.

**Analysis:** Pins the layering algorithm at the file-inclusion level (verbatim file contents, not phrases). The test uses real file reads, so copy-edits to .md files do not break it -- it stays green as long as base, overlay, and examples are all present and ordered correctly. This is exactly the right level of abstraction.

**Load-bearing behavior:** Yes. If the composition order inverts (overlay before base) or a file silently drops out, the model's precedence ladder collapses.

**Verdict: keep**

---

### TEST-02: Different genres must produce different system instructions.

**Analysis:** Guards against a genre routing bug where all genres silently converge to the same instruction. Structural test at the output level.

**Verdict: keep**

---

### TEST-03: Each genre's overlay must add genre-specific content vs. generic.

**Analysis:** Ensures no overlay file is empty or a verbatim copy of generic. Load-bearing for per-genre routing correctness.

**Verdict: keep**

---

### TEST-04: NULL genre input resolves via DEFAULT_GENRE.

**Analysis:** Pins the fallback path for novels with no genre set. Load-bearing for first-run novels and NULL DB rows.

**Verdict: keep**

---

### TEST-05: Unknown genre key falls back gracefully without crashing.

**Analysis:** Defensive test for DB/registry drift. Load-bearing for EXE builds where old novel records may carry removed genre keys.

**Verdict: keep**

---

### TEST-06: Custom brief APPENDS after genre overlay, does not replace it.

**Analysis:** Pins the append-not-replace behavior, which is a deliberate architectural decision (brief governs voice/word-choice, not structural conventions). The test uses a literal brief string ("Make all dialogue sound sarcastic") and checks that the overlay still appears. This is structural, not wording-pinned.

**Dependency note:** TPL-04 (brief scope) and TPL-05 (precedence ladder rung 4 > 3) underpin this behavior. If another bundle removes or alters TPL-04/TPL-05, this test would need review.

**Verdict: keep**

---

### TEST-07: Empty or whitespace-only brief treated as None (no brief section emitted).

**Analysis:** Pins the normalization logic that prevents empty-string briefs from adding a marker section with no content. Specific behavior pinned by this test: "" and "   " and "\n\n\t" all produce the same output as None. This is a cache-correctness test (empty brief must not cause cache misses).

**Verdict: keep**

---

### TEST-08: LRU cache returns same string on repeat calls (identity, not just equality).

**Analysis:** Pins that the cache returns the *same object* (is identity), confirming the LRU hit rather than a recomputed equal string. This prevents silent cache misses that would cost the user a translation every time.

**Verdict: keep**

---

### TEST-09: PEMT section absent when free_draft is None/empty/whitespace.

**Analysis:** Pins graceful degrade when no free draft exists. Structural test, not wording-pinned (comments in test file explicitly state "assertions are structural, never pinned to prompt phrasing").

**Verdict: keep**

---

### TEST-10: Non-empty free_draft changes prompt and appears verbatim.

**Analysis:** Pins that the draft body actually makes it into the prompt. Load-bearing for PEMT correctness.

**Verdict: keep**

---

### TEST-11: PEMT reference block sits BEFORE Chinese source text.

**Analysis:** Pins the ordering assumption (reference before source). If ordering flipped, the LLM reads source first and uses reference as a post-hoc check rather than a pre-read fidelity anchor. Load-bearing for PEMT semantics.

**Verdict: keep**

---

### TEST-12: Adding free_draft does not change glossary block formatting.

**Analysis:** Isolation test: PEMT insertion must not disturb the glossary block. Load-bearing for glossary block stability.

**Verdict: keep**

---

### TEST-13: free_draft over FREE_DRAFT_REF_MAX_CHARS is truncated with a marker.

**Analysis:** Pins the truncation guard. Load-bearing for prompt-size safety.

**Verdict: keep**

---

### TEST-14: PROMPT_INCLUDE_FREE_DRAFT flag state recorded in snapshot separately from block-emit state.

**Analysis:** Pins that the config snapshot distinguishes "flag was off" from "flag was on but data was empty." Load-bearing for A/B analysis: you cannot run the PROMPT_INCLUDE_FREE_DRAFT A/B test without this distinction.

**Verdict: keep**

---

### TEST-15: prompt_template_version key present in snapshot.

**Analysis:** Pins that the snapshot always contains the template version for cache invalidation and A/B attribution. One-liner assertion inside a larger test (test_build_prompt_config_snapshot_well_formed_all_flags_true, line 131). Load-bearing for traceability: without this key, you cannot identify which chapters were translated with which prompt version.

**Verdict: keep**

---

### TEST-16: PROMPT_INCLUDE_STYLE_NOTE=false suppresses style note regardless of DB content.

**Analysis:** Pins flag-short-circuit behavior for style note. Load-bearing for A/B flag gate correctness.

**Verdict: keep**

---

### TEST-17: PROMPT_INCLUDE_STYLE_EDITS=false suppresses style edits regardless of DB content.

**Analysis:** Same as TEST-16 for style edits. Load-bearing for A/B flag gate.

**Verdict: keep**

---

### TEST-18: PREVIOUS_CONTEXT_ENABLED=False suppresses previous-tail block even with valid done chapter.

**Analysis:** Pins the kill-switch behavior for the previous-context block. Load-bearing: if this flag gate malfunctions, the A/B test on PREVIOUS_CONTEXT_ENABLED becomes unrunnable.

**Dependency note:** BASE-CONT-01/02 and CLAUDE.md both state PREVIOUS_CONTEXT_ENABLED defaults true and is not part of the A/B sequence (it does real continuity work). This test correctly pins the flag respects the off state; it does not mandate the default.

**Verdict: keep**

---

### TEST-19: A done chapter beyond PREVIOUS_CONTEXT_MAX_GAP (10) yields None.

**Analysis:** Pins the 10-chapter window. The window size (10) is product-tuned, not prompt-tuned. If the window were changed, this test should be updated to match. Currently pins exact constant behavior.

**Verdict: keep** (if constant changes, update the test constant too)

---

### TEST-20: Identical before/after style-edit pairs collapse to one entry (newest-first dedup).

**Analysis:** Pins deduplication behavior for the style-edits prompt block. Load-bearing for prompt-size management and preventing duplicate entries from inflating the block.

**Verdict: keep**

---

### TEST-21: All registered genres have their overlay file on disk.

**Analysis:** Registry-to-file parity test. Load-bearing for deploy safety: if a genre is added to genres.py without creating the overlay file, the test catches it before production.

**Verdict: keep**

---

### TEST-22: Every registered genre has its examples file on disk.

**Analysis:** Same parity check for examples files.

**Verdict: keep**

---

### Tests that would block prompt evolution

**None of the 22 tests pin specific prompt wording.** All test files explicitly note "assertions are structural (prompt equality, draft-text containment, ordering against test-owned inputs), never pinned to prompt phrasing -- the .md files are edited constantly and a copy edit must not break the suite." This design means all 22 tests would survive complete rewrites of base.md, xianxia.md, and examples/xianxia.md, as long as:
- All three files are still present and non-empty (TEST-01, TEST-21, TEST-22)
- The xianxia instruction differs from generic (TEST-02, TEST-03)
- Brief still appends after overlay (TEST-06)
- Empty brief still normalizes to None (TEST-07)
- Cache still returns identity (TEST-08)
- PEMT block absent/present/ordered correctly (TEST-09 through TEST-13)
- Flag gates still short-circuit correctly (TEST-14 through TEST-20)

The only evolution that would break tests:
- Removing a genre from the GENRES registry without removing its file entries (would not break tests, just change outcomes)
- Changing the brief from append to replace (breaks TEST-06)
- Removing the PEMT block ordering guarantee (breaks TEST-11)
- Removing the prompt_template_version key from snapshots (breaks TEST-15)
- Changing PREVIOUS_CONTEXT_MAX_GAP constant without updating TEST-19

---

## Verdict Summary

| Category | keep | amend | remove | user-decision | loosen |
|----------|------|-------|--------|---------------|--------|
| glossary (BASE-GLOS-01 to 07) | 7 | 0 | 0 | 0 | 0 |
| glossary-data (GLOS-01 to 11) | 11 | 0 | 0 | 0 | 0 |
| GLOS-TITLELEN | 0 | 0 | 0 | 1 | 0 |
| test-pin (TEST-01 to 22) | 22 | 0 | 0 | 0 | 0 |
| **Total** | **40** | **0** | **0** | **1** | **0** |

---

## False-Positive Scorecard (from morning's retranslate findings)

| Flagged miss | Verdict |
|---|---|
| 'mid stage' missing in ch424 | FALSE POSITIVE: rendered as "mid-stage" per XIA-15 hyphenation rule; glossary entry has no hyphen but overlay wins |
| 'Moon Star' missing in ch424 | FALSE POSITIVE: source has 日月星辰 (sun, moon, and stars); 月星 appears as part of this compound phrase; translated correctly as "the sun, the moon, and the stars"; 月星 is the compound's sub-element, not a standalone mention |
| 'cultivation technique' missing in ch425 | FALSE POSITIVE: source has 佛门功法 (Buddhist cultivation techniques); GLOS-02 prevents sub-entry decomposition; rendered as "Buddhist techniques" which is correct prose compression |
| Predicate losses near Demon-Purging True Person ch425 | FALSE POSITIVE: both 出手 occurrences are correctly rendered ("reached out", "did not lift a finger"); no predicate dropped |
| 'Faction' missing ch427 | FALSE POSITIVE: source has 家族的势力越大 where 势力 = abstract "power/strength" absorbed into "the greater and stronger the family grows"; the glossary entry 势力 -> Faction is itself a data defect (see below) but the rendering is not a glossary miss |
| 'late stage' missing ch427 | FALSE POSITIVE: rendered as "late-stage Foundation Establishment Great True Person" per XIA-15 hyphenation |
| 'Yun Family' lowercase in ch424 | CONFIRMED BUG: "Yun family" (lowercase f) in "Yun family's Old Ancestor" (42x across novel) vs. stored "Yun Family" (capital F); casing fixup does not catch the possessive form |
| 'slew' in ch427 | CONFIRMED VIOLATION: "slew them all alike, friend and foe, without distinction" violates BASE-DIC-07 hard ban on 'slew'; one occurrence across all phase14 chapters |

---

## Glossary-Data Fix List

Fixes ranked by impact. All are non-destructive (no row deletion, no clobber of locked entries). Each tagged live-DB/dev-DB/both.

### FIX-1 (HIGH): 'Yun family' possessive casing -- both DBs

**Entry:** 云家 -> "Yun Family" (character, locked=1)
**Problem:** In possessive construction "Yun family's Old Ancestor", 'family' loses its capital. This occurs 42x across novel_id=2 chapters vs. 2x correct "Yun Family". The fixup layer (enforce_locked_term_casing) matches "Yun Family" as a phrase but the possessive "Yun family's" has lowercase 'f' and is not matched.
**Fix:** The casing enforcement code needs to handle the possessive form. Alternatively, a companion entry "Yun family's Old Ancestor" -> "Yun Family's Old Ancestor" in the glossary as a locked alias. Non-destructive: only add the alias, leave the existing entry.
**Scope:** Both DBs. The pattern "Yun family's" is a recurring surface.

### FIX-2 (HIGH): 气/氣 -> 'Qi' should be lowercase -- both DBs

**Entry:** 气 / 氣 -> "Qi" (other, locked=1)
**Problem:** "qi" is in the GENERIC_LOWERCASE list (GLOS-04). The stored term_en "Qi" has a capital Q. The enforce_locked_term_casing fixup sees "Qi" as a casing-locked term and could enforce capital Q at every occurrence of "Qi" in prose, fighting against the generic-lowercase intent. WW corpus: "battle qi", "true qi" always lowercase. "Qi" should not be force-Title-Cased as a standalone generic noun.
**Fix:** Update the term_en to lowercase "qi" (non-destructive update, not a delete) AND add usage_note "lowercase" to engage the explicit escape hatch. Locked status stays 1.
**Scope:** Both DBs.

### FIX-3 (MEDIUM): 势力 -> 'Faction' is wrong in most contexts -- both DBs

**Entry:** 势力 -> "Faction" (place, locked=1)
**Problem:** 势力 is a generic abstract noun meaning "power / influence / faction / forces". The glossary assigns the institutional rendering "Faction" (capital F) which fits only when 势力 refers to an organized group. In ch427, 家族的势力越大 = "the greater the family's power/influence grows" -- "Faction" does not fit, so the model correctly absorbed it into a recomposition rather than using the glossary term. In ch425, 散修势力 = "rogue cultivator faction" -- rendered as "rogue cultivator Faction" (capital F) which is forced and wrong for a loose collective.
**Fix:** Change term_en to "faction / power" (lowercase) and add usage_note "lowercase; use only for organized groups, not abstract power". This preserves the entry (non-destructive) and removes the forced capitalization.
**Scope:** Both DBs.

### FIX-4 (MEDIUM): Stage modifier form inconsistency -- both DBs

**Entries:** 中期 -> "mid stage", 后期/後期 -> "late stage", 初期 -> "early stage" (all other, locked=1)
**Problem:** XIA-15 mandates "stage modifiers hyphenate: early-stage, mid-stage, late-stage, peak." The glossary stores the unhyphenated form. The model correctly uses the hyphenated form (per overlay precedence over glossary) but the glossary entries trigger false positives in any scoring tool that does exact-match against term_en.
**Fix:** Update term_en to "mid-stage", "late-stage", "early-stage" to match the overlay-mandated hyphenated form. Non-destructive update. Alternatively, add usage_note "overlay mandates hyphenation; both forms acceptable".
**Scope:** Both DBs.

### FIX-5 (LOW): Unlocked technique entries with inconsistent casing

**Entries:** Several unlocked technique entries that auto-detected but were never reviewed:
- "催眠" -> "Hypnosis" (technique, unlocked=0): marginal; if hypnosis is used as a named technique this is correct; if generic, should be lowercase. Review-and-lock needed.
- "俱藏" -> "All-Concealment" (technique, unlocked=0) and "敕山移岳正法" -> "Mountain-Commanding Peak-Moving Righteous Dharma" (technique, unlocked=0): these look like named techniques that should be locked.
- "真言" -> "Words of Power" (technique, unlocked=0): generic-sounding name but may be a specific named technique.
**Fix:** Human review and lock of all 56 unlocked entries. Priority: technique and character categories. No data changes until reviewed.
**Scope:** Both DBs.

### FIX-6 (LOW): 'slew' hard-ban violation -- prompt side fix, not glossary

**Not a glossary-data fix.** The 'slew' in ch427 ("slew them all alike") violates BASE-DIC-07. The passage is the climactic Heaven-Slaying Sword moment; "slew" was likely chosen for dramatic weight. Fix: retranslate ch427 or do a find-replace on the specific sentence. Not a glossary issue.
**Scope:** Ch427 only, single occurrence.

---

## Five Highest-Impact Findings

1. **All six "missing render" flags from morning's retranslate were false positives** (mid stage, Moon Star, cultivation technique, predicate losses, late stage) with one exception (Yun family lowercase). The scoring tool is doing exact-match against unhyphenated glossary entries while the model correctly outputs the overlay-mandated hyphenated forms. This systematically over-counts glossary misses.

2. **GLOS-TITLELEN: pronominalization deficit.** Phase14 chapters run at 1.6-2.6:1 pronoun:full-title ratio; WW reference is 3.6-6.1:1. In ch424 and ch426, 50% of "Demon-Purging True Person" uses appear within 300 chars of the previous use. The rule text is correct; the model is not obeying it. A worked-example addition to xianxia examples showing close-action pronominalization is the recommended fix.

3. **气 -> Qi is a GLOS-04 violation in the live DB.** 'qi' is on the GENERIC_LOWERCASE list but stored as "Qi" (capital Q) with locked=1. This could cause the casing enforcer to Title-Case generic "qi" across prose, directly contradicting the corpus norm (lowercase qi throughout WW) and the user casing policy.

4. **势力 -> Faction is a wrong mapping** that causes forced capitalization of an abstract noun as an institutional name. In ch427 the model correctly avoided it via recomposition; in ch425 it produced "rogue cultivator Faction" (capital F) which is grammatically odd for a loose collective.

5. **All 22 test-pin tests are correctly scoped** (structural not wording-pinned) and none would block legitimate prompt evolution. The one evolution risk is TEST-11 (PEMT ordering) and TEST-06 (brief append order), which pin deliberate architectural decisions that should not change without deliberate design review.
