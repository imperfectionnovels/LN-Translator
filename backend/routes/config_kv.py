"""Simple key/value config endpoints (Design v2 Phase G).

Reserved for app-level state — first_run_complete, etc. Per-novel state
belongs on the novels table. Values are short strings (typically "1" /
"0" or JSON), not blobs.
"""

from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.db import get_conn

router = APIRouter()


class ConfigValue(BaseModel):
    value: str = Field(max_length=2000)


@router.get("/config/{key}")
async def get_config(
    key: str, conn: aiosqlite.Connection = Depends(get_conn)
) -> dict:
    """Returns {value: <str>} or 404 when the key isn't set. Callers
    treat 404 as "use the default" rather than retrying — the absence
    of a key is semantically meaningful (e.g. first_run_complete missing
    = the user has never finished onboarding)."""
    cur = await conn.execute(
        "SELECT value FROM config_kv WHERE key = ?", (key,)
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="key not set")
    return {"key": key, "value": row["value"]}


@router.put("/config/{key}")
async def set_config(
    key: str,
    body: ConfigValue,
    conn: aiosqlite.Connection = Depends(get_conn),
) -> dict:
    """Upsert. Returns the value just written; the request is idempotent."""
    await conn.execute(
        "INSERT INTO config_kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, body.value),
    )
    await conn.commit()
    return {"key": key, "value": body.value}


@router.delete("/config/{key}", status_code=204)
async def delete_config(
    key: str, conn: aiosqlite.Connection = Depends(get_conn)
) -> None:
    """Idempotent — DELETE on a missing key returns 204, not 404."""
    await conn.execute("DELETE FROM config_kv WHERE key = ?", (key,))
    await conn.commit()
