"""Load and persist app settings (api_base_url, cors_origins, debug_log_path, debug_ingest_url) from DB with env fallbacks."""
from __future__ import annotations

import asyncio
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as env_settings
from app.database import AsyncSessionLocal, AppSetting

# In-memory cache: updated by get_app_settings(); read by sync getters (e.g. debug log path).
_settings_cache: dict[str, str] = {}
_cache_lock = asyncio.Lock()

KEYS = (
    "api_base_url",
    "cors_origins",
    "debug_log_path",
    "debug_ingest_url",
    # New Oracle DB keys
    "oracle_db_dsn",
    "oracle_db_user",
    "oracle_wallet_path",
    # Custom system prompt
    "system_prompt",
)

# Portable default: .cursor/debug.log under the project root.
_DEFAULT_DEBUG_LOG_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".cursor", "debug.log")
)
DEFAULTS = {
    "api_base_url": lambda: env_settings.backend_url,
    "cors_origins": lambda: "http://localhost:3000",
    "debug_log_path": lambda: _DEFAULT_DEBUG_LOG_PATH,
    "debug_ingest_url": lambda: "http://127.0.0.1:7242/ingest/39510544-183f-44b0-ba0d-e5d8993ed0e5",
    "oracle_db_dsn": lambda: "",
    "oracle_db_user": lambda: "",
    "oracle_wallet_path": lambda: "",
    "system_prompt": lambda: "",
}


async def get_app_settings() -> dict[str, str]:
    """Read app settings from DB; fallback to env/defaults for missing keys. Updates in-memory cache."""
    global _settings_cache
    async with _cache_lock:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(AppSetting).where(AppSetting.key.in_(KEYS)))
            rows = {r.key: (r.value or "") for r in result.scalars().all()}
        out: dict[str, str] = {}
        for k in KEYS:
            raw = rows.get(k)
            val = (raw or "").strip()
            if k in ("debug_log_path", "debug_ingest_url"):
                out[k] = val  # empty = disabled; seed/backfill populates defaults
            elif k.startswith("oracle_nl2sql_") or k.startswith("oracle_db_") or k == "system_prompt":
                out[k] = val  # no default; empty = not configured
            else:
                out[k] = val or DEFAULTS[k]()
        _settings_cache = dict(out)
        return out


async def set_app_settings(data: dict[str, Any]) -> None:
    """Upsert given keys into app_settings table. Accepts api_base_url, cors_origins, debug_log_path, debug_ingest_url, oracle_nl2sql_*."""
    global _settings_cache
    allowed = set(KEYS)
    to_set = {k: (v if isinstance(v, str) else str(v)) for k, v in data.items() if k in allowed}
    if not to_set:
        return
    async with AsyncSessionLocal() as session:
        for key, value in to_set.items():
            row = (await session.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
            if row is None:
                session.add(AppSetting(key=key, value=value))
            else:
                row.value = value
        await session.commit()
    async with _cache_lock:
        _settings_cache.update(to_set)


def get_debug_log_path_sync() -> str:
    """Return cached debug_log_path (no DB access). Empty string = disabled. Prime cache at startup via get_app_settings()."""
    return _settings_cache.get("debug_log_path") or ""
