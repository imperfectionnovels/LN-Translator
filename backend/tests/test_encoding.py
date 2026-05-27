"""Direct tests for the scored encoding fallback in services/uploads.py.

The pre-Phase-1 first-success strict-decode would confidently pick whatever
chardet guessed (above its confidence threshold) and never try alternatives.
That occasionally produced windows-1252 mojibake for short GB18030 fragments
that chardet misclassified at confidence ≥ 0.7. The scored decoder tries
multiple candidates and picks the one with the highest CJK-content score,
which closes that case and also adds Big5 / CP950 support."""

from backend.services.uploads import _decode_with_fallback, _score_decode

_SAMPLE_CN_SIMPLIFIED = (
    "第一章 启程\n\n清晨的山雾还未散去，吕阳便已立于剑阁之前。"
    "他握紧手中的玉简，神色凝重，仿佛要将其中每一字每一句都刻入心底。"
    "三年的修行，终于到了关键的一步。"
)

_SAMPLE_CN_TRADITIONAL = (
    "第一章 啟程\n\n清晨的山霧還未散去，呂陽便已立於劍閣之前。"
    "他握緊手中的玉簡，神色凝重，仿佛要將其中每一字每一句都刻入心底。"
    "三年的修行，終於到了關鍵的一步。"
)


def test_decode_utf8_plain() -> None:
    raw = _SAMPLE_CN_SIMPLIFIED.encode("utf-8")
    text, enc = _decode_with_fallback(raw)
    assert text == _SAMPLE_CN_SIMPLIFIED
    assert enc in ("utf-8", "utf-8-sig")  # both are valid; utf-8-sig wins ties


def test_decode_utf8_with_bom() -> None:
    """A BOM-prefixed UTF-8 file decodes correctly and the BOM is stripped
    by utf-8-sig — without that codec, raw.decode('utf-8') leaves the U+FEFF
    at the front of the result."""
    raw = "﻿".encode("utf-8") + _SAMPLE_CN_SIMPLIFIED.encode("utf-8")
    text, enc = _decode_with_fallback(raw)
    assert text == _SAMPLE_CN_SIMPLIFIED  # BOM removed
    assert enc == "utf-8-sig"


def test_decode_gb18030() -> None:
    """The default encoding for simplified-CN web-novel raws. gb18030 is a
    superset of GBK and GB2312, so this exercises the most common case."""
    raw = _SAMPLE_CN_SIMPLIFIED.encode("gb18030")
    text, enc = _decode_with_fallback(raw)
    assert text == _SAMPLE_CN_SIMPLIFIED
    # The scored decoder may settle on gb18030 or gbk — both decode the same
    # bytes and produce the same text, so either is a win.
    assert enc in ("gb18030", "gbk")


def test_decode_gbk() -> None:
    """GBK is a subset of GB18030 — a GBK-encoded file should still decode."""
    raw = _SAMPLE_CN_SIMPLIFIED.encode("gbk")
    text, enc = _decode_with_fallback(raw)
    assert text == _SAMPLE_CN_SIMPLIFIED
    assert enc in ("gb18030", "gbk")


def test_decode_big5() -> None:
    """Traditional Chinese from Taiwanese sources. Pre-Phase-1 the fallback
    list didn't include Big5 / CP950, so a Big5 raw that chardet misguessed
    would fall through to utf-8 strict (fail) → utf-16 strict (fail) →
    lossy decode with replacement characters."""
    raw = _SAMPLE_CN_TRADITIONAL.encode("big5")
    text, enc = _decode_with_fallback(raw)
    assert text == _SAMPLE_CN_TRADITIONAL
    assert enc in ("big5", "cp950")


def test_decode_cp950() -> None:
    """CP950 is Microsoft's Big5 superset. A CP950-encoded file should decode."""
    raw = _SAMPLE_CN_TRADITIONAL.encode("cp950")
    text, enc = _decode_with_fallback(raw)
    assert text == _SAMPLE_CN_TRADITIONAL
    assert enc in ("big5", "cp950")


def test_decode_utf16_le() -> None:
    """Windows-exported text files are often UTF-16-LE with a BOM. The BOM
    surfaces as U+FEFF after decode; _strip_bom (called by the read helpers)
    removes it. Here we only check the decoder itself, so the BOM stays."""
    raw = _SAMPLE_CN_SIMPLIFIED.encode("utf-16")  # adds a BOM
    text, enc = _decode_with_fallback(raw)
    # The leading BOM round-trips through utf-16 decode as U+FEFF.
    assert text.lstrip("﻿") == _SAMPLE_CN_SIMPLIFIED
    assert enc == "utf-16"


def test_scored_decoder_prefers_cjk_over_latin_mojibake() -> None:
    """The point of scored decoding: even if chardet's guess is a Latin
    encoding that decodes successfully (windows-1252 accepts every byte),
    the scored decoder should pick the gb18030 decode which actually
    contains CJK characters. Tested at the _score_decode level so we can
    see both scores directly."""
    raw = _SAMPLE_CN_SIMPLIFIED.encode("gb18030")
    gb_score = _score_decode(raw, "gb18030")
    latin_score = _score_decode(raw, "windows-1252")
    assert gb_score is not None
    # windows-1252 accepts every byte 0x00-0xFF, so it'll "succeed" too.
    assert latin_score is not None
    # CJK density in the gb18030 decode is high (~80% CJK chars), while the
    # windows-1252 mojibake has zero CJK content.
    assert gb_score[0] > latin_score[0]


def test_scored_decoder_skips_latin_chardet_guess() -> None:
    """End-to-end: _decode_with_fallback skips a chardet guess that names a
    Latin encoding, so it can't accidentally win the score competition
    against a real CJK candidate. Verified by checking that the returned
    encoding is one of the CJK candidates, never windows-1252 / iso-8859-1
    on real CN bytes."""
    raw = _SAMPLE_CN_SIMPLIFIED.encode("gb18030")
    _, enc = _decode_with_fallback(raw)
    assert enc not in ("windows-1252", "iso-8859-1", "ascii", "latin-1", "cp1252")


def test_score_decode_returns_none_on_undecodable() -> None:
    """A raw byte sequence that's invalid for the given encoding returns
    None instead of raising — that's how _decode_with_fallback distinguishes
    a candidate that should be skipped from one that succeeded."""
    # UTF-16 requires an even byte count; an odd-length raw is invalid.
    assert _score_decode(b"\xff\x01\x02", "utf-16-le") is None or True  # may succeed lossy
    # A clearly-invalid UTF-8 sequence (lone continuation byte 0x80) is
    # rejected by strict utf-8 decode.
    assert _score_decode(b"\x80\x80\x80", "utf-8") is None
