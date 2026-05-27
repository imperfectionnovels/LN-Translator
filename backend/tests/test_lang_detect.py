"""Tests for the source-language auto-detect heuristic.

Exercises the Unicode-block tally against representative fixtures for
zh / ja / ko / mixed / empty / pinyin. The threshold (_PRESENCE_THRESHOLD)
is treated as an implementation detail; tests use realistic text lengths
so they survive small threshold tweaks.
"""

from __future__ import annotations

import pytest

from backend.services.lang_detect import detect_source_language


def test_pure_chinese_returns_zh() -> None:
    text = "第一章 重生\n\n他睁开眼睛，发现自己回到了十年前。"
    assert detect_source_language(text) == "zh"


def test_pure_japanese_returns_ja() -> None:
    text = "第一章 はじまり\n\n彼は目を覚ますと、十年前に戻っていることに気づいた。"
    assert detect_source_language(text) == "ja"


def test_japanese_katakana_only_returns_ja() -> None:
    text = "メインキャラクター。ストーリーはここから始まる。プロローグ。"
    assert detect_source_language(text) == "ja"


def test_korean_hangul_returns_ko() -> None:
    text = "제1장 시작\n\n그는 눈을 떴고, 십 년 전으로 돌아왔다는 것을 깨달았다."
    assert detect_source_language(text) == "ko"


def test_empty_string_falls_back_to_zh() -> None:
    """No signal → preserve the schema default. The translator pipeline
    treats unknown language as Chinese-by-default since the app was
    originally Chinese-only."""
    assert detect_source_language("") == "zh"


def test_pure_ascii_falls_back_to_zh() -> None:
    """English / pinyin text has no CJK signal. Returns the schema
    default rather than guessing 'en' (not a supported value)."""
    text = "He woke up and realized he was back ten years in the past."
    assert detect_source_language(text) == "zh"


def test_chinese_with_one_stray_katakana_still_returns_zh() -> None:
    """A single foreign-language word in a Chinese text must NOT flip
    the result. The threshold guards against this."""
    text = (
        "他从口袋里掏出一张写着「ロボット」的卡片，然后把它递给了对方。"
        "这是一本古老的小说，里面有许多关于机械的描写。"
        "故事的主人公是一位年轻的工程师，他正在研究一种新型的机器。"
    )
    assert detect_source_language(text) == "zh"


def test_mixed_japanese_with_chinese_returns_ja() -> None:
    """Japanese novels often contain unannotated kanji (CJK ideographs)
    alongside kana. As long as kana is meaningfully present, the result
    is Japanese."""
    text = (
        "彼は本を読みながら、お茶を飲んでいた。窓の外では、雨が降っていた。"
        "「これは何ですか？」と彼は尋ねた。"
    )
    assert detect_source_language(text) == "ja"


def test_korean_with_some_chinese_characters_returns_ko() -> None:
    """Korean texts occasionally include hanja (CJK ideographs) for
    proper nouns. The presence of hangul wins."""
    text = "한국어로 된 소설입니다. 主人公이 깨어났을 때, 그는 모든 것을 잊었다."
    assert detect_source_language(text) == "ko"


@pytest.mark.parametrize("lang,sample", [
    ("zh", "我", ),
    ("zh", "ABC123 hello"),
    ("zh", ""),
])
def test_very_short_inputs_default_to_zh(lang: str, sample: str) -> None:
    """Below-threshold inputs (one CJK char, ASCII only, empty) all fall
    back to 'zh' — the safe default for a Chinese-first pipeline."""
    assert detect_source_language(sample) == lang
