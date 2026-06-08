"""Unit tests for scripts/check_em_dashes.py (the CI em/en-dash gate).

This script literally fails the build when a stray em/en-dash lands in the
model-facing prompt text, so its detection logic is worth pinning directly.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

import check_em_dashes  # noqa: E402  static import -> credits scripts/check_em_dashes.py

EM = "—"  # em-dash
EN = "–"  # en-dash
MIDDLE_DOT = "·"  # the allowed replacement glyph


def test_module_constants_match_target_glyphs():
    # Pin the glyphs the gate is actually built around.
    assert check_em_dashes.EM == EM
    assert check_em_dashes.EN == EN
    assert ".md" in check_em_dashes.EXTS
    assert "node_modules" in check_em_dashes.SKIP_DIRS


def test_scan_flags_em_and_en_dash(tmp_path):
    f = tmp_path / "dirty.md"
    f.write_text(f"clean line\nbad {EM} dash here\nanother {EN} one\n", encoding="utf-8")
    hits = check_em_dashes._scan(f)
    # One em-dash hit and one en-dash hit, nothing on the clean line.
    assert len(hits) == 2
    chars = sorted(ch for _p, _ln, _col, ch, _line in hits)
    assert chars == sorted([EM, EN])
    line_nums = sorted(ln for _p, ln, _col, _ch, _line in hits)
    assert line_nums == [2, 3]


def test_scan_clean_file_has_no_hits(tmp_path):
    f = tmp_path / "clean.md"
    f.write_text(f"comma, colon: period. parens (ok) middle{MIDDLE_DOT}dot\nhyphen-ok\n", encoding="utf-8")
    hits = check_em_dashes._scan(f)
    # Middle-dot, hyphen, and ASCII punctuation are all allowed.
    assert hits == []
    assert len(hits) == 0
    assert all(ch not in (EM, EN) for _p, _l, _c, ch, _t in hits)


def test_scan_noqa_line_is_exempt(tmp_path):
    f = tmp_path / "exempt.md"
    f.write_text(
        f'"You X{EM}"  noqa: em-dash\nbad {EM} dash with no marker\n',
        encoding="utf-8",
    )
    hits = check_em_dashes._scan(f)
    # The noqa line is skipped; only the unmarked line trips.
    assert len(hits) == 1
    assert hits[0][1] == 2  # line number of the surviving hit
    assert hits[0][3] == EM


def test_cjk_neighbor_exemption(tmp_path):
    # A dash sitting next to a CJK char is treated as Chinese text and skipped.
    cjk_line = f"门{EM}道"  # men - dao around an em-dash
    assert check_em_dashes._is_cjk_neighbor(cjk_line, cjk_line.index(EM)) is True
    # Far from any CJK char, the helper returns False.
    ascii_line = f"plain ascii {EM} text"
    assert check_em_dashes._is_cjk_neighbor(ascii_line, ascii_line.index(EM)) is False

    f = tmp_path / "cjk.md"
    f.write_text(cjk_line + "\n", encoding="utf-8")
    assert check_em_dashes._scan(f) == []


def test_gather_files_respects_ext_and_skip_dirs(tmp_path):
    (tmp_path / "keep.md").write_text("x", encoding="utf-8")
    (tmp_path / "keep.py").write_text("x", encoding="utf-8")
    (tmp_path / "drop.txt").write_text("x", encoding="utf-8")  # .txt not in EXTS
    skip = tmp_path / "node_modules"
    skip.mkdir()
    (skip / "buried.md").write_text("x", encoding="utf-8")

    gathered = {p.name for p in check_em_dashes._gather_files(tmp_path)}
    assert "keep.md" in gathered
    assert "keep.py" in gathered
    assert "drop.txt" not in gathered
    assert "buried.md" not in gathered  # under a SKIP_DIRS dir


def test_gather_files_single_file_returns_itself(tmp_path):
    f = tmp_path / "solo.md"
    f.write_text("x", encoding="utf-8")
    out = check_em_dashes._gather_files(f)
    assert out == [f]
    assert len(out) == 1
    # A path that does not exist yields nothing.
    assert check_em_dashes._gather_files(tmp_path / "missing") == []


def test_main_exits_zero_on_clean_scope(tmp_path, monkeypatch, capsys):
    clean = tmp_path / "ok.md"
    clean.write_text(f"all good{MIDDLE_DOT}here\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check_em_dashes.py", str(clean)])
    rc = check_em_dashes.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "EM-DASH CHECK PASS" in out
    assert "em-dash," not in out  # no violation summary line


def test_main_exits_one_and_reports_on_violation(tmp_path, monkeypatch, capsys):
    dirty = tmp_path / "bad.md"
    dirty.write_text(f"line with {EM} and {EN}\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check_em_dashes.py", str(dirty)])
    rc = check_em_dashes.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "1 em-dash, 1 en-dash in user-visible code." in out
    assert "em-dash:" in out  # per-hit report kind label
