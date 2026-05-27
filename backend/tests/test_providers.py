"""Phase 1a tests for the per-novel provider abstraction.

Covers:
- Schema migrations land on a fresh DB (new novels/chapters columns + providers table).
- `_drop_dead_columns` rebuild preserves the new novel columns on a DB that
  still carries the legacy `humanizer_tone` sentinel.
- Provider CRUD + set_default cross-row atomicity.
- Queue worker resolves the per-novel translator_provider_id.
"""

from __future__ import annotations

import aiosqlite
import pytest

from backend.db import init_db, open_conn
from backend.services import providers as providers_svc

pytestmark = pytest.mark.asyncio


async def _reset_db() -> None:
    """Truncate every table the tests touch. Cheaper than reinit; matches the
    pattern other test files use."""
    async with open_conn() as conn:
        for table in ("chapters", "novels", "providers"):
            try:
                await conn.execute(f"DELETE FROM {table}")
            except aiosqlite.OperationalError:
                pass
        await conn.commit()


@pytest.fixture(autouse=True)
async def fresh_db():
    await init_db()
    await _reset_db()
    yield
    await _reset_db()


# ----- schema migration -----

async def test_novels_has_phase1_columns():
    async with open_conn() as conn:
        cur = await conn.execute("PRAGMA table_info(novels)")
        cols = {r[1] for r in await cur.fetchall()}
    for required in (
        "source_language",
        "genre",
        "custom_style_brief",
        "translator_provider_id",
        "refinement_provider_id",
    ):
        assert required in cols, f"novels is missing column {required!r}"


async def test_chapters_has_refinement_columns():
    async with open_conn() as conn:
        cur = await conn.execute("PRAGMA table_info(chapters)")
        cols = {r[1] for r in await cur.fetchall()}
    for required in ("refinement_status", "refined_text", "refinement_error", "refined_at"):
        assert required in cols, f"chapters is missing column {required!r}"


async def test_providers_table_exists():
    async with open_conn() as conn:
        cur = await conn.execute("PRAGMA table_info(providers)")
        cols = {r[1] for r in await cur.fetchall()}
    for required in (
        "id", "name", "provider_type", "base_url", "model_id",
        "params_json", "secret_ref", "is_default",
    ):
        assert required in cols


async def test_source_language_defaults_to_zh():
    async with open_conn() as conn:
        await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, ?)",
            ("test", "paste"),
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT source_language FROM novels WHERE title = 'test'"
        )
        r = await cur.fetchone()
    assert r["source_language"] == "zh"


async def test_refinement_status_defaults_to_none():
    """A freshly-inserted chapter row gets refinement_status='none' so the
    Phase 4 worker can use the column as an unambiguous opt-in signal."""
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, ?)",
            ("test", "paste"),
        )
        novel_id = cur.lastrowid
        await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text) "
            "VALUES (?, 1, '')",
            (novel_id,),
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT refinement_status FROM chapters WHERE novel_id = ?",
            (novel_id,),
        )
        r = await cur.fetchone()
    assert r["refinement_status"] == "none"


# ----- _drop_dead_columns preservation -----

async def test_drop_dead_columns_preserves_new_novel_columns():
    """Simulate an old DB that hasn't yet run the humanizer cleanup. After
    init_db, the rebuild should preserve the Phase 1 columns even though they
    weren't present when the legacy sentinel column existed."""
    # Build a "legacy" novels table with humanizer_tone present + a value in
    # the new columns. The additive migrations should add the columns, then
    # _drop_dead_columns should carry them forward when it rebuilds.
    async with open_conn() as conn:
        # Drop and rebuild novels with the pre-cleanup shape.
        await conn.execute("PRAGMA foreign_keys = OFF")
        await conn.execute("DROP TABLE IF EXISTS chapter_fts")
        for shadow in (
            "chapter_fts_data", "chapter_fts_idx",
            "chapter_fts_docsize", "chapter_fts_config",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {shadow}")
        await conn.execute("DROP TABLE IF EXISTS chapters")
        await conn.execute("DROP TABLE IF EXISTS novels")
        await conn.execute(
            "CREATE TABLE novels (id INTEGER PRIMARY KEY, title TEXT, "
            "source_type TEXT, source_url TEXT, created_at TEXT, "
            "style_note TEXT, humanizer_tone TEXT, "
            "source_language TEXT NOT NULL DEFAULT 'zh', "
            "genre TEXT, custom_style_brief TEXT, "
            "translator_provider_id INTEGER, refinement_provider_id INTEGER)"
        )
        await conn.execute(
            "CREATE TABLE chapters (id INTEGER PRIMARY KEY, novel_id INTEGER, "
            "chapter_num INTEGER, title_zh TEXT, title_en TEXT, "
            "original_text TEXT NOT NULL DEFAULT '', translated_text TEXT, "
            "status TEXT NOT NULL DEFAULT 'pending', error_msg TEXT, "
            "translate_queued INTEGER NOT NULL DEFAULT 0, "
            "force_retranslate INTEGER NOT NULL DEFAULT 0, "
            "translation_degraded INTEGER NOT NULL DEFAULT 0, "
            "glossary_merge_error TEXT, humanized_text TEXT, "
            "UNIQUE (novel_id, chapter_num))"
        )
        # Insert a row whose new-column values must survive the rebuild.
        await conn.execute(
            "INSERT INTO novels (title, source_type, source_url, created_at, "
            "style_note, humanizer_tone, source_language, genre, "
            "custom_style_brief) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("survivor", "paste", None, "2026-01-01",
             "test brief", "scholarly", "zh", "xianxia", "custom prose"),
        )
        await conn.commit()
        await conn.execute("PRAGMA foreign_keys = ON")

    # Trigger the cleanup pass.
    await init_db()

    # Confirm the row's Phase 1 fields survived.
    async with open_conn() as conn:
        cur = await conn.execute(
            "SELECT source_language, genre, custom_style_brief, style_note "
            "FROM novels WHERE title = 'survivor'"
        )
        row = await cur.fetchone()
    assert row is not None, "row was dropped during rebuild"
    assert row["source_language"] == "zh"
    assert row["genre"] == "xianxia"
    assert row["custom_style_brief"] == "custom prose"
    assert row["style_note"] == "test brief"


# ----- provider CRUD -----

async def test_create_and_load_provider():
    p = await providers_svc.create_provider(
        name="test-gemini",
        provider_type="gemini",
        model_id="gemini-3-pro-preview",
        secret_ref="GEMINI_API_KEY",
    )
    assert p.id > 0
    loaded = await providers_svc.load_provider(p.id)
    assert loaded is not None
    assert loaded.name == "test-gemini"
    assert loaded.provider_type == "gemini"
    # First provider in an empty table is auto-promoted to default. The
    # second provider in the same table (without is_default=True) is not.
    assert loaded.is_default is True
    p2 = await providers_svc.create_provider(
        name="second", provider_type="gemini", model_id="m2",
    )
    assert p2.is_default is False


async def test_first_provider_auto_becomes_default_even_when_flag_false():
    """Regression guard for the empty-state UX. The settings page's
    'first provider becomes default automatically' copy depends on the
    backend forcing is_default on row #1 regardless of the flag."""
    p = await providers_svc.create_provider(
        name="solo",
        provider_type="gemini",
        model_id="m",
        is_default=False,  # explicit False — backend must still promote
    )
    assert p.is_default is True
    default = await providers_svc.get_default_provider()
    assert default is not None and default.id == p.id


async def test_set_default_is_atomic():
    p1 = await providers_svc.create_provider(
        name="a", provider_type="gemini", model_id="m1", is_default=True,
    )
    p2 = await providers_svc.create_provider(
        name="b", provider_type="gemini", model_id="m2",
    )
    # Initially p1 is default.
    default = await providers_svc.get_default_provider()
    assert default is not None and default.id == p1.id

    # Promote p2; p1 must lose the flag in the same transaction.
    await providers_svc.set_default(p2.id)
    default = await providers_svc.get_default_provider()
    assert default is not None and default.id == p2.id

    # Verify p1 lost the flag.
    p1_after = await providers_svc.load_provider(p1.id)
    assert p1_after is not None and p1_after.is_default is False


async def test_unknown_provider_type_rejected_by_test_provider():
    p = await providers_svc.create_provider(
        name="bogus",
        provider_type="nonexistent_backend",
        model_id="m",
    )
    ok, msg = await providers_svc.test_provider(p)
    assert ok is False
    assert "nonexistent_backend" in msg


async def test_test_provider_warns_about_missing_secret():
    p = await providers_svc.create_provider(
        name="needs-key",
        provider_type="gemini",
        model_id="m",
        secret_ref="DEFINITELY_NOT_SET_FOR_TESTS",
    )
    ok, msg = await providers_svc.test_provider(p)
    assert ok is False
    assert "DEFINITELY_NOT_SET_FOR_TESTS" in msg


# NOTE: the previous test_provider_pricing_round_trips and
# test_provider_without_pricing_defaults_to_none tests were dropped on
# 2026-05-26 when the user-entered pricing fields were removed from the
# Add Provider dialog (catalog redesign expanding providers from 4 to
# 17 types). The Provider dataclass and providers DB table no longer
# carry pricing columns; chapters.cost_usd (actual recorded spend) is
# unchanged and still exercised by test_token_cost.py.


async def test_test_provider_makes_no_network_call(monkeypatch):
    """Regression guard: the settings UI's Test button hits this on every
    click, and a real LLM round-trip would burn paid quota each time. The
    config-only contract is load-bearing — if you ever promote this to a
    real round-trip, do it behind a separate ?deep=true code path and
    leave the default cheap.

    Implementation: spy on the constructors of the two network-capable
    clients used by Gemini / DeepSeek backends (httpx.AsyncClient,
    google.genai.Client). Replacing the classes outright would break SDKs
    that subclass them at module import; wrapping `__init__` leaves the
    class shape intact while still catching instantiation.
    """
    import httpx

    constructed: list[str] = []

    real_httpx_init = httpx.AsyncClient.__init__
    def _spy_httpx(self, *a, **kw):
        constructed.append("httpx.AsyncClient")
        return real_httpx_init(self, *a, **kw)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _spy_httpx)

    try:
        from google import genai
        real_genai_init = genai.Client.__init__
        def _spy_genai(self, *a, **kw):
            constructed.append("genai.Client")
            return real_genai_init(self, *a, **kw)
        monkeypatch.setattr(genai.Client, "__init__", _spy_genai)
    except ImportError:
        pass

    # Valid-config Gemini provider: secret resolves via env, model_id set.
    monkeypatch.setenv("REGRESSION_KEY", "sk-not-real-but-non-empty")
    p = await providers_svc.create_provider(
        name="cheap-test",
        provider_type="gemini",
        model_id="gemini-3-pro-preview",
        secret_ref="REGRESSION_KEY",
    )
    ok, msg = await providers_svc.test_provider(p)
    assert ok is True, msg
    assert constructed == [], (
        f"test_provider instantiated network client(s) {constructed} — "
        f"it must stay config-only. If you want a real round-trip, gate it "
        f"behind a separate ?deep=true code path."
    )
    assert "round-trip" in msg.lower(), (
        "success message should advertise that no round-trip happened, so a "
        "future contributor reading the UI tooltip understands the contract"
    )


# ----- PATCH null-clearing semantics (P2 fix) -----

async def test_update_provider_can_clear_nullable_fields():
    """PATCH {base_url: null, secret_ref: null} must actually clear those
    fields. The earlier `kwarg is not None` shape silently ignored explicit
    nulls — the route now uses an updates dict with key-presence semantics.
    """
    p = await providers_svc.create_provider(
        name="clearable",
        provider_type="deepseek",
        model_id="m",
        base_url="https://api.deepseek.com",
        secret_ref="DEEPSEEK_API_KEY",
    )
    updated = await providers_svc.update_provider(
        p.id, {"base_url": None, "secret_ref": None},
    )
    assert updated is not None
    assert updated.base_url is None
    assert updated.secret_ref is None


async def test_update_provider_ignores_omitted_keys():
    """Keys not in the updates dict must not touch the column."""
    p = await providers_svc.create_provider(
        name="partial",
        provider_type="gemini",
        model_id="m",
        base_url="https://generativelanguage.googleapis.com",
        secret_ref="GEMINI_API_KEY",
    )
    updated = await providers_svc.update_provider(p.id, {"name": "renamed"})
    assert updated is not None
    assert updated.name == "renamed"
    assert updated.base_url == "https://generativelanguage.googleapis.com"
    assert updated.secret_ref == "GEMINI_API_KEY"


# ----- delete_provider promotes successor (P2 fix) -----

async def test_delete_default_promotes_oldest_survivor():
    """Deleting the default with other providers present must atomically
    promote another row so the live session is never left without a
    default (which would silently fall through to legacy env routing)."""
    p1 = await providers_svc.create_provider(
        name="a", provider_type="gemini", model_id="m1", is_default=True,
    )
    p2 = await providers_svc.create_provider(
        name="b", provider_type="gemini", model_id="m2",
    )
    await providers_svc.create_provider(
        name="c", provider_type="gemini", model_id="m3",
    )
    ok = await providers_svc.delete_provider(p1.id)
    assert ok is True
    default = await providers_svc.get_default_provider()
    assert default is not None
    assert default.id == p2.id  # oldest surviving


async def test_delete_only_provider_leaves_no_default():
    """Deleting the only provider is allowed and leaves the table empty
    (the user must configure a new one via the settings UI)."""
    p = await providers_svc.create_provider(
        name="solo", provider_type="gemini", model_id="m", is_default=True,
    )
    ok = await providers_svc.delete_provider(p.id)
    assert ok is True
    assert await providers_svc.get_default_provider() is None


async def test_delete_non_default_leaves_default_alone():
    p1 = await providers_svc.create_provider(
        name="a", provider_type="gemini", model_id="m1", is_default=True,
    )
    p2 = await providers_svc.create_provider(
        name="b", provider_type="gemini", model_id="m2",
    )
    await providers_svc.delete_provider(p2.id)
    default = await providers_svc.get_default_provider()
    assert default is not None
    assert default.id == p1.id


# ----- queue worker provider resolution (P3 fix) -----

async def test_queue_resolves_per_novel_provider(monkeypatch):
    """A novel with translator_provider_id set must route into
    translate_chapter with that exact Provider. Stubs translate_chapter
    so we capture the kwarg without making an LLM call."""
    from backend.models import TranslationResult
    from backend.services import queue as queue_svc

    chosen = await providers_svc.create_provider(
        name="chosen",
        provider_type="gemini",
        model_id="chosen-model",
        is_default=False,
    )
    other = await providers_svc.create_provider(
        name="default",
        provider_type="gemini",
        model_id="default-model",
        is_default=True,
    )

    # Seed a novel that points at `chosen`.
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type, translator_provider_id) "
            "VALUES (?, ?, ?)",
            ("routed", "paste", chosen.id),
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "status, translate_queued) VALUES (?, 1, '原文', 'pending', 1)",
            (novel_id,),
        )
        chapter_id = cur.lastrowid
        await conn.commit()

    captured: dict = {}

    async def _capture_translate(*args, provider=None, **kwargs):
        captured["provider"] = provider
        return TranslationResult(
            title_en="t", translated_text="body", new_terms=[],
        )

    monkeypatch.setattr(
        "backend.services.queue.translate_chapter", _capture_translate,
    )

    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)

    assert captured.get("provider") is not None, (
        "queue worker did not pass a provider to translate_chapter"
    )
    assert captured["provider"].id == chosen.id, (
        f"expected provider id {chosen.id} (chosen) but got "
        f"{captured['provider'].id} (likely default-fallback bug)"
    )
    # Sanity: the captured provider is `chosen`, not `other` (default).
    assert captured["provider"].id != other.id


# ----- per-provider model_id and secret routing (Phase 1b) -----

async def test_gemini_translator_uses_provider_model_id(monkeypatch):
    """Two providers of type 'gemini' with different model_ids must produce
    translator instances whose model_id (and therefore cache_identity) reflect
    the provider, not the legacy GEMINI_TRANSLATOR_MODEL global."""
    monkeypatch.setenv("FAKE_KEY_A", "key-a")
    monkeypatch.setenv("FAKE_KEY_B", "key-b")
    # Stub the genai client so we don't try to reach Google's API.
    import backend.services.translators.gemini as gemini_mod
    monkeypatch.setattr(
        gemini_mod.genai, "Client", lambda api_key: object(),
    )

    p1 = await providers_svc.create_provider(
        name="gem-pro",
        provider_type="gemini",
        model_id="gemini-3-pro-preview",
        secret_ref="FAKE_KEY_A",
    )
    p2 = await providers_svc.create_provider(
        name="gem-flash",
        provider_type="gemini",
        model_id="gemini-3-flash-preview",
        secret_ref="FAKE_KEY_B",
    )

    t1 = gemini_mod.GeminiTranslator(provider=p1)
    t2 = gemini_mod.GeminiTranslator(provider=p2)
    assert t1.model_id == "gemini-3-pro-preview"
    assert t2.model_id == "gemini-3-flash-preview"
    # cache_identity must differ so llm_cache buckets stay separate.
    assert t1.cache_identity() != t2.cache_identity()


async def test_gemini_translator_raises_when_secret_missing(monkeypatch):
    """A provider whose secret_ref env var is unset must fail fast at
    construction time so the user sees the misconfiguration before the
    queue worker churns through retries."""
    monkeypatch.delenv("DEFINITELY_UNSET_KEY", raising=False)
    # Also clear the legacy global fallback so the test is deterministic.
    import backend.services.translators.gemini as gemini_mod
    monkeypatch.setattr(gemini_mod, "GEMINI_API_KEY", "")

    p = await providers_svc.create_provider(
        name="needs-key",
        provider_type="gemini",
        model_id="m",
        secret_ref="DEFINITELY_UNSET_KEY",
    )
    with pytest.raises(RuntimeError, match="no resolvable API key"):
        gemini_mod.GeminiTranslator(provider=p)


async def test_deepseek_draft_model_follows_provider(monkeypatch):
    """When a Provider is passed and params.draft_model is unset, the draft
    pass must use provider.model_id — not the legacy DEEPSEEK_DRAFT_MODEL
    env var. Otherwise DEEPSEEK_REVISION_ENABLED=False would silently run
    a different model than the user selected.
    """
    monkeypatch.setenv("FAKE_DEEPSEEK_KEY", "k")
    import backend.services.translators.deepseek as ds_mod
    # Stub the OpenAI client so we don't try to reach DeepSeek.
    monkeypatch.setattr(ds_mod.openai, "AsyncOpenAI", lambda **kwargs: object())

    p = await providers_svc.create_provider(
        name="ds-single",
        provider_type="deepseek",
        model_id="deepseek-v4-pro",
        secret_ref="FAKE_DEEPSEEK_KEY",
    )
    t = ds_mod.DeepSeekTranslator(provider=p)
    assert t.model_id == "deepseek-v4-pro"
    assert t._draft_model == "deepseek-v4-pro", (
        "draft model must follow provider.model_id, not DEEPSEEK_DRAFT_MODEL"
    )


async def test_deepseek_draft_model_override_via_params(monkeypatch):
    """params.draft_model overrides the default draft model. This preserves
    the cheap-draft + expensive-revise workflow for power users without
    coupling it to a global env var."""
    monkeypatch.setenv("FAKE_DEEPSEEK_KEY", "k")
    import backend.services.translators.deepseek as ds_mod
    monkeypatch.setattr(ds_mod.openai, "AsyncOpenAI", lambda **kwargs: object())

    p = await providers_svc.create_provider(
        name="ds-split",
        provider_type="deepseek",
        model_id="deepseek-v4-pro",
        secret_ref="FAKE_DEEPSEEK_KEY",
        params={"draft_model": "deepseek-chat"},
    )
    t = ds_mod.DeepSeekTranslator(provider=p)
    assert t.model_id == "deepseek-v4-pro"
    assert t._draft_model == "deepseek-chat"


async def test_provider_secret_does_not_fall_back_to_global(monkeypatch):
    """When a Provider is passed but its secret_ref resolves to nothing,
    the backend must NOT silently substitute the legacy GEMINI_API_KEY
    global. That mask a bad provider config — the user thinks they're
    routing through the Provider's key but they're actually using the
    legacy global key.
    """
    # The global IS set, but the Provider's secret_ref is not. The construction
    # must still fail because the explicit Provider's config is broken.
    monkeypatch.setenv("GEMINI_API_KEY", "global-key-should-be-ignored")
    monkeypatch.delenv("PROVIDER_KEY_UNSET", raising=False)
    import backend.services.translators.gemini as gemini_mod
    # Mirror module-level constant so the test is deterministic.
    monkeypatch.setattr(gemini_mod, "GEMINI_API_KEY", "global-key-should-be-ignored")
    monkeypatch.setattr(gemini_mod.genai, "Client", lambda api_key: object())

    p = await providers_svc.create_provider(
        name="strict",
        provider_type="gemini",
        model_id="m",
        secret_ref="PROVIDER_KEY_UNSET",
    )
    with pytest.raises(RuntimeError, match="no resolvable API key"):
        gemini_mod.GeminiTranslator(provider=p)


async def test_factory_passes_provider_into_constructor(monkeypatch):
    """get_translator must thread the Provider through to the backend
    constructor so the backend can read model_id / secret. Regression guard
    against a future refactor that drops the kwarg."""
    monkeypatch.setenv("FAKE_GEMINI_KEY", "test-key")
    import backend.services.translators.gemini as gemini_mod
    monkeypatch.setattr(
        gemini_mod.genai, "Client", lambda api_key: object(),
    )

    from backend.services.translators import factory

    p = await providers_svc.create_provider(
        name="routed",
        provider_type="gemini",
        model_id="gemini-test-model",
        secret_ref="FAKE_GEMINI_KEY",
    )
    factory.invalidate_provider_cache()
    translator = factory.get_translator(p)
    assert translator.model_id == "gemini-test-model", (
        "factory did not pass Provider.model_id into the backend constructor"
    )


async def test_queue_falls_back_to_default_when_novel_unset(monkeypatch):
    """Novel with NULL translator_provider_id falls back to the default
    Provider row (not to legacy env routing while a default exists)."""
    from backend.models import TranslationResult
    from backend.services import queue as queue_svc

    default = await providers_svc.create_provider(
        name="def", provider_type="gemini", model_id="m", is_default=True,
    )
    async with open_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO novels (title, source_type) VALUES (?, ?)",
            ("unrouted", "paste"),
        )
        novel_id = cur.lastrowid
        cur = await conn.execute(
            "INSERT INTO chapters (novel_id, chapter_num, original_text, "
            "status, translate_queued) VALUES (?, 1, '原文', 'pending', 1)",
            (novel_id,),
        )
        chapter_id = cur.lastrowid
        await conn.commit()

    captured: dict = {}

    async def _capture_translate(*args, provider=None, **kwargs):
        captured["provider"] = provider
        return TranslationResult(
            title_en="t", translated_text="body", new_terms=[],
        )

    monkeypatch.setattr(
        "backend.services.queue.translate_chapter", _capture_translate,
    )

    async with open_conn() as conn:
        await queue_svc._translate_chapter_in_db(conn, novel_id, chapter_id)

    assert captured.get("provider") is not None
    assert captured["provider"].id == default.id
