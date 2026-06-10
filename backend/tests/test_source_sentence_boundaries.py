"""Tests for enforce_source_sentence_boundaries.

The translator (and, more so, the refiner) sometimes promotes a Chinese comma
`，` to an English full stop, shattering one source sentence into several short
English ones. This source-aware backstop rejoins an over-split ONLY when the
aligned source paragraph was a single sentence, leaving multi-sentence source
paragraphs (percussive action beats), dialogue, and defensible 1->2 splits
alone.
"""

from backend.models import GlossaryEntry
from backend.services.text_fixups import enforce_source_sentence_boundaries


def _entry(zh: str, en: str) -> GlossaryEntry:
    return GlossaryEntry(
        id=0,
        novel_id=1,
        term_zh=zh,
        term_en=en,
        category="character",
        notes=None,
        auto_detected=False,
        locked=True,
    )


def test_orphan_fragment_rejoined_with_comma() -> None:
    src = "没必要，分身既然已经入了剑阁，就不能留下线索，还是切割得干净一点为好。"
    tgt = (
        "No need. Now that his avatar has entered the Sword Pavilion, it cannot "
        "leave a trace. Better to sever the connection cleanly."
    )
    out, n = enforce_source_sentence_boundaries(tgt, src)
    assert n == 2
    # fragment -> comma + lowercase; independent clause -> semicolon + lowercase
    assert out == (
        "No need, now that his avatar has entered the Sword Pavilion, it cannot "
        "leave a trace; better to sever the connection cleanly."
    )


def test_shattered_run_on_rejoined_with_semicolons() -> None:
    src = "他乃是皇室贵胄，当今天子是他兄长，血脉非凡，远眺万里更是不在话下。"
    tgt = (
        "He was a scion of the imperial bloodline. The Son of Heaven was his "
        "brother. His blood was extraordinary. Peering ten thousand li was "
        "trivial."
    )
    out, n = enforce_source_sentence_boundaries(tgt, src)
    assert n == 3
    assert out == (
        "He was a scion of the imperial bloodline; the Son of Heaven was his "
        "brother; his blood was extraordinary; peering ten thousand li was "
        "trivial."
    )


def test_multi_sentence_source_left_unchanged() -> None:
    # Source is genuinely three short sentences (percussive action beats).
    src = "他动了。剑光一闪。血溅当场。"
    tgt = "He moved. The sword flashed. Blood sprayed."
    out, n = enforce_source_sentence_boundaries(tgt, src)
    assert n == 0
    assert out == tgt


def test_defensible_two_way_split_left_unchanged() -> None:
    # One source sentence, output split into two, but the first clause is not a
    # short fragment -> a defensible split, left alone.
    src = "他缓缓回过头去，眼中闪过一丝复杂的神色。"
    tgt = "He slowly turned his head back. A complex look flickered in his eyes."
    out, n = enforce_source_sentence_boundaries(tgt, src)
    assert n == 0
    assert out == tgt


def test_question_and_exclamation_not_touched() -> None:
    # Output periods are the only rejoin candidates; ? and ! map to source
    # ？/！ and must be left intact. The lone period split has an independent
    # first clause ("He asked quietly"), so it is a defensible 2-way split and
    # is left alone.
    src = "他低声问，那有什么用？"
    tgt = "He asked quietly. What use is that?"
    out, n = enforce_source_sentence_boundaries(tgt, src)
    assert n == 0
    assert out == tgt


def test_single_break_true_fragment_joined() -> None:
    # A genuine verbless fragment as the first clause of a 2-way split IS joined.
    src = "真的吗，你确定要这么做？"
    tgt = "Really. You are sure you want to do this?"
    out, n = enforce_source_sentence_boundaries(tgt, src)
    assert n == 1
    assert out == "Really, you are sure you want to do this?"


def test_proper_noun_after_break_keeps_capital() -> None:
    src = "他离开了，吴泰安默默注视着他的背影，久久不语。"
    tgt = (
        "He left. Wu Taian watched his retreating figure in silence. He said "
        "nothing for a long time."
    )
    glossary = [_entry("吴泰安", "Wu Taian")]
    out, n = enforce_source_sentence_boundaries(tgt, src, glossary=glossary)
    assert n == 2
    # "He left" is a short but independent clause -> semicolon (not a splice);
    # "Wu" is a glossary proper noun -> capital preserved; the second "He" is
    # lowercased after its semicolon.
    assert "; Wu Taian watched" in out
    assert "; he said nothing" in out


def test_pronoun_I_keeps_capital() -> None:
    src = "可惜了，我终究还是来晚了一步，错失了良机。"
    tgt = "A pity. I came a step too late after all. I missed the chance."
    out, n = enforce_source_sentence_boundaries(tgt, src)
    assert n == 2
    # "A pity" is a true fragment -> comma; the I-clauses keep capital I.
    assert out == (
        "A pity, I came a step too late after all; I missed the chance."
    )


def test_glossary_article_not_kept_capital_after_join() -> None:
    # A glossary entry beginning with "The" must not leak the bare article into
    # the keep-capital set: a rejoin landing before an unrelated "The ..."
    # clause lowercases it (live ch427 artifact: "once more; The severed").
    src = "只这一剑，他重新稳定了气机，气运也在恢复，他终究是又多出了一段时间。"
    tgt = (
        "He steadied his aura once more. The severed destiny was regrowing. "
        "He had bought time."
    )
    glossary = [_entry("德充符", "The Symbol of Virtue Fulfilled")]
    out, n = enforce_source_sentence_boundaries(tgt, src, glossary=glossary)
    assert n == 2
    assert "; the severed destiny was regrowing" in out
    assert "; he had bought time" in out


def test_glossary_term_after_join_keeps_inner_capitals() -> None:
    # A rejoin landing exactly before a The-leading glossary term lowercases
    # only the article (the mid-sentence form enforce_locked_term_casing
    # enforces); the term's inner capitals survive untouched.
    src = "他打开卷轴，德充符就在其中，他慢慢读了起来。"
    tgt = (
        "He opened the scroll. The Symbol of Virtue Fulfilled lay within. "
        "He read it slowly."
    )
    glossary = [_entry("德充符", "The Symbol of Virtue Fulfilled")]
    out, n = enforce_source_sentence_boundaries(tgt, src, glossary=glossary)
    assert n == 2
    assert "; the Symbol of Virtue Fulfilled lay within" in out
    assert "; he read it slowly" in out


def test_full_bodied_multi_split_left_alone() -> None:
    # The prompt licenses unspooling one long source sentence into several
    # full-bodied English sentences. Clauses this long are a deliberate split,
    # not shatter: the gate leaves the paragraph untouched.
    src = "陈平安沿着溪边的小路慢慢走着，心里盘算着明天要做的事情，远处的山影在暮色里渐渐模糊了。"
    tgt = (
        "Chen Pingan walked slowly along the path beside the stream while the "
        "dusk settled. He turned over tomorrow's plans in his mind as he went "
        "on walking. The mountains in the distance slowly blurred into the "
        "failing evening light."
    )
    out, n = enforce_source_sentence_boundaries(tgt, src)
    assert n == 0
    assert out == tgt


def test_full_bodied_split_with_short_punch_left_alone() -> None:
    # A deliberate short subject-led punch after two long sentences is style,
    # not shatter: surgical mode only joins true verbless fragments.
    src = "老者打量了他半晌没有说话，少年沉稳的目光似乎终于让他满意，他笑了。"
    tgt = (
        "The old man studied him for a long moment without saying a word. "
        "Something in the young man's steady gaze seemed to satisfy him at "
        "last. He smiled."
    )
    out, n = enforce_source_sentence_boundaries(tgt, src)
    assert n == 0
    assert out == tgt


def test_full_bodied_split_leading_fragment_joined_surgically() -> None:
    # Long clauses put the paragraph in surgical mode, but a stranded verbless
    # opener is still a defect: that one boundary joins, the rest stay.
    src = "没必要，分身既然已经入了剑阁，就完全没有理由留下线索，还是干脆利落地切割干净为好。"
    tgt = (
        "No need. Now that his avatar has entered the Sword Pavilion, there is "
        "no reason at all to leave a trace behind. Better to sever the "
        "connection cleanly and walk away without any hesitation."
    )
    out, n = enforce_source_sentence_boundaries(tgt, src)
    assert n == 1
    assert out == (
        "No need, now that his avatar has entered the Sword Pavilion, there is "
        "no reason at all to leave a trace behind. Better to sever the "
        "connection cleanly and walk away without any hesitation."
    )


def test_surgical_path_idempotent() -> None:
    src = "没必要，分身既然已经入了剑阁，就完全没有理由留下线索，还是干脆利落地切割干净为好。"
    tgt = (
        "No need. Now that his avatar has entered the Sword Pavilion, there is "
        "no reason at all to leave a trace behind. Better to sever the "
        "connection cleanly and walk away without any hesitation."
    )
    once, _ = enforce_source_sentence_boundaries(tgt, src)
    twice, n2 = enforce_source_sentence_boundaries(once, src)
    assert n2 == 0
    assert twice == once


def test_shatter_just_below_threshold_still_fully_rejoins() -> None:
    # Threshold pin: clauses averaging just under the gate (here 23/3, ~7.7)
    # are still shatter and rejoin fully. The canonical orphan-fragment case
    # above sits at 22/3, so the gate constant must stay above that; if a
    # future tweak lowers it, this test fails loudly.
    src = "剑光一闪而过，院中弟子纷纷回头看去，竟没有一个人敢先开口说话。"
    tgt = (
        "The sword light flashed across the courtyard. Every disciple in the "
        "yard turned to look. Nobody dared to say a single word first."
    )
    out, n = enforce_source_sentence_boundaries(tgt, src)
    assert n == 2
    assert out == (
        "The sword light flashed across the courtyard; every disciple in the "
        "yard turned to look; nobody dared to say a single word first."
    )


def test_dialogue_paragraph_skipped() -> None:
    src = "“你来了，那就别走了，留下来陪我喝一杯。”"
    tgt = '"You came. Then do not leave. Stay and drink with me."'
    out, n = enforce_source_sentence_boundaries(tgt, src)
    assert n == 0
    assert out == tgt


def test_idempotent() -> None:
    src = "没必要，分身既然已经入了剑阁，就不能留下线索，还是切割得干净一点为好。"
    tgt = (
        "No need. Now that his avatar has entered the Sword Pavilion, it cannot "
        "leave a trace. Better to sever the connection cleanly."
    )
    once, n1 = enforce_source_sentence_boundaries(tgt, src)
    twice, n2 = enforce_source_sentence_boundaries(once, src)
    assert n2 == 0
    assert twice == once


def test_empty_inputs() -> None:
    assert enforce_source_sentence_boundaries("", "abc") == ("", 0)
    assert enforce_source_sentence_boundaries("abc", "") == ("abc", 0)


def test_unalignable_text_unchanged() -> None:
    # Paragraph counts diverge too far for a confident alignment (4 source
    # paragraphs vs 1 target) -> aligner returns None, nothing is touched.
    src = "甲一句话。\n\n乙一句话。\n\n丙一句话。\n\n丁一句话。"
    tgt = "No need. Now that it is done, it cannot be undone. Better stop."
    out, n = enforce_source_sentence_boundaries(tgt, src)
    assert n == 0
    assert out == tgt


def test_only_eligible_paragraph_changes_in_multi_para() -> None:
    src = (
        "他动了。剑光一闪。血溅当场。\n\n"
        "没必要，既然已经入局，就不能留痕，还是收手为好。"
    )
    tgt = (
        "He moved. The sword flashed. Blood sprayed.\n\n"
        "No need. Now that he is committed, he cannot leave a mark. Better to "
        "stop here."
    )
    out, n = enforce_source_sentence_boundaries(tgt, src)
    assert n == 2
    # action-beat paragraph untouched
    assert out.startswith("He moved. The sword flashed. Blood sprayed.\n\n")
    # second paragraph rejoined
    assert "No need, now that he is committed" in out
