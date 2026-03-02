import logging
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import router as api_router
from app.database import init_db
from app.app_settings import get_app_settings
from app.mcp_manager import mcp_manager
from app.agent import ensure_graph_uses_current_tools
from app.tool_visibility import load_visibility_from_db
from app.tool_description import load_description_overrides_from_db


# Ensure app and mcp_manager logs go to stdout (visible in uvicorn terminal)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
for name in ("app", "app.mcp_manager", "app.api"):
    logging.getLogger(name).setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and refresh MCP tools on startup."""
    await init_db()
    await get_app_settings()  # Prime in-memory cache for debug_log_path etc.
    await load_visibility_from_db()
    await load_description_overrides_from_db()

    try:
        await mcp_manager.refresh_tools()
        ensure_graph_uses_current_tools()
        from app.mcp_manager import ACTIVE_TOOLS
        print(f"[STARTUP] Loaded {len(ACTIVE_TOOLS)} MCP tools; graph recreated")
    except Exception as e:
        print(f"[STARTUP] Warning: Failed to refresh MCP tools: {e}")
    yield
    # Shutdown: clear MCP state without calling context __aexit__ from this task,
    # to avoid "Attempted to exit cancel scope in a different task" (MCP/anyio).
    logger = logging.getLogger("app.main")
    try:
        await mcp_manager.close_all_sessions(graceful=False)
        logger.info("MCP connections cleared on shutdown")
    except Exception as e:
        logger.warning("Error clearing MCP connections on shutdown: %s", e)


app = FastAPI(
    title="AI Agent Backend", 
    version="0.1.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # Next.js default port
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API router
app.include_router(api_router)


@app.get("/")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "message": "AI Agent Backend is running"}
