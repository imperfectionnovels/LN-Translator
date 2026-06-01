"""Provider CRUD routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.db import open_conn
from backend.models import (
    Provider as ProviderModel,
)
from backend.models import (
    ProviderCreate,
    ProviderTestResult,
    ProviderUpdate,
)
from backend.services import providers as providers_svc
from backend.services.translators.factory import invalidate_provider_cache

router = APIRouter()


class SetSecretBody(BaseModel):
    """Body for POST /providers/{id}/set-secret. The secret value never
    appears in any provider response or DB row — only stored in the OS
    keychain under `LN-Translator/<secret_ref>`. If the provider has no
    secret_ref configured, the route 400s."""
    value: str = Field(min_length=1, max_length=500)


def _to_model(p: providers_svc.Provider) -> ProviderModel:
    return ProviderModel(
        id=p.id,
        name=p.name,
        provider_type=p.provider_type,
        base_url=p.base_url,
        model_id=p.model_id,
        params=p.params,
        secret_ref=p.secret_ref,
        is_default=p.is_default,
        last_tested_at=p.last_tested_at,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


@router.get("/catalog")
async def get_provider_catalog() -> list[dict]:
    """Return the catalog of supported provider types and their curated
    model versions. The Settings → Add Provider dialog reads this to
    populate the Type and Model dropdowns; the onboarding wizard reads it
    to resolve default model IDs.

    Stable shape — see `services/translator_catalog.to_api_payload` for
    the full field list.
    """
    from backend.services.translator_catalog import to_api_payload
    return to_api_payload()


@router.get("")
async def list_providers() -> list[ProviderModel]:
    return [_to_model(p) for p in await providers_svc.list_providers()]


@router.get("/{provider_id}")
async def get_provider(provider_id: int) -> ProviderModel:
    p = await providers_svc.load_provider(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="provider not found")
    return _to_model(p)


@router.post("", status_code=201)
async def create_provider(body: ProviderCreate) -> ProviderModel:
    if body.provider_type not in providers_svc.KNOWN_PROVIDER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown provider_type {body.provider_type!r}; "
                f"supported: {sorted(providers_svc.KNOWN_PROVIDER_TYPES)}"
            ),
        )
    try:
        p = await providers_svc.create_provider(
            name=body.name,
            provider_type=body.provider_type,
            model_id=body.model_id,
            base_url=body.base_url,
            params=body.params,
            secret_ref=body.secret_ref,
            is_default=body.is_default,
        )
    except Exception as e:
        # The most common failure is the UNIQUE(name) constraint.
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _to_model(p)


@router.patch("/{provider_id}")
async def update_provider(provider_id: int, body: ProviderUpdate) -> ProviderModel:
    # exclude_unset preserves explicit `null` values (the key is present in
    # the dict with value None) so the service can clear nullable columns
    # like `base_url` and `secret_ref`. Without this we couldn't move a
    # provider away from a stored secret_ref.
    updates = body.model_dump(exclude_unset=True)
    if "provider_type" in updates and updates["provider_type"] not in providers_svc.KNOWN_PROVIDER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown provider_type {updates['provider_type']!r}",
        )
    p = await providers_svc.update_provider(provider_id, updates)
    if p is None:
        raise HTTPException(status_code=404, detail="provider not found")
    # Provider config changed — drop the cached backend so the next request
    # picks up fresh model_id / secret / base_url.
    invalidate_provider_cache(provider_id)
    return _to_model(p)


@router.delete("/{provider_id}")
async def delete_provider(provider_id: int) -> dict:
    ok = await providers_svc.delete_provider(provider_id)
    if not ok:
        raise HTTPException(status_code=404, detail="provider not found")
    invalidate_provider_cache(provider_id)
    return {"deleted": provider_id}


@router.post("/{provider_id}/set-default")
async def set_default(provider_id: int) -> ProviderModel:
    p = await providers_svc.set_default(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="provider not found")
    return _to_model(p)


@router.post("/{provider_id}/test")
async def test_provider(provider_id: int) -> ProviderTestResult:
    p = await providers_svc.load_provider(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="provider not found")
    ok, message = await providers_svc.test_provider(p)
    if ok:
        await providers_svc.stamp_last_tested(provider_id)
    return ProviderTestResult(ok=ok, message=message)


@router.post("/{provider_id}/set-secret")
async def set_secret(provider_id: int, body: SetSecretBody) -> dict:
    """Persist a secret value to the OS keychain under the provider's
    secret_ref. Preferred path for the frozen EXE so secrets aren't
    sitting in .env. Returns {ok: True} on success; 503 when keyring
    is unavailable (e.g. headless Linux without dbus) so the UI can
    suggest the env-var fallback.
    """
    p = await providers_svc.load_provider(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="provider not found")
    if not p.secret_ref:
        raise HTTPException(
            status_code=400,
            detail="provider has no secret_ref configured; set one via "
                   "PATCH /providers/{id} first.",
        )
    ok = providers_svc.store_secret(p.secret_ref, body.value)
    if not ok:
        raise HTTPException(
            status_code=503,
            detail="OS keychain unavailable on this host. Set the env var "
                   f"named {p.secret_ref!r} instead — the resolver falls "
                   "back to env vars when keyring isn't available.",
        )
    invalidate_provider_cache(provider_id)
    return {"ok": True, "stored_under": p.secret_ref}


@router.delete("/{provider_id}/secret")
async def delete_secret(provider_id: int) -> dict:
    """Remove a stored secret from the OS keychain. Succeeds whether or not
    anything was actually stored: idempotent so the UI's "clear saved key"
    button never errors on a no-op."""
    p = await providers_svc.load_provider(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="provider not found")
    if p.secret_ref:
        providers_svc.delete_secret(p.secret_ref)
    invalidate_provider_cache(provider_id)
    return {"provider_id": provider_id, "secret_cleared": True}


# ============================================================
# Control-room data feeds for the settings page (see plan §01).
# All three are pure aggregations over existing columns: the only
# new schema piece is providers.last_tested_at (handled above).
# ============================================================

_STATS_WINDOW_DAYS = 30
_SPARKLINE_BUCKETS = 14


def _bucket_iso_dates(days: int) -> list[str]:
    """Return ISO `YYYY-MM-DD` strings for the last `days` days inclusive of
    today, oldest first. Used to align sparse SQL aggregates to a fixed-length
    sparkline array so the frontend doesn't have to fill gaps."""
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]


@router.get("/{provider_id}/stats")
async def provider_stats(provider_id: int) -> dict:
    """30-day rollup for the provider control-room card. All columns
    referenced here already exist:
      - chapters.translated_at  (Initiative 6)
      - chapters.cost_usd       (Section 6.1)
      - chapters.status         (the pipeline state machine)
      - chapter_translation_attempts.status (Bundle 2 F22)
      - novels.translator_provider_id (per-novel routing)

    A chapter is "translated by this provider" when its parent novel currently
    routes to this provider — there is no per-chapter provider column, so the
    join is novels.translator_provider_id. That can drift if a novel is rerouted
    after some chapters translate, but it's the same model the rest of the app
    uses (e.g. stats.py). Good enough; document the limitation here so the
    next reader doesn't go hunting.
    """
    p = await providers_svc.load_provider(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="provider not found")

    days = _bucket_iso_dates(_SPARKLINE_BUCKETS)
    first_bucket_iso = days[0]
    window_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=_STATS_WINDOW_DAYS)
    ).isoformat(timespec="seconds").replace("+00:00", "")

    async with open_conn() as conn:
        # Throughput + spend per day for the sparkline window.
        cur = await conn.execute(
            """
            SELECT substr(c.translated_at, 1, 10) AS day,
                   COUNT(*) AS chapters,
                   COALESCE(SUM(c.cost_usd), 0.0) AS spend
            FROM chapters c
            JOIN novels n ON n.id = c.novel_id
            WHERE n.translator_provider_id = ?
              AND c.translated_at IS NOT NULL
              AND c.translated_at >= ?
            GROUP BY day
            """,
            (provider_id, first_bucket_iso),
        )
        by_day: dict[str, dict] = {
            r["day"]: {"chapters": r["chapters"], "spend": float(r["spend"])}
            for r in await cur.fetchall()
        }

        # 30-day totals (chapters + spend).
        cur = await conn.execute(
            """
            SELECT COUNT(*) AS chapters, COALESCE(SUM(c.cost_usd), 0.0) AS spend
            FROM chapters c
            JOIN novels n ON n.id = c.novel_id
            WHERE n.translator_provider_id = ?
              AND c.translated_at IS NOT NULL
              AND c.translated_at >= ?
            """,
            (provider_id, window_cutoff),
        )
        row = await cur.fetchone()
        chapters_30d = int(row["chapters"] or 0)
        spend_30d = float(row["spend"] or 0.0)

        # Failure rate from the attempts log over the same window.
        cur = await conn.execute(
            """
            SELECT
              SUM(CASE WHEN a.status = 'error' THEN 1 ELSE 0 END) AS failures,
              COUNT(*) AS attempts
            FROM chapter_translation_attempts a
            JOIN chapters c ON c.id = a.chapter_id
            JOIN novels n ON n.id = c.novel_id
            WHERE n.translator_provider_id = ?
              AND a.started_at >= ?
            """,
            (provider_id, window_cutoff),
        )
        row = await cur.fetchone()
        attempts_30d = int(row["attempts"] or 0)
        failures_30d = int(row["failures"] or 0)
        failure_rate = (failures_30d / attempts_30d) if attempts_30d else 0.0

    chapters_buckets = [by_day.get(d, {}).get("chapters", 0) for d in days]
    spend_buckets = [round(by_day.get(d, {}).get("spend", 0.0), 4) for d in days]

    return {
        "provider_id": provider_id,
        "window_days": _STATS_WINDOW_DAYS,
        "chapters_translated_30d": chapters_30d,
        "chapters_translated_buckets": chapters_buckets,
        "spend_30d_usd": round(spend_30d, 4),
        "spend_30d_buckets": spend_buckets,
        "failure_rate_30d": round(failure_rate, 4),
        "failure_count_30d": failures_30d,
        "attempts_30d": attempts_30d,
        "last_tested_at": p.last_tested_at,
    }


@router.get("/{provider_id}/routed-novels")
async def provider_routed_novels(provider_id: int, limit: int = 12) -> dict:
    """Which novels currently route through this provider, either as
    primary translator or as refinement. The settings card shows the
    first `limit` as a chip-row with a "+N more" overflow link.
    """
    p = await providers_svc.load_provider(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="provider not found")
    limit = max(1, min(limit, 100))
    async with open_conn() as conn:
        cur = await conn.execute(
            """
            SELECT id, title,
                   CASE
                     WHEN translator_provider_id = ? AND refinement_provider_id = ? THEN 'both'
                     WHEN translator_provider_id = ? THEN 'translator'
                     WHEN refinement_provider_id = ? THEN 'refinement'
                     ELSE 'unknown'
                   END AS role
            FROM novels
            WHERE (translator_provider_id = ? OR refinement_provider_id = ?)
              AND deleted_at IS NULL
            ORDER BY id ASC
            LIMIT ?
            """,
            (provider_id, provider_id, provider_id, provider_id,
             provider_id, provider_id, limit),
        )
        rows = await cur.fetchall()
        novels = [
            {"id": r["id"], "title": r["title"], "role": r["role"]}
            for r in rows
        ]
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM novels "
            "WHERE (translator_provider_id = ? OR refinement_provider_id = ?) "
            "AND deleted_at IS NULL",
            (provider_id, provider_id),
        )
        total = int((await cur.fetchone())["n"] or 0)
    return {
        "provider_id": provider_id,
        "novels": novels,
        "total": total,
        "limit": limit,
    }


@router.get("/{provider_id}/activity")
async def provider_activity(provider_id: int, limit: int = 6) -> dict:
    """Last N translation attempts on novels routed through this provider.
    Maps `chapter_translation_attempts.status` into the 3-state ok/warn/err
    bucket the settings card uses for icons.
    """
    p = await providers_svc.load_provider(provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="provider not found")
    limit = max(1, min(limit, 50))
    async with open_conn() as conn:
        cur = await conn.execute(
            """
            SELECT a.started_at, a.finished_at, a.status AS att_status,
                   a.parse_error,
                   c.chapter_num, c.title_en,
                   n.title AS novel_title
            FROM chapter_translation_attempts a
            JOIN chapters c ON c.id = a.chapter_id
            JOIN novels n ON n.id = c.novel_id
            WHERE n.translator_provider_id = ?
            ORDER BY a.started_at DESC
            LIMIT ?
            """,
            (provider_id, limit),
        )
        rows = await cur.fetchall()

    def _bucket(status: str) -> str:
        if status in ("ok", "fallback_plaintext"):
            return "ok"
        if status == "parse_failed":
            return "warn"
        return "err"

    def _msg(status: str, novel_title: str, chapter_num: int) -> str:
        if status == "ok":
            return f"Translated {novel_title} · ch. {chapter_num}"
        if status == "fallback_plaintext":
            return f"Translated {novel_title} · ch. {chapter_num} (plaintext fallback)"
        if status == "parse_failed":
            return f"Parse retry · {novel_title} · ch. {chapter_num}"
        return f"Error · {novel_title} · ch. {chapter_num}"

    events = []
    for r in rows:
        started = r["started_at"]
        finished = r["finished_at"]
        duration_ms = None
        if started and finished:
            try:
                started_dt = datetime.fromisoformat(started.replace("Z", ""))
                finished_dt = datetime.fromisoformat(finished.replace("Z", ""))
                duration_ms = int((finished_dt - started_dt).total_seconds() * 1000)
            except ValueError:
                duration_ms = None
        events.append({
            "when_iso": started,
            "status": _bucket(r["att_status"]),
            "raw_status": r["att_status"],
            "novel_title": r["novel_title"],
            "chapter_num": r["chapter_num"],
            "duration_ms": duration_ms,
            "msg": _msg(r["att_status"], r["novel_title"], r["chapter_num"]),
        })
    return {"provider_id": provider_id, "events": events}
