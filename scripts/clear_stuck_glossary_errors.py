"""One-shot: clear chapters.glossary_merge_error rows left over from the
zhconv/zhcdict bundle bug. Defaults to %APPDATA%\\LN-Translator\\novels.db;
pass a path to override.
"""

from __future__ import annotations

import os
import sqlite3
import sys


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        path = os.path.expandvars(r"%APPDATA%\LN-Translator\novels.db")
    print(f"db: {path}")
    if not os.path.exists(path):
        print("(missing — nothing to do)")
        return
    conn = sqlite3.connect(path)
    # 1. Clear glossary_merge_error column (set on a successful translate
    #    whose post-translate merge step crashed).
    rows = conn.execute(
        "SELECT id, chapter_num, title_zh, length(glossary_merge_error) "
        "FROM chapters WHERE glossary_merge_error IS NOT NULL"
    ).fetchall()
    print(f"glossary_merge_error rows: {len(rows)}")
    for r in rows:
        print(" ", r)
    n_merge = conn.execute(
        "UPDATE chapters SET glossary_merge_error = NULL "
        "WHERE glossary_merge_error IS NOT NULL"
    ).rowcount

    # 2. Reset chapters that failed translation specifically because of
    #    the zhcdict bundle bug. status='error' + error_msg mentions
    #    zhcdict.json → safe to flip back to 'pending' so the user can
    #    re-click Translate against the fixed bundle.
    rows = conn.execute(
        "SELECT id, chapter_num, title_zh, substr(error_msg, 1, 100) "
        "FROM chapters WHERE status='error' AND error_msg LIKE '%zhcdict%'"
    ).fetchall()
    print(f"chapters stuck at status='error' due to zhcdict: {len(rows)}")
    for r in rows:
        print(" ", r)
    n_err = conn.execute(
        "UPDATE chapters SET status='pending', error_msg=NULL, "
        "translate_queued=0 "
        "WHERE status='error' AND error_msg LIKE '%zhcdict%'"
    ).rowcount

    conn.commit()
    conn.close()
    print(f"cleared {n_merge} merge-error rows; reset {n_err} status='error' rows")


if __name__ == "__main__":
    main()
