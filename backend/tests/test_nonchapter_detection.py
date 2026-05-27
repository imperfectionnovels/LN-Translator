"""Tests for author-note / non-chapter block detection.

A bulk import treats each file as one chapter; an author's update post that
slips in as a file takes a chapter number and shifts every chapter after it.
`is_non_chapter_block` keeps those out."""

from backend.services.parser import (
    has_author_note_markers,
    is_non_chapter_block,
)

# Real author-note titles taken from an actual imported novel.
_AUTHOR_NOTES = [
    "四月更新计划+求月票！\n\n时间过得还挺快，作者每天九千字。",
    "十更完毕！求月票！\n\n说十更，那就是十更。",
    "欠一章\n\n今天有点累过头了，洗个澡就准备睡了。",
    "不用等了\n\n今晚本来想赶更新的，没赶出来，明天我休息。",
    "请假一天，整理大纲剧情\n\n这两天卡文，先整理一下后续。",
    "停更一天。\n\n身体不舒服，明天恢复。",
    "今天更新放到晚上八点，五更！",
]

# Real story-chapter openings — must NOT be flagged.
_REAL_CHAPTERS = [
    "第298章 越级偷袭，所向披靡！\n\n“何方道友。”\n\n赶海李氏，甘棠道上方。",
    "吕阳进入剑阁之后，便开始静心修行，感悟天地灵气的流转变化。" * 30,
]


def test_author_notes_detected() -> None:
    for note in _AUTHOR_NOTES:
        assert is_non_chapter_block(note), f"missed author note: {note[:20]!r}"


def test_real_chapters_not_flagged() -> None:
    for chapter in _REAL_CHAPTERS:
        assert not is_non_chapter_block(chapter), f"flagged real chapter: {chapter[:20]!r}"


def test_block_with_chapter_heading_is_never_a_non_chapter() -> None:
    # A 第N章 heading on the first line means a real chapter even if the body
    # happens to mention an update word.
    text = "第12章 更新的剑\n\n他求月票的剑法大成。"
    assert not is_non_chapter_block(text)


def test_oversized_block_is_not_flagged() -> None:
    # The detector only fires on short blocks — a long body is a real chapter.
    long_text = "求月票！" + ("吕阳挥剑斩出，剑气纵横。" * 400)
    assert not is_non_chapter_block(long_text)


def test_empty_input() -> None:
    assert not is_non_chapter_block("")
    assert not is_non_chapter_block("   ")


def test_has_author_note_markers_is_heading_agnostic() -> None:
    # Used to spot an author note hiding behind a numberless 番外 heading.
    assert has_author_note_markers("番外+活动预告！\n\n全订番外明天发，求月票。")
    assert not has_author_note_markers("番外：吕阳的童年\n\n吕阳幼时家境贫寒。")
