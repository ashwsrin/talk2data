"""
In-memory cache and DB helpers for MCP tool visibility (which tools are sent to the LLM).
Tool identity is the sanitized full name (e.g. SalesDB_connect).
"""
from typing import Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, MCPToolVisibility


# In-memory cache: tool_name (sanitized full name) -> enabled (bool). Default True if not present.
_visibility_cache: Dict[str, bool] = {}


async def load_visibility_from_db() -> None:
    """Load tool visibility from database into in-memory cache."""
    global _visibility_cache
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(MCPToolVisibility))
        rows = result.scalars().all()
    _visibility_cache = {row.tool_name: bool(row.enabled) for row in rows}


def is_tool_enabled(tool_name: str) -> bool:
    """Return True if the tool should be sent to the LLM. Unknown tools default to enabled."""
    return _visibility_cache.get(tool_name, True)


def set_visibility_cached(tool_name: str, enabled: bool) -> None:
    """Update in-memory cache only (call after DB update)."""
    _visibility_cache[tool_name] = enabled


async def set_tool_visibility(tool_name: str, enabled: bool, db: AsyncSession) -> None:
    """Upsert tool visibility in DB and update in-memory cache."""
    obj = MCPToolVisibility(tool_name=tool_name, enabled=enabled)
    await db.merge(obj)
    await db.commit()
    set_visibility_cached(tool_name, enabled)
