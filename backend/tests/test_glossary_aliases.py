"""Tests for slash-delimited glossary alias handling.

Glossary presets pack variant spellings into one row (`筑基 / 築基`). The old
literal-substring logic never matched such a row against a chapter containing
a bare variant, and never suppressed an unlocked duplicate of it."""

from backend.models import GlossaryEntry
from backend.services.glossary import (
    dedupe_against_locked,
    filter_glossary_for_chapter,
    missing_translator_terms,
    split_aliases,
)


def _entry(zh: str, en: str, locked: bool = False) -> GlossaryEntry:
    return GlossaryEntry(
        id=0,
        novel_id=1,
        term_zh=zh,
        term_en=en,
        category="place",
        notes=None,
        auto_detected=not locked,
        locked=locked,
    )


def _idiom_entry(zh: str, en: str, locked: bool = True) -> GlossaryEntry:
    return GlossaryEntry(
        id=0,
        novel_id=1,
        term_zh=zh,
        term_en=en,
        category="idiom",
        notes=None,
        auto_detected=not locked,
        locked=locked,
    )


def test_split_aliases_one_english_many_zh() -> None:
    assert split_aliases("筑基 / 築基", "Foundation Establishment") == [
        ("筑基", "Foundation Establishment"),
        ("築基", "Foundation Establishment"),
    ]


def test_split_aliases_positional() -> None:
    assert split_aliases("小周天 / 大周天", "Lesser / Greater Circulation") == [
        ("小周天", "Lesser"),
        ("大周天", "Greater Circulation"),
    ]


def test_split_aliases_no_slash() -> None:
    assert split_aliases("天剑诀", "Sky Sword Art") == [("天剑诀", "Sky Sword Art")]


def test_split_aliases_english_only_slash_kept_whole() -> None:
    # term_zh has no slash — the English is left intact, not split.
    assert split_aliases("重光", "Chongguang / Double Radiance") == [
        ("重光", "Chongguang / Double Radiance")
    ]


def test_alias_row_matches_chapter_with_bare_variant() -> None:
    locked = _entry("筑基 / 築基", "Foundation Establishment", locked=True)
    kept = filter_glossary_for_chapter([locked], "吕阳冲击筑基，三日后筑基稳固。")
    assert kept == [locked]


def test_locked_alias_suppresses_unlocked_duplicate() -> None:
    locked = _entry("筑基 / 築基", "Foundation Establishment", locked=True)
    dupe = _entry("筑基", "Foundation Establishment", locked=False)
    other = _entry("剑阁", "Sword Pavilion", locked=False)
    result = dedupe_against_locked([locked, dupe, other])
    assert locked in result and other in result
    assert dupe not in result


def test_missing_translator_terms_flags_dropped_locked_term() -> None:
    locked = _entry("筑基 / 築基", "Foundation Establishment", locked=True)
    chapter_zh = "吕阳冲击筑基，终于成功。"
    # English omits the locked rendering entirely.
    missing = missing_translator_terms(
        chapter_zh, "Lü Yang charged at the realm and finally succeeded.", [locked]
    )
    assert missing and missing[0][1] == "Foundation Establishment"


def test_missing_translator_terms_clean_when_present() -> None:
    locked = _entry("筑基 / 築基", "Foundation Establishment", locked=True)
    chapter_zh = "吕阳冲击筑基，终于成功。"
    ok = missing_translator_terms(
        chapter_zh,
        "Lü Yang charged at Foundation Establishment and finally succeeded.",
        [locked],
    )
    assert ok == []


def test_missing_translator_terms_ignores_unlocked_and_absent() -> None:
    locked_absent = _entry("元婴 / 元嬰", "Nascent Soul", locked=True)
    unlocked = _entry("筑基", "Foundation Establishment", locked=False)
    # Neither should be flagged: one is absent from the source, one is unlocked.
    assert missing_translator_terms("吕阳冲击筑基。", "He pushed onward.",
                                    [locked_absent, unlocked]) == []


def test_missing_translator_terms_skips_shorter_term_inside_longer_locked_alias() -> None:
    short = _entry("宝光", "treasure light", locked=True)
    long = _entry(
        "长曜宝光洞天",
        "Eternal Radiance Treasure-Light Grotto-Heaven",
        locked=True,
    )
    assert missing_translator_terms(
        "长曜宝光洞天乃是真君留下的洞天。",
        "Eternal Radiance Treasure-Light Grotto-Heaven was left behind by the True Monarch.",
        [short, long],
    ) == []


def test_missing_translator_terms_skips_known_false_chinese_substrings() -> None:
    dao_body = _entry("道身", "Dao Body", locked=True)
    demonic_path = _entry("魔道", "Demonic Path", locked=True)
    assert missing_translator_terms(
        "一道身影浮现，血魔道人随之走出。",
        "A figure appeared, and Blood Demon Daoist walked out after it.",
        [dao_body, demonic_path],
    ) == []


def test_missing_translator_terms_accepts_locked_idiom_standalone() -> None:
    idiom = _idiom_entry("找死", "you court death")
    assert missing_translator_terms(
        "你这是找死！",
        "You court death!",
        [idiom],
    ) == []


def test_missing_translator_terms_accepts_locked_idiom_inflection() -> None:
    idiom = _idiom_entry("找死", "you court death")
    assert missing_translator_terms(
        "这与找死有什么区别？",
        "How was that different from courting death?",
        [idiom],
    ) == []


def test_missing_translator_terms_rejects_disallowed_idiom_paraphrase() -> None:
    idiom = _idiom_entry("找死", "you court death")
    assert missing_translator_terms(
        "这与找死有什么区别？",
        "How was that different from asking to die?",
        [idiom],
    ) == [("找死", "you court death")]
