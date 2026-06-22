"""Quality scorecard harness (backend.scripts.quality_metrics + quality_report).

Pins the pieces the meta-lever depends on:
  * the ported rule-category scorers fire on known-bad text and stay pure;
  * the orchestrator harvests chapter_observations and groups by
    prompt_config_snapshot (the A/B-over-back-catalog grouping);
  * the aggregate math (sums + per-chapter rates) and the A/B diff render.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from backend.config import DB_PATH
from backend.db import SCHEMA, open_conn
from backend.models import GlossaryEntry
from backend.scripts import quality_metrics as qm
from backend.scripts import quality_report as qr


@pytest.fixture(autouse=True)
def _reset_db():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    yield


def _gloss(zh, en, category="item", locked=True):
    return GlossaryEntry(
        id=1, novel_id=1, term_zh=zh, term_en=en, category=category,
        notes=None, usage_note=None, auto_detected=not locked, locked=locked,
    )


# ---- pure scorers -------------------------------------------------------


def test_rule_categories_fire_on_known_bad_text():
    source = "金丹之力。\n他心想，此事不妙。\n那座山峰在三十里外。"
    text = (
        "He would delve into the matter; he paused; she paused; it paused; "
        "they paused.\n"
        "The peak lay thirty li away, whilst the valley waited."
    )
    glossary = [_gloss("金丹", "Golden Core")]
    cats = {c.name: c for c in qm.rule_category_scores(text, source, glossary)}

    assert cats["banned_words"].violations >= 2  # 'delve' + 'whilst'
    assert cats["punctuation_carry"].violations >= 1  # >3 semicolons
    assert cats["unit_conversion"].violations >= 1  # untranslated 'li'
    # 金丹 present in source but 'Golden Core' absent from the English.
    assert cats["glossary_presence"].violations >= 1
    # opportunities are counted even where nothing fires.
    assert cats["sentence_shape"].opportunities > 0


def test_score_text_surface_only_when_no_source():
    out = qm.score_text("A short clean sentence; and another.", None, [])
    assert out["categories"] == []
    assert out["surface"]["semicolons"] == 1
    assert out["surface"]["words"] == 6
    assert "anchor_rate" in out["flow"]


def test_scorers_do_not_mutate_inputs():
    text = "He delved; whilst waiting."
    before = text
    qm.surface_metrics(text)
    qm.flow_metrics(text)
    assert text == before


# ---- aggregation math ---------------------------------------------------


def test_aggregate_categories_sums_and_keeps_per_chapter_rates():
    def _score(viol, opp):
        return {"categories": [
            {"name": "banned_words", "violations": viol, "reviews": 0,
             "opportunities": opp, "examples": []},
        ]}

    scored = [(1, _score(2, 10)), (2, _score(0, 10)), (3, _score(4, 10))]
    agg = qr._aggregate_categories(scored)
    bw = agg["banned_words"]
    assert bw["violations"] == 6
    assert bw["opportunities"] == 30
    assert bw["rate"] == pytest.approx(6 / 30)
    assert bw["per_chapter_rates"] == [0.2, 0.0, 0.4]


def test_config_tag_from_snapshot():
    snap = json.dumps({
        "prompt_template_version": "phase17-flow-seams-1",
        "translator_provider_type": "claude",
        "translator_model_id": "claude-opus-4-8",
        "refiner_model_id": "gemini-2.5-pro",
    })
    assert qr._config_tag(snap) == (
        "phase17-flow-seams-1 | claude:claude-opus-4-8 +refine:gemini-2.5-pro"
    )
    assert qr._config_tag(None) == "(no snapshot)"
    assert qr._config_tag("not json{") == "(no snapshot)"


# ---- DB-backed orchestration -------------------------------------------


async def _seed_two_arms():
    snap_a = json.dumps({
        "prompt_template_version": "v1", "translator_provider_type": "claude",
        "translator_model_id": "opus",
    })
    snap_b = json.dumps({
        "prompt_template_version": "v2", "translator_provider_type": "gemini",
        "translator_model_id": "flash",
    })
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES ('QR', 'paste')"
        )
        novel_id = cur.lastrowid
        rows = [
            (1, "金丹之力。三十里外。",
             "He would delve in; he paused; she paused; it paused; they did.", snap_a),
            (2, "清晨的阳光。", "Morning light filled the valley.", snap_b),
        ]
        ch_ids = {}
        fixups = {1: json.dumps({"rules": {"locked_case": 3, "em_dash": 1}, "total": 4})}
        for num, src, tgt, snap in rows:
            cur = await conn.execute(
                "INSERT INTO chapters (novel_id, chapter_num, original_text, "
                "translated_text, status, prompt_config_snapshot, fixup_audit) "
                "VALUES (?, ?, ?, ?, 'done', ?, ?)",
                (novel_id, num, src, tgt, snap, fixups.get(num)),
            )
            ch_ids[num] = cur.lastrowid
        await conn.execute(
            "INSERT INTO glossary_entries (novel_id, term_zh, term_en, category, "
            "locked, auto_detected) VALUES (?, '金丹', 'Golden Core', 'item', 1, 0)",
            (novel_id,),
        )
        # Discarded observer signal, seeded directly.
        for ch, kind in [(1, "mt_texture"), (1, "mt_texture"), (1, "residual_cjk"),
                         (2, "mt_texture")]:
            await conn.execute(
                "INSERT INTO chapter_observations (chapter_id, kind, excerpt) "
                "VALUES (?, ?, 'x')",
                (ch_ids[ch], kind),
            )
        await conn.commit()
    return novel_id


_FAKE_CONSISTENCY = {
    "tcr": {"overall_tcr": 0.9, "checkable": 10},
    "segment_reuse": {"substantive": {"reuse_rate_chars": 0.05}},
    "bracketed_blocks": {"identity_rate": 1.0},
}


async def test_load_range_harvests_observations_and_groups_by_config():
    novel_id = await _seed_two_arms()
    data = await qr._load_range(novel_id, 1, 2)
    assert len(data["chapters"]) == 2
    assert data["observations"]["mt_texture"] == {"count": 3, "chapters": 2}
    assert data["observations"]["residual_cjk"] == {"count": 1, "chapters": 1}

    card = qr._build_scorecard(novel_id, 1, 2, data, _FAKE_CONSISTENCY)
    assert card["chapters_scored"] == 2
    # Two distinct prompt-config arms -> two columns.
    assert len(card["by_config"]) == 2
    assert any("v1 | claude:opus" in tag for tag in card["by_config"])
    assert any("v2 | gemini:flash" in tag for tag in card["by_config"])
    # Chapter 1's English carries 'delve' -> banned_words fired in aggregate.
    assert card["categories"]["banned_words"]["violations"] >= 1
    assert card["consistency"]["overall_tcr"] == 0.9
    # Fixup self-audit harvested from chapters.fixup_audit (ch1 only).
    fx = card["fixup_churn"]
    assert fx["recorded_chapters"] == 1
    assert fx["rule_counts"]["locked_case"] == 3
    assert fx["rule_counts"]["em_dash"] == 1


async def test_load_range_empty_when_out_of_range():
    novel_id = await _seed_two_arms()
    data = await qr._load_range(novel_id, 500, 600)
    assert data["chapters"] == []


# ---- fixup self-audit + casing-collision detector -----------------------


def test_aggregate_fixups_counts_rules_and_flags_high_churn():
    audits = [
        (1, json.dumps({"rules": {"locked_case": 2, "em_dash": 1}, "total": 3})),
        (2, None),  # predates the column
        (3, json.dumps({"rules": {"locked_case": 9}, "total": 9})),  # high churn
    ]
    out = qr._aggregate_fixups(audits)
    assert out["recorded_chapters"] == 2
    assert out["rule_counts"]["locked_case"] == 11
    assert out["rule_chapters"]["locked_case"] == 2
    assert [d["chapter"] for d in out["high_churn_chapters"]] == [3]


def test_casing_collisions_flags_orphan_and_respects_escape_hatch():
    # Title-Case force-cased entry colliding with a lowercase-intent sibling.
    orphan = [
        _gloss("灵宝", "Spirit Treasure", category="item"),       # atomic -> force-cased
        _gloss("宝物", "spirit treasure", category="item"),       # lowercase intent
    ]
    coll = qr._casing_collisions(orphan)
    assert len(coll) == 1
    assert "Spirit Treasure" in coll[0]["force_cased"]
    assert "spirit treasure" in coll[0]["lowercase_intent"]

    # Escape hatch: the Title-Case row carries a 'lowercase' note -> not atomic,
    # so it is NOT force-cased and there is no collision.
    hatched = [
        GlossaryEntry(id=1, novel_id=1, term_zh="灵宝", term_en="Spirit Treasure",
                      category="item", notes="lowercase in narration", usage_note=None,
                      auto_detected=False, locked=True),
        _gloss("宝物", "spirit treasure", category="item"),
    ]
    assert qr._casing_collisions(hatched) == []


# ---- A/B diff renders ---------------------------------------------------


def test_diff_renders_delta(tmp_path, capsys):
    def _card(rate):
        return {
            "novel_id": 1, "chapter_range": [1, 5], "chapters_scored": 5,
            "categories": {
                "banned_words": {
                    "violations": int(rate * 50), "reviews": 0,
                    "opportunities": 50, "rate": rate,
                    "per_chapter_rates": [rate] * 5, "examples": [],
                }
            },
        }

    a = tmp_path / "base.json"
    b = tmp_path / "against.json"
    a.write_text(json.dumps(_card(0.20)), encoding="utf-8")
    b.write_text(json.dumps(_card(0.05)), encoding="utf-8")

    qr._diff(str(a), str(b))
    out = capsys.readouterr().out
    assert "banned_words" in out
    assert "better" in out  # against rate is lower than baseline
