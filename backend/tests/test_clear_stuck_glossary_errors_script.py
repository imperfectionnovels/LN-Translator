"""Unit tests for the one-shot maintenance script
`scripts/clear_stuck_glossary_errors.py`: it clears leftover
`chapters.glossary_merge_error` rows and resets chapters stuck at
`status='error'` specifically due to the zhcdict bundle bug.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import clear_stuck_glossary_errors as clear_stuck  # noqa: E402


def _seed(db_path) -> None:
    con = sqlite3.connect(str(db_path))
    con.executescript(
        """
        CREATE TABLE chapters (
            id INTEGER PRIMARY KEY,
            chapter_num INTEGER,
            title_zh TEXT,
            glossary_merge_error TEXT,
            status TEXT,
            error_msg TEXT,
            translate_queued INTEGER DEFAULT 0
        );
        """
    )
    con.executemany(
        "INSERT INTO chapters (id, chapter_num, title_zh, glossary_merge_error, "
        "status, error_msg, translate_queued) VALUES (?,?,?,?,?,?,?)",
        [
            # merge-error left over on an otherwise-done chapter -> should clear
            (1, 1, "ch1", "boom merge crash", "done", None, 0),
            # stuck error due to zhcdict bug -> should flip to pending
            (2, 2, "ch2", None, "error", "missing zhcdict.json bundle", 1),
            # unrelated error -> must NOT be touched
            (3, 3, "ch3", None, "error", "network timeout", 1),
            # clean done chapter -> untouched
            (4, 4, "ch4", None, "done", None, 0),
        ],
    )
    con.commit()
    con.close()


def _run_main(monkeypatch, db_path):
    monkeypatch.setattr(sys, "argv", ["clear_stuck_glossary_errors.py", str(db_path)])
    # pytest's captured stdout may lack reconfigure(); make it a no-op if so.
    monkeypatch.setattr(sys.stdout, "reconfigure", lambda *a, **k: None, raising=False)
    clear_stuck.main()


def _row(db_path, cid):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM chapters WHERE id=?", (cid,)).fetchone()
    con.close()
    return r


def test_clears_merge_error_and_resets_zhcdict(tmp_path, monkeypatch):
    db = tmp_path / "novels.db"
    _seed(db)
    _run_main(monkeypatch, db)

    # row 1: merge error cleared
    assert _row(db, 1)["glossary_merge_error"] is None
    # row 2: zhcdict-stuck reset to pending, error cleared, dequeued
    r2 = _row(db, 2)
    assert r2["status"] == "pending"
    assert r2["error_msg"] is None
    assert r2["translate_queued"] == 0


def test_leaves_unrelated_rows_untouched(tmp_path, monkeypatch):
    db = tmp_path / "novels.db"
    _seed(db)
    _run_main(monkeypatch, db)

    # row 3: unrelated error preserved
    r3 = _row(db, 3)
    assert r3["status"] == "error"
    assert r3["error_msg"] == "network timeout"
    # row 4: clean chapter untouched
    r4 = _row(db, 4)
    assert r4["status"] == "done"
    assert r4["glossary_merge_error"] is None


def test_idempotent_second_run_clears_nothing(tmp_path, monkeypatch):
    db = tmp_path / "novels.db"
    _seed(db)
    _run_main(monkeypatch, db)
    _run_main(monkeypatch, db)  # second run is a no-op

    assert _row(db, 1)["glossary_merge_error"] is None
    assert _row(db, 2)["status"] == "pending"
    assert _row(db, 3)["status"] == "error"


def test_missing_db_path_is_a_noop(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "does_not_exist.db"
    monkeypatch.setattr(sys, "argv", ["clear_stuck_glossary_errors.py", str(missing)])
    monkeypatch.setattr(sys.stdout, "reconfigure", lambda *a, **k: None, raising=False)
    clear_stuck.main()
    out = capsys.readouterr().out
    assert "nothing to do" in out
    assert not missing.exists()
