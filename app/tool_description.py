"""
In-memory cache and DB helpers for MCP tool description overrides.
Tool identity is the sanitized full name (e.g. SalesDB_connect).
"""
from typing import Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, MCPToolDescriptionOverride


# In-memory cache: tool_name (sanitized full name) -> description (str).
_description_override_cache: Dict[str, str] = {}


async def load_description_overrides_from_db() -> None:
    """Load tool description overrides from database into in-memory cache."""
    global _description_override_cache
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(MCPToolDescriptionOverride))
        rows = result.scalars().all()
    _description_override_cache = {row.tool_name: row.description for row in rows}


def get_tool_description_override(tool_name: str) -> str | None:
    """Return the overridden description for the tool, or None if not set."""
    return _description_override_cache.get(tool_name)


def set_description_override_cached(tool_name: str, description: str) -> None:
    """Update in-memory cache only (call after DB update)."""
    _description_override_cache[tool_name] = description


async def set_tool_description_override(tool_name: str, description: str, db: AsyncSession) -> None:
    """Upsert tool description override in DB and update in-memory cache."""
    obj = MCPToolDescriptionOverride(tool_name=tool_name, description=description)
    await db.merge(obj)
    await db.commit()
    set_description_override_cached(tool_name, description)


async def delete_tool_description_override(tool_name: str, db: AsyncSession) -> None:
    """Delete tool description override from DB and remove from cache."""
    from sqlalchemy import delete
    await db.execute(delete(MCPToolDescriptionOverride).where(MCPToolDescriptionOverride.tool_name == tool_name))
    await db.commit()
    _description_override_cache.pop(tool_name, None)
