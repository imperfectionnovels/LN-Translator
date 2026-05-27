"""Source-language auto-detect for imported novels.

Heuristic only — distinguishes Chinese, Japanese, and Korean from each
other using Unicode-block tallies. Reliable for CJK-pure novel text;
falls back to 'zh' when there's no signal at all.

Why this is enough: each language uses a distinct script with no overlap
in the CJK Unified Ideographs block. Hiragana / Katakana characters are
Japanese-only; Hangul syllables are Korean-only. A text containing any
non-trivial count of either of those is the matching language. CJK
ideographs without kana / hangul are Chinese by elimination.

Why this is NOT a perfect classifier: romanized Chinese (pinyin) reads as
'zh' (no CJK) and surfaces 'zh' from the fallback — but that's the right
column for a translator that's still going to handle it as Chinese. Mixed
Japanese-with-romaji defaults to 'ja' as long as ANY kana is present.
Edge cases (Vietnamese with chữ Nôm, mixed-language anthologies) need
the manual override on the novel overview page.
"""

from __future__ import annotations

# Unicode block ranges, inclusive.
# https://www.unicode.org/charts/
_HIRAGANA = (0x3040, 0x309F)
_KATAKANA = (0x30A0, 0x30FF)
_HANGUL_SYLLABLES = (0xAC00, 0xD7AF)
_CJK_IDEOGRAPHS = (0x4E00, 0x9FFF)

# Detection thresholds. The heuristic is ratio-based, not raw-count: a
# Chinese text containing a quoted Japanese loanword like 「ロボット」
# (4 katakana) must still classify as zh because kana is a tiny fraction
# of the overall CJK signal. A real Japanese chapter has hundreds of kana
# characters and dominates by share, not just by presence.
#
# Tuned empirically against representative samples; the test suite in
# backend/tests/test_lang_detect.py pins the policy.
_MIN_CJK_SIGNAL = 10        # below this, no confident detection — fall back
_KANA_SHARE_FOR_JA = 0.10   # 10% of CJK code points must be kana to call ja
_HANGUL_SHARE_FOR_KO = 0.05  # 5% suffices because hangul never bleeds
                             # into Chinese / Japanese text the way kana
                             # bleeds in via loanwords


def _in_block(cp: int, block: tuple[int, int]) -> bool:
    return block[0] <= cp <= block[1]


def detect_source_language(text: str) -> str:
    """Return the most likely source language for `text`.

    Returns one of: 'zh', 'ja', 'ko'. Falls back to 'zh' when there's
    no confident CJK signal (preserves the schema's pre-2026-05-25
    default).
    """
    if not text:
        return "zh"

    kana_count = 0
    hangul_count = 0
    ideograph_count = 0

    for ch in text:
        cp = ord(ch)
        if _in_block(cp, _HIRAGANA) or _in_block(cp, _KATAKANA):
            kana_count += 1
        elif _in_block(cp, _HANGUL_SYLLABLES):
            hangul_count += 1
        elif _in_block(cp, _CJK_IDEOGRAPHS):
            ideograph_count += 1

    total_cjk = kana_count + hangul_count + ideograph_count
    if total_cjk < _MIN_CJK_SIGNAL:
        # No confident detection. Pure ASCII / romanized / very short
        # snippet — return the safe default rather than guess.
        return "zh"

    kana_share = kana_count / total_cjk
    hangul_share = hangul_count / total_cjk

    if kana_share >= _KANA_SHARE_FOR_JA:
        return "ja"
    if hangul_share >= _HANGUL_SHARE_FOR_KO:
        return "ko"
    return "zh"
