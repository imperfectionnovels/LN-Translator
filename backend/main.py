import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.config import (
    FRONTEND_DIR,
    GEMINI_API_KEY,
    GEMINI_TRANSLATOR_MODEL,
    TRANSLATOR_BACKEND,
)
from backend.db import LAST_ORPHAN_RECOVERY, init_db
from backend.routes import (
    bookmarks,
    cache,
    chapters,
    config_kv,
    covers,
    find_replace,
    genres,
    global_glossary,
    glossary,
    imports,
    novels,
    observations,
    opus_mt as opus_mt_routes,
    providers,
    stats,
    tm,
    translate,
)
from backend.services import llm_cache
from backend.services import queue as queue_svc
from backend.services.providers import (
    Provider,
    ensure_default_provider,
    resolve_secret,
)

logger = logging.getLogger(__name__)

# Translator probe outcome from the most recent boot. Values:
#   "unknown" — initial, before _probe_backends runs.
#   "ok"      — probe succeeded.
#   "warn"    — transient failure (network blip, rate-limit, 5xx); boot
#               continued and the first real call will retry.
LAST_PROBE_STATE: dict[str, str] = {"translator": "unknown"}


async def _probe_one(role: str, provider: Provider) -> None:
    """Fail fast on startup if the resolved Provider is unusable.

    Distinguishes transient probe failures from permanent ones. Transient
    → warn and start so a flaky network doesn't block boot. Permanent →
    raise so the user fixes the config before serving.

    The probe targets the **resolved default Provider** (from the providers
    table), not the legacy `TRANSLATOR_BACKEND` env var, so the validation
    matches what the queue worker will actually route to.
    """
    backend = provider.provider_type
    if backend == "claude_cli":
        from backend.services.translators.claude_cli import probe_cli
        await probe_cli()
        LAST_PROBE_STATE[role] = "ok"
        return

    if backend == "claude_agent":
        from backend.services.translators.claude_agent import probe_sdk
        await probe_sdk()
        LAST_PROBE_STATE[role] = "ok"
        return

    if backend == "deepseek":
        from backend.services.translators.deepseek import probe_deepseek
        # Hand the resolved Provider through so the probe targets the same
        # api_key / model_id / base_url the queue worker will use, not the
        # legacy DEEPSEEK_* globals.
        await probe_deepseek(provider)
        LAST_PROBE_STATE[role] = "ok"
        return

    if backend == "gemini":
        # Resolve the API key from the provider's secret_ref (keyring first,
        # env var fallback). The legacy GEMINI_API_KEY global is the last-
        # resort fallback when the seeded default's secret_ref points at it.
        api_key = resolve_secret(provider) or GEMINI_API_KEY
        if not api_key:
            raise RuntimeError(
                f"Default provider {provider.name!r} is type 'gemini' but its "
                f"secret_ref {provider.secret_ref!r} is unset. Set the env var "
                f"or update the provider's secret_ref in /api/providers."
            )
        gemini_model = provider.model_id or GEMINI_TRANSLATOR_MODEL
        from google import genai
        from google.genai import errors as genai_errors
        from google.genai import types as genai_types

        transient_codes = {408, 429, 500, 502, 503, 504}
        transient_statuses = {
            "UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED",
            "INTERNAL", "UNKNOWN",
        }

        client = genai.Client(api_key=api_key)
        try:
            await client.aio.models.generate_content(
                model=gemini_model,
                contents="ok",
                config=genai_types.GenerateContentConfig(max_output_tokens=1),
            )
        except genai_errors.APIError as e:
            is_transient = (
                e.code in transient_codes
                or (e.status or "").upper() in transient_statuses
            )
            if is_transient:
                logger.warning(
                    "Gemini %s probe TRANSIENT failure for model %r: %s. Starting "
                    "anyway — first real call will retry.",
                    role, gemini_model, e,
                )
                LAST_PROBE_STATE[role] = "warn"
                return
            raise RuntimeError(
                f"Gemini {role} probe failed for model {gemini_model!r}: {e}. "
                "Check the API key and the model name."
            ) from e
        except Exception as e:
            logger.warning(
                "Gemini %s probe network failure for model %r: %s. Starting anyway.",
                role, gemini_model, e,
            )
            LAST_PROBE_STATE[role] = "warn"
            return
        logger.info("Gemini %s probe ok (model=%s)", role, gemini_model)
        LAST_PROBE_STATE[role] = "ok"
        return

    # ---- CLI subprocess types: probe by checking the binary exists ----
    _CLI_BINARIES = {
        "codex_cli":  ("codex",    ["--version"]),
        "gemini_cli": ("gemini",   ["--version"]),
        "opencode":   ("opencode", ["--version"]),
    }
    if backend in _CLI_BINARIES:
        from backend.services.translators._subprocess_utils import probe_binary
        binary, version_args = _CLI_BINARIES[backend]
        try:
            version = await probe_binary(binary, version_args)
            logger.info("%s probe ok (binary=%s, version=%s)", role, binary, version)
            LAST_PROBE_STATE[role] = "ok"
        except RuntimeError as e:
            # Don't kill boot — the user may add an alternative provider before
            # touching this one. Surface the error in logs so it's obvious why
            # the first translate request will fail.
            logger.warning(
                "%s probe: %s CLI missing for provider %r — first call will "
                "fail until installed. (%s)",
                role, binary, provider.name, e,
            )
            LAST_PROBE_STATE[role] = "warn"
        return

    # ---- API-key types: config-shape probe only (no live round-trip) ----
    # A real round-trip would burn paid tokens on every server boot. The
    # service-side /test endpoint already does cheap config-check probes;
    # boot just enforces that the secret resolves.
    _API_KEY_BACKENDS = {
        "anthropic_api", "openai", "xai", "mistral", "openrouter",
        "qwen", "zhipu", "moonshot", "groq", "openai_compatible",
    }
    if backend in _API_KEY_BACKENDS:
        if not provider.secret_ref:
            raise RuntimeError(
                f"Default provider {provider.name!r} (type {backend}) has no "
                f"secret_ref configured. Edit the provider in /settings and "
                "add an env var name."
            )
        if not resolve_secret(provider):
            raise RuntimeError(
                f"Default provider {provider.name!r} (type {backend}) has "
                f"secret_ref={provider.secret_ref!r} but it resolves to "
                "empty. Set the env var or use Settings → Set API key."
            )
        if backend == "openai_compatible" and not provider.base_url:
            raise RuntimeError(
                f"Default provider {provider.name!r} (openai_compatible) "
                "has no base_url. Edit the provider and point it at the "
                "vendor's /v1 endpoint."
            )
        logger.info(
            "%s probe ok (%s, config-only; first call will exercise the network)",
            role, backend,
        )
        LAST_PROBE_STATE[role] = "ok"
        return

    if backend == "ollama":
        # Local server — no auth, no boot probe. The first translate call will
        # surface a connection error if the user hasn't started Ollama.
        logger.info(
            "%s probe ok (ollama, local — first call will verify the server is running)",
            role,
        )
        LAST_PROBE_STATE[role] = "ok"
        return

    if backend == "opus_mt":
        # Offline CPU NMT. Boot must never fail when a pair isn't installed —
        # the user may have created the provider via the wizard and skipped
        # the model download; the first translate call (or the explicit
        # Download button in Settings) handles installation.
        from backend.services import opus_mt_models
        pair = provider.model_id or ""
        if pair not in opus_mt_models.SUPPORTED_PAIRS:
            logger.warning(
                "%s probe: opus_mt provider %r has unsupported model_id %r; "
                "the first translate call will fail until it's set to one of %s.",
                role, provider.name, pair,
                sorted(opus_mt_models.SUPPORTED_PAIRS),
            )
            LAST_PROBE_STATE[role] = "warn"
            return
        if not opus_mt_models.is_installed(pair):
            logger.warning(
                "%s probe: opus_mt pair %r is not installed yet — open Settings "
                "→ Providers to download the model. Boot continues.",
                role, pair,
            )
            LAST_PROBE_STATE[role] = "warn"
            return
        logger.info("%s probe ok (opus_mt %s, local)", role, pair)
        LAST_PROBE_STATE[role] = "ok"
        return

    # Unknown provider_type. Don't fail boot — a future provider type that
    # the probe doesn't know about should not block the server from coming up.
    logger.warning(
        "%s probe: unknown provider_type %r; skipping (first real call will "
        "surface the error).", role, backend,
    )
    LAST_PROBE_STATE[role] = "warn"


def _probe_bundled_runtime_data() -> None:
    """Fail loud at boot if a frozen-bundle data file is missing.

    The frozen EXE bundles third-party packages whose .py files import
    fine but die on first use when a sibling data file (JSON / dict /
    template) is absent. We discovered this the hard way when zhconv's
    zhcdict.json was omitted from an early build — translation jobs
    silently failed mid-flight with no startup-log signal, leaving the
    Translate button looking broken. Force a use-it-now check during
    lifespan so any future regression in LN-Translator.spec surfaces in
    startup.log on the very next launch instead of mid-chapter.

    Each probe is a real call against the runtime data path, not just an
    import. Imports succeed even when sibling data files are missing —
    that's the whole reason this class of bug was hard to spot.
    """
    # zhconv — glossary_filters._zh_convert loads zhcdict.json lazily on
    # the first convert() call. Pre-load it here so a missing dict fails
    # at boot, not when the user clicks Translate.
    try:
        from zhconv import convert as _zh_convert

        _zh_convert("測試", "zh-cn")  # 測試 → 测试
    except FileNotFoundError as e:
        logger.error(
            "Bundled runtime data MISSING: zhconv/zhcdict.json (%s). "
            "Glossary merge will fail mid-translate. Rebuild the EXE — "
            "the spec must include collect_data_files('zhconv').",
            e,
        )
    except Exception:
        logger.exception("zhconv probe failed (non-fatal)")

    # chardet — encoding detection on uploaded .txt files. Models live in
    # chardet/models/*.bin. Skip the probe under a tiny sample so we don't
    # mistake a confidence-zero result for a missing model.
    try:
        import chardet

        chardet.detect(b"hello world")
    except FileNotFoundError as e:
        logger.error(
            "Bundled runtime data MISSING: chardet models (%s). "
            ".txt uploads with non-UTF-8 encoding will fail to decode.",
            e,
        )
    except Exception:
        logger.exception("chardet probe failed (non-fatal)")

    # cloudscraper — CF v1/v2 fallback in services/scrapers/cloudflare.py
    # reads user_agent/browsers.json at construct time. Missing file =
    # any cloudflare-fronted scrape will crash. Only probe if the file is
    # plausibly needed; CF bypass is only used by /scrape, but the import
    # cost is tiny.
    try:
        import cloudscraper

        # create_scraper() reads browsers.json. Don't actually send a
        # request; the construct alone is the canary.
        cloudscraper.create_scraper()
    except FileNotFoundError as e:
        logger.error(
            "Bundled runtime data MISSING: cloudscraper/user_agent/browsers.json (%s). "
            "URL scraping will fail when the Cloudflare bypass tier is reached.",
            e,
        )
    except Exception:
        logger.exception("cloudscraper probe failed (non-fatal)")


async def _probe_backends(default_provider: Provider | None) -> None:
    """Probe the **resolved** default provider, not the legacy env var.

    If `default_provider` is None (no providers configured yet) we skip the
    probe and let the first real translation surface the configuration
    error. This matches the desktop-app first-run flow where the user
    arrives at the settings page with no providers and adds one through
    the UI.
    """
    if default_provider is None:
        logger.info(
            "translator probe skipped: no default provider configured yet. "
            "Add one via /api/providers or the settings UI."
        )
        LAST_PROBE_STATE["translator"] = "warn"
        return
    await _probe_one("translator", default_provider)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Multi-worker check. The translate queue holds a process-global
    # asyncio lock for serial single-chapter-at-a-time execution. Running
    # multiple worker processes defeats it: each worker gets its own copy
    # and chapters can translate in parallel, burning the subscription
    # window. Warn once; do not refuse.
    web_concurrency = os.environ.get("WEB_CONCURRENCY", "").strip()
    uvicorn_workers = os.environ.get("UVICORN_WORKERS", "").strip()
    worker_signal = web_concurrency or uvicorn_workers
    if worker_signal and worker_signal != "1":
        logger.warning(
            "WEB_CONCURRENCY/UVICORN_WORKERS=%s detected — this app holds "
            "a process-global asyncio lock for serial translation. With "
            "multiple workers each gets its own lock; chapters can translate "
            "in parallel and burn the Claude subscription window. "
            "Run with a single worker.",
            worker_signal,
        )
    await init_db()
    # Probe bundled-package data files before anything tries to use them.
    # Logs (and surfaces in startup.log) if zhconv/chardet/cloudscraper data
    # is missing from the frozen bundle. Cheap; runs at most once per boot.
    await asyncio.to_thread(_probe_bundled_runtime_data)
    # Seed a default Provider row from the legacy TRANSLATOR_BACKEND env var
    # if no providers exist yet. Once seeded, the providers table is the
    # source of truth and this is a no-op on every subsequent boot.
    default_provider = await ensure_default_provider()
    await _probe_backends(default_provider)
    try:
        removed = await asyncio.to_thread(llm_cache.gc_orphan_tmp_files)
        if removed:
            logger.info("llm_cache GC removed %d orphan tmp files", removed)
    except Exception:
        logger.exception("llm_cache GC failed (non-fatal)")
    await queue_svc.drain_on_startup()
    # Free-draft worker lane drains independently of the LLM queue.
    try:
        from backend.services import free_draft_queue
        await free_draft_queue.drain_on_startup()
    except Exception:
        logger.exception("free_draft_queue drain failed (non-fatal)")
    # Re-fire the import runner for any novel still mid-scrape after a
    # crash. Recipes with persisted skeleton URLs auto-resume; bulk /
    # EPUB partials flip to 'paused' so the user sees them in the
    # library card. Non-blocking — runners spawn as background tasks.
    try:
        from backend.services import import_runner
        await import_runner.drain_imports_on_startup()
    except Exception:
        logger.exception("import_runner drain failed (non-fatal)")
    try:
        yield
    finally:
        # Cancel in-flight queue workers so subprocess cleanup runs before
        # the event loop is torn down.
        await queue_svc.shutdown()


app = FastAPI(title="Chinese Novel Translator", lifespan=lifespan)

app.include_router(translate.router, prefix="/api/translate", tags=["translate"])
app.include_router(novels.router, prefix="/api/novels", tags=["novels"])
app.include_router(chapters.router, prefix="/api", tags=["chapters"])
app.include_router(glossary.router, prefix="/api", tags=["glossary"])
app.include_router(providers.router, prefix="/api/providers", tags=["providers"])
# Top-level /api/opus-mt/ for model-lifecycle endpoints. Kept off the
# /api/providers/{id}/ prefix so the dynamic-segment route in routes/providers.py
# cannot shadow these paths.
app.include_router(opus_mt_routes.router, prefix="/api/opus-mt", tags=["opus-mt"])
app.include_router(genres.router, prefix="/api/genres", tags=["genres"])
app.include_router(cache.router, prefix="/api/cache", tags=["cache"])
app.include_router(observations.router, prefix="/api", tags=["observations"])
app.include_router(covers.router, prefix="/api/novels", tags=["covers"])
app.include_router(bookmarks.router, prefix="/api", tags=["bookmarks"])
app.include_router(global_glossary.router, prefix="/api", tags=["global-glossary"])
app.include_router(find_replace.router, prefix="/api", tags=["find-replace"])
app.include_router(tm.router, prefix="/api", tags=["tm"])
app.include_router(stats.router, prefix="/api", tags=["stats"])
app.include_router(config_kv.router, prefix="/api", tags=["config"])
app.include_router(imports.router, prefix="/api/imports", tags=["imports"])


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "translator_backend": TRANSLATOR_BACKEND,
        "orphan_recovery": dict(LAST_ORPHAN_RECOVERY),
        "probe_state": dict(LAST_PROBE_STATE),
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/library")
async def library_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "library.html")


@app.get("/reader")
async def reader_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "reader.html")


@app.get("/novel")
async def novel_overview_page() -> FileResponse:
    """Per-novel overview surface (2026-05-25). Renders metadata,
    primary + secondary genres, source language, pipeline overrides,
    and stats/glossary summaries for one novel. Linked from the
    library card title and the reader breadcrumb."""
    return FileResponse(FRONTEND_DIR / "novel-overview.html")


@app.get("/glossary")
async def glossary_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "glossary.html")


@app.get("/glossary/global")
async def glossary_global_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "glossary-global.html")


@app.get("/find-replace")
async def find_replace_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "find-replace.html")


@app.get("/stats")
async def stats_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "stats.html")


@app.get("/settings")
async def settings_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "settings.html")


@app.get("/queue")
async def queue_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "queue.html")


@app.get("/onboarding")
async def onboarding_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "onboarding.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
