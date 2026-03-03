"""Load and persist app settings (system_prompt) from DB."""
from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, AppSetting

# In-memory cache: updated by get_app_settings(); read by sync getters.
_settings_cache: dict[str, str] = {}
_cache_lock = asyncio.Lock()

KEYS = (
    # Custom system prompt
    "system_prompt",
)

DEFAULTS = {
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
            out[k] = val  # empty = not configured; use default
        _settings_cache = dict(out)
        return out


async def set_app_settings(data: dict[str, Any]) -> None:
    """Upsert given keys into app_settings table. Accepts system_prompt."""
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
