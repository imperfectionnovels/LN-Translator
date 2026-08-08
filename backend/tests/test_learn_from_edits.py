"""Tests for the in-app learn-from-edits loop (stage + commit + ground-truth).

Phase 6: proposals derive from the segment ledger (chapter_segments rows with
machine_text != target_text on edited|confirmed rows), not from style_edits.
Seeds a chapter with edited segments that exercise a glossary casing change
(Spiritual Power -> spiritual power) and the exclamation-density brief signal,
then drives the two endpoints: stage derives the proposal without writing
(same shape the style_edits source produced), commit applies only confirmed
ids (brief -> custom_style_brief, casing -> glossary entry recased + locked,
save_ground_truth -> ground_truth_edits row), and a forged id is ignored.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.models import GlossaryEntry
from backend.services.learn_from_edits import _detect_casing_changes

DB_PATH = Path(os.environ["DB_PATH"])


def _entry(entry_id: int, term_zh: str, term_en: str) -> GlossaryEntry:
    return GlossaryEntry(
        id=entry_id,
        term_zh=term_zh,
        term_en=term_en,
        category="other",
        notes=None,
        auto_detected=False,
        locked=True,
    )


@pytest.fixture
def client(monkeypatch):
    if DB_PATH.exists():
        DB_PATH.unlink()

    async def _no_probe(default_provider):
        return None

    async def _no_drain():
        return None

    monkeypatch.setattr("backend.main._probe_backends", _no_probe)
    monkeypatch.setattr("backend.services.queue.drain_on_startup", _no_drain)

    from backend.main import app

    with TestClient(app) as c:
        yield c


def _seed() -> tuple[int, int]:
    """A novel + one done chapter, a locked glossary term, and edited segment
    rows carrying two before/after pairs: one recases the term, one strips
    three exclamation marks. Also seeds a machine row and a no-change edited
    row (machine_text == target_text) that must NOT count as captured edits.
    Returns (novel_id, entry_id)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO novels (title, source_type) VALUES ('N', 'paste')")
        novel_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "translated_text, status) VALUES (?, 1, '源', 'His spiritual power surged.', 'done')",
            (novel_id,),
        )
        chapter_id = conn.execute(
            "SELECT id FROM chapters WHERE novel_id=? AND chapter_num=1", (novel_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO glossary_entries (novel_id, term_zh, term_en, category, "
            "locked, auto_detected) VALUES (?, '灵力', 'Spiritual Power', 'other', 1, 0)",
            (novel_id,),
        )
        entry_id = conn.execute(
            "SELECT id FROM glossary_entries WHERE novel_id=?", (novel_id,)
        ).fetchone()[0]
        rows = [
            # (seg_index, source, machine/before, target/after, status, origin)
            # Human-edited rows carry origin='human' (what update_segment's
            # save stamps); the provenance gate (F2, bug hunt 2026-08-04)
            # requires it for a pair to count.
            (0, "灵力涌动。", "His Spiritual Power surged.",
             "His spiritual power surged.", "edited", "human"),
            (1, "停！快！走！", "Stop! Now! Go!", "Stop. Now. Go.",
             "confirmed", "human"),
            # Untouched machine row: never a pair.
            (2, "他走了。", "He left.", "He left.", "machine", "llm"),
            # Confirmed-as-is row (machine origin kept by confirm): no pair.
            (3, "夜深了。", "The night deepened.", "The night deepened.",
             "confirmed", "llm"),
        ]
        for seg_index, src, machine, target, status, origin in rows:
            conn.execute(
                "INSERT INTO chapter_segments (novel_id, chapter_id, seg_index, "
                "source_text, source_hash, target_text, machine_text, status, "
                "origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (novel_id, chapter_id, seg_index, src, f"h{seg_index:015d}",
                 target, machine, status, origin),
            )
        conn.commit()
    finally:
        conn.close()
    return novel_id, entry_id


def test_stage_derives_proposal_without_writing(client):
    nid, entry_id = _seed()
    r = client.post(f"/api/novels/{nid}/chapters/1/learn-edits")
    assert r.status_code == 200
    p = r.json()
    # Only the two rows whose machine_text differs from target_text count:
    # the untouched machine row and the confirmed-as-is row are excluded.
    assert p["captured_edits"] == 2
    # Exclamation signal present.
    assert any("xclamation" in b["text"] for b in p["brief"])
    # Casing proposal for the seeded term (same shape as the pre-Phase-6
    # style_edits-sourced proposal: id/entry_id/term_zh/term_en/proposed_en).
    gc = p["glossary_casing"]
    assert len(gc) == 1
    assert gc[0]["entry_id"] == entry_id
    assert gc[0]["id"] == f"gloss-{entry_id}"
    assert gc[0]["term_zh"] == "灵力"
    assert gc[0]["term_en"] == "Spiritual Power"
    assert gc[0]["proposed_en"] == "spiritual power"
    # Nothing written yet.
    conn = sqlite3.connect(DB_PATH)
    try:
        brief = conn.execute(
            "SELECT custom_style_brief FROM novels WHERE id=?", (nid,)
        ).fetchone()[0]
        term = conn.execute(
            "SELECT term_en FROM glossary_entries WHERE id=?", (entry_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert not brief
    assert term == "Spiritual Power"


def test_commit_applies_confirmed_subset(client):
    nid, entry_id = _seed()
    body = {
        "brief": ["brief-0"],
        "glossary_casing": [f"gloss-{entry_id}"],
        "save_ground_truth": True,
    }
    r = client.post(f"/api/novels/{nid}/chapters/1/learn-edits/commit", json=body)
    assert r.status_code == 200
    res = r.json()
    assert res["applied_brief"] == 1
    assert res["applied_glossary"] == 1
    assert res["ground_truth_saved"] is True

    conn = sqlite3.connect(DB_PATH)
    try:
        brief = conn.execute(
            "SELECT custom_style_brief FROM novels WHERE id=?", (nid,)
        ).fetchone()[0]
        term_en, notes, locked = conn.execute(
            "SELECT term_en, notes, locked FROM glossary_entries WHERE id=?", (entry_id,)
        ).fetchone()
        gt = conn.execute(
            "SELECT edited_text, source FROM ground_truth_edits WHERE novel_id=?", (nid,)
        ).fetchone()
    finally:
        conn.close()
    assert brief and "xclamation" in brief
    assert term_en == "spiritual power"      # recased
    assert "lowercase" in (notes or "").lower()  # down-caser backstop noted
    assert locked == 1                        # lock-on-edit
    assert gt[0] == "His spiritual power surged."
    assert gt[1] == "draft"


def test_commit_forged_id_is_ignored(client):
    nid, entry_id = _seed()
    body = {"brief": ["brief-99"], "glossary_casing": ["gloss-999999"]}
    r = client.post(f"/api/novels/{nid}/chapters/1/learn-edits/commit", json=body)
    assert r.status_code == 200
    res = r.json()
    assert res["applied_brief"] == 0
    assert res["applied_glossary"] == 0


def test_no_edited_segments_is_empty_proposal(client):
    """A chapter whose ledger has only machine rows stages the valid empty
    proposal (captured_edits 0), the shape the UI renders as "nothing to
    learn yet"."""
    nid, _ = _seed()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE chapter_segments SET status='machine', "
            "target_text=machine_text WHERE novel_id=?",
            (nid,),
        )
        conn.commit()
    finally:
        conn.close()
    r = client.post(f"/api/novels/{nid}/chapters/1/learn-edits")
    assert r.status_code == 200
    p = r.json()
    assert p["captured_edits"] == 0
    assert p["brief"] == []
    assert p["glossary_casing"] == []


def test_missing_chapter_404(client):
    nid, _ = _seed()
    assert client.post(f"/api/novels/{nid}/chapters/99/learn-edits").status_code == 404
    assert client.post(
        f"/api/novels/{nid}/chapters/99/learn-edits/commit", json={}
    ).status_code == 404


# _detect_casing_changes word-bounded matching (regression for the bare
# substring-membership bug: a short term_en like "Qi" used to match inside an
# unrelated longer word like "qigong", and the first-occurrence-anywhere index
# could land on that unrelated match rather than the recased one).


def test_casing_no_proposal_when_bounded_occurrence_matches_stored_case():
    """"qigong" contains "qi" as a substring but is not a bounded match, so the
    only bounded occurrence of "Qi" in the after text is "his Qi settled",
    which already carries the stored casing. No proposal."""
    glossary = [_entry(1, "气", "Qi")]
    pairs = [(
        "His qi was unstable.",
        "He circulated qigong through the meridians, then his Qi settled.",
    )]
    assert _detect_casing_changes(pairs, glossary) == []


def test_casing_proposes_recase_when_every_bounded_occurrence_agrees():
    glossary = [_entry(1, "气", "Qi")]
    pairs = [(
        "His qi flow was calm.",
        "He gathered qi and channeled qi again.",
    )]
    out = _detect_casing_changes(pairs, glossary)
    assert len(out) == 1
    assert out[0]["entry_id"] == 1
    assert out[0]["term_en"] == "Qi"
    assert out[0]["proposed_en"] == "qi"


def test_casing_no_proposal_when_bounded_occurrences_disagree():
    """One bounded occurrence recased to "qi", another to "QI": the signal is
    ambiguous, so the conservative behavior is no proposal at all."""
    glossary = [_entry(1, "气", "Qi")]
    pairs = [(
        "His qi was steady.",
        "His qi surged, then the QI dispersed.",
    )]
    assert _detect_casing_changes(pairs, glossary) == []


def test_casing_multi_word_term_recase_still_detected():
    glossary = [_entry(1, "金丹", "Golden Core")]
    pairs = [(
        "His Golden Core pulsed with energy.",
        "His golden core pulsed, and the golden core grew brighter.",
    )]
    out = _detect_casing_changes(pairs, glossary)
    assert len(out) == 1
    assert out[0]["term_en"] == "Golden Core"
    assert out[0]["proposed_en"] == "golden core"


def test_casing_term_ending_in_non_word_char_matches_bounded_without_crash():
    """term_en can end in a non-word character (e.g. a trailing parenthetical
    like "talent(s)"); \\b would not bound it correctly, hence the lookaround
    construction. Must not raise and must still detect the recase."""
    glossary = [_entry(1, "天赋", "Talent(s)")]
    pairs = [(
        "His Talent(s) awakened.",
        "His talent(s) awakened, unlocking new talent(s).",
    )]
    out = _detect_casing_changes(pairs, glossary)
    assert len(out) == 1
    assert out[0]["term_en"] == "Talent(s)"
    assert out[0]["proposed_en"] == "talent(s)"
