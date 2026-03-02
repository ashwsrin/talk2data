import asyncio
import json
import os
import re
import time
from contextvars import ContextVar
from typing import List, Dict, Any, Annotated, Optional, Tuple
from datetime import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import httpx
from mcp.client.sse import sse_client
from mcp import ClientSession
try:
    from mcp.client.streamable_http import streamable_http_client
except ImportError:
    streamable_http_client = None  # type: ignore[assignment]
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, create_model

from app.database import AsyncSessionLocal, MCPServer
from app.tool_description import get_tool_description_override

logger = logging.getLogger(__name__)

# Main event loop for MCP tool calls when graph runs in executor thread (set by API before run_graph_streaming).
# Tool sync wrapper runs the async MCP call on this loop via run_coroutine_threadsafe so existing sessions are used.
_MAIN_LOOP: ContextVar[Optional[asyncio.AbstractEventLoop]] = ContextVar("mcp_main_loop", default=None)


def set_main_loop_for_tools(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Set the main event loop so sync tool wrapper can run MCP coroutines on it from the executor thread."""
    _MAIN_LOOP.set(loop)


# Global variable to store active tools
ACTIVE_TOOLS: List[BaseTool] = []

# Empty Pydantic model for tools with no parameters. Ensures provider-safe schema
# (type=object, properties={}) instead of inferred "any" from **kwargs.
# OCI/OpenAI reject "any"; empty object is valid.
EmptyToolInput = create_model("EmptyToolInput")


def _ensure_no_any_in_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively replace 'any' type with 'string' in a JSON schema.
    OCI/OpenAI reject 'any'; use 'string' as a safe fallback.
    """
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for k, v in schema.items():
        if k == "type":
            if v == "any":
                out[k] = "string"
            elif isinstance(v, list):
                out[k] = ["string" if t == "any" else t for t in v]
            else:
                out[k] = v
        elif k in ("properties", "items", "additionalProperties") and isinstance(v, dict):
            out[k] = {pk: _ensure_no_any_in_schema(pv) for pk, pv in v.items()}
        elif k in ("oneOf", "anyOf", "allOf") and isinstance(v, list):
            out[k] = [_ensure_no_any_in_schema(s) for s in v]
        elif isinstance(v, dict):
            out[k] = _ensure_no_any_in_schema(v)
        elif isinstance(v, list):
            out[k] = [_ensure_no_any_in_schema(s) if isinstance(s, dict) else s for s in v]
        else:
            out[k] = v
    return out


def _sanitize_tool_name_for_oci(server_name: str, tool_name: str) -> str:
    """
    Produce a tool name that OCI Generative AI accepts: only A-Za-z0-9_, no leading digit.
    """
    raw = f"{server_name}_{tool_name}"
    s = re.sub(r"[^A-Za-z0-9_]+", "_", raw)
    s = re.sub(r"_+", "_", s).strip("_") or "tool"
    if s[0].isdigit():
        s = "mcp_" + s
    return s


def _mcp_result_to_string(result: Any) -> str:
    """Convert MCP CallToolResult (content list + optional structuredContent) to a string."""
    if result is None:
        return ""
    
    # Check for error first
    if hasattr(result, "isError") and result.isError:
        parts = []
        if hasattr(result, "content") and result.content:
            content_text = _content_blocks_to_text(result.content)
            if content_text:
                parts.append(content_text)
        # Also check for error message in other attributes
        if hasattr(result, "error") and result.error:
            parts.append(str(result.error))
        if not parts:
            # Fallback: try to get any string representation
            parts.append(str(result))
        error_msg = " ".join(parts).strip()
        return f"Error: {error_msg}" if error_msg else "Error: Unknown error"
    
    # Extract text from content blocks
    text = ""
    if hasattr(result, "content") and result.content:
        text = _content_blocks_to_text(result.content)
    
    # Fallback to structured content
    if not text and hasattr(result, "structuredContent") and result.structuredContent:
        text = json.dumps(result.structuredContent)
    
    # Final fallback
    return text or str(result) or "(empty result)"


def _content_blocks_to_text(blocks: List[Any]) -> str:
    """Extract text from MCP content blocks (e.g. TextContent)."""
    out = []
    for b in blocks:
        if hasattr(b, "text"):
            out.append(b.text)
        elif isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text", ""))
        else:
            out.append(str(b))
    return " ".join(out).strip()


# OAuth 2.0 token cache: server_name -> (access_token, expires_at). Refresh 60s before expiry.
_OAUTH2_TOKEN_CACHE: Dict[str, Tuple[str, float]] = {}
_OAUTH2_CACHE_LOCK: asyncio.Lock = asyncio.Lock()
_OAUTH2_DEFAULT_EXPIRY_SECONDS = 300


def _is_oauth2_configured(server: MCPServer) -> bool:
    """Return True if server has OAuth 2.0 Client Credentials configured (all three required fields set)."""
    url = (getattr(server, "oauth2_access_token_url", None) or "").strip()
    client_id = (getattr(server, "oauth2_client_id", None) or "").strip()
    client_secret = (getattr(server, "oauth2_client_secret", None) or "").strip()
    return bool(url and client_id and client_secret)


async def _get_oauth2_bearer_token(server: MCPServer) -> str:
    """
    Obtain a Bearer token via OAuth 2.0 Client Credentials.
    Uses in-memory cache with expiry; refreshes 60 seconds before expiry.
    """
    server_name = server.name
    token_url = (server.oauth2_access_token_url or "").strip()
    client_id = (server.oauth2_client_id or "").strip()
    client_secret = (server.oauth2_client_secret or "").strip()
    scope = (getattr(server, "oauth2_scope", None) or "").strip()

    async with _OAUTH2_CACHE_LOCK:
        now = time.time()
        cached = _OAUTH2_TOKEN_CACHE.get(server_name)
        if cached:
            token, expires_at = cached
            if now < expires_at - 60:  # 60-second buffer
                return token
            # Expired or soon to expire; remove and fetch new
            _OAUTH2_TOKEN_CACHE.pop(server_name, None)

    body: Dict[str, str] = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scope:
        body["scope"] = scope

    async with httpx.AsyncClient() as client:
        response = await client.post(
            token_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30.0,
        )
    response.raise_for_status()
    data = response.json()
    access_token = data.get("access_token")
    if not access_token:
        raise ValueError("OAuth 2.0 token response missing access_token")
    expires_in = data.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expires_at = time.time() + float(expires_in)
    else:
        expires_at = time.time() + _OAUTH2_DEFAULT_EXPIRY_SECONDS

    async with _OAUTH2_CACHE_LOCK:
        _OAUTH2_TOKEN_CACHE[server_name] = (access_token, expires_at)

    return access_token


class MCPClientManager:
    """Manages connections to remote MCP servers and their tools."""
    
    def __init__(self):
        self._sessions: Dict[str, ClientSession] = {}
        self._server_info: Dict[str, MCPServer] = {}
        self._connections: Dict[str, Dict[str, Any]] = {}  # Store SSE connection context managers and streams
        self._server_tools: Dict[str, List[Dict[str, Any]]] = {}  # Store tools per server
        self._connection_tasks: Dict[str, asyncio.Task] = {}  # Background tasks to keep connections alive
        self._connection_states: Dict[str, str] = {}  # Track connection state: 'connected', 'disconnected', 'reconnecting'
        self._last_success: Dict[str, datetime] = {}  # Track last successful tool call timestamp

    def is_server_included_in_llm(self, server_name: str) -> bool:
        """Return True if tools from this server should be sent to the LLM. Default True."""
        server = self._server_info.get(server_name)
        if server is None:
            return True
        return bool(getattr(server, "include_in_llm", True))
    
    async def refresh_tools(self) -> None:
        """
        Refresh tools from all active MCP servers.
        Reads active servers from DB, connects via SSE, fetches tools,
        converts to LangChain BaseTool objects, and updates ACTIVE_TOOLS.
        
        IMPORTANT: We mutate ACTIVE_TOOLS in place (clear + append) so that all
        modules that import it (e.g. agent.get_all_tools) see the same updated list.
        Reassigning (ACTIVE_TOOLS = []) would break references in other modules.
        """
        global ACTIVE_TOOLS
        
        # Clear in place so all importers keep the same reference
        ACTIVE_TOOLS.clear()
        self._server_tools.clear()
        
        # Read active servers from database
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(MCPServer).where(MCPServer.is_active == True)
            )
            active_servers = result.scalars().all()
        
        if not active_servers:
            logger.info("No active MCP servers found in database")
            return
        
        # Connect to each server and fetch tools
        tasks = []
        for server in active_servers:
            # Check for Docker/Env var overrides for specific servers
            normalized_name = (server.name or "").upper().replace(" ", "").replace("_", "")
            if normalized_name == "NL2SQL" and os.environ.get("NL2SQL_MCP_URL"):
                override_url = os.environ["NL2SQL_MCP_URL"]
                logger.info(f"Using env override for {server.name} URL: {override_url}")
                server.url = override_url
                server.transport_type = "sse"
            elif normalized_name in ("AGENTICTOOLS", "AGENTIC") and os.environ.get("AGENTIC_MCP_URL"):
                override_url = os.environ["AGENTIC_MCP_URL"]
                logger.info(f"Using env override for {server.name} URL: {override_url}")
                server.url = override_url
                server.transport_type = "sse"

            tasks.append(self._fetch_tools_from_server(server))
        
        # Wait for all connections to complete
        await asyncio.gather(*tasks, return_exceptions=True)
        
        logger.info(f"Refreshed tools: {len(ACTIVE_TOOLS)} tools from {len(active_servers)} servers")
    
    async def _is_connection_healthy(self, server_name: str) -> bool:
        """
        Check if MCP connection is healthy and ready for tool calls.
        
        For stdio: Checks if streams are open and session exists
        For SSE: Checks if streams are open and session exists
        
        Returns:
            True if connection appears healthy, False otherwise
        """
        conn_data = self._connections.get(server_name)
        if not conn_data:
            logger.debug(f"Connection data not found for {server_name}")
            return False
        
        # Check if session exists
        session = self._sessions.get(server_name)
        if not session:
            logger.debug(f"Session not found for {server_name}")
            return False
        
        transport_type = conn_data.get('transport')
        write_stream = conn_data.get('write')
        read_stream = conn_data.get('read')
        
        # Check streams are not closed
        if not write_stream or not read_stream:
            logger.debug(f"Streams missing for {server_name}")
            return False
        
        # Check write stream
        if hasattr(write_stream, 'is_closed') and write_stream.is_closed():
            logger.debug(f"Write stream is closed for {server_name}")
            return False
        if hasattr(write_stream, 'closed') and write_stream.closed:
            logger.debug(f"Write stream is closed (attr) for {server_name}")
            return False
        
        # Check read stream
        if hasattr(read_stream, 'is_closed') and read_stream.is_closed():
            logger.debug(f"Read stream is closed for {server_name}")
            return False
        if hasattr(read_stream, 'closed') and read_stream.closed:
            logger.debug(f"Read stream is closed (attr) for {server_name}")
            return False
        
        # For stdio connections, we can't directly check subprocess, but we can
        # try a lightweight operation to see if the connection is responsive
        # This will be done in _validate_connection if needed
        
        return True
    
    async def _validate_connection(self, server_name: str) -> bool:
        """
        Validate connection by attempting a lightweight operation.
        This is more expensive than _is_connection_healthy but provides
        better validation that the connection is actually working.
        
        Returns:
            True if connection is validated, False otherwise
        """
        session = self._sessions.get(server_name)
        if not session:
            return False
        
        try:
            # Try to list tools as a lightweight validation
            # This will fail if the connection is broken
            await session.list_tools()
            return True
        except Exception as e:
            logger.debug(f"Connection validation failed for {server_name}: {e}")
            return False
    
    async def _clear_server_connection(self, server_name: str) -> None:
        """
        Remove and close a single server's session and connection so the next
        call will trigger a fresh connection. Use when the connection is dead
        (e.g. ClosedResourceError after SSE peer closed).
        """
        if server_name in self._connection_tasks:
            try:
                task = self._connection_tasks[server_name]
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                del self._connection_tasks[server_name]
            except Exception as e:
                logger.warning(f"Error cancelling connection task for {server_name}: {e}")
        if server_name in self._connections:
            try:
                conn_data = self._connections[server_name]
                if 'ctx' in conn_data and hasattr(conn_data['ctx'], '__aexit__'):
                    await conn_data['ctx'].__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error closing connection for {server_name}: {e}")
            finally:
                del self._connections[server_name]
        if server_name in self._sessions:
            try:
                session = self._sessions[server_name]
                if hasattr(session, '__aexit__'):
                    await session.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error closing session for {server_name}: {e}")
            finally:
                del self._sessions[server_name]
        # Clear OAuth token cache so next connect gets a fresh token
        async with _OAUTH2_CACHE_LOCK:
            _OAUTH2_TOKEN_CACHE.pop(server_name, None)
        self._connection_states[server_name] = 'disconnected'
    
    async def _fetch_tools_from_server(self, server: MCPServer) -> None:
        """Connect to a single MCP server and fetch its tools."""
        try:
            transport_type = getattr(server, 'transport_type', 'sse') or 'sse'
            
            # Skip reconnect if we already have a healthy connection (preserves stdio subprocess state,
            # e.g. Oracle DB connection in SQLcl). Just re-fetch tools from the existing session.
            if server.name in self._sessions and await self._is_connection_healthy(server.name):
                session = self._sessions[server.name]
                # Keep server info (e.g. include_in_llm) current from DB
                self._server_info[server.name] = server
                try:
                    tools_response = await session.list_tools()
                    server_tools_list = []
                    for mcp_tool in tools_response.tools:
                        tool_info = {
                            "name": mcp_tool.name,
                            "description": mcp_tool.description or f"Tool from {server.name}",
                            "inputSchema": mcp_tool.inputSchema if hasattr(mcp_tool, 'inputSchema') else None,
                        }
                        server_tools_list.append(tool_info)
                        langchain_tool = self._convert_mcp_tool_to_langchain(mcp_tool, server)
                        if langchain_tool:
                            ACTIVE_TOOLS.append(langchain_tool)
                    self._server_tools[server.name] = server_tools_list
                    logger.info(f"Reused existing connection for {server.name}, fetched {len(tools_response.tools)} tools")
                    return
                except Exception as e:
                    logger.warning(f"Existing connection for {server.name} failed list_tools, will reconnect: {e}")
                    # Fall through to reconnect
            
            if transport_type == 'sse':
                logger.info(f"Connecting to SSE MCP server: {server.name} at {server.url}")
            elif transport_type == 'streamable_http':
                logger.info(f"Connecting to Streamable HTTP MCP server: {server.name} at {server.url}")
            else:
                raise ValueError(f"Unsupported transport type: {transport_type}")
            
            # Close existing connection if any
            if server.name in self._connections:
                try:
                    conn_data = self._connections[server.name]
                    if 'ctx' in conn_data and hasattr(conn_data['ctx'], '__aexit__'):
                        await conn_data['ctx'].__aexit__(None, None, None)
                    if 'http_client' in conn_data and conn_data['http_client'] is not None:
                        await conn_data['http_client'].__aexit__(None, None, None)
                    # Cancel background task if exists
                    if server.name in self._connection_tasks:
                        task = self._connection_tasks[server.name]
                        if not task.done():
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                        del self._connection_tasks[server.name]
                except Exception as e:
                    logger.warning(f"Error closing existing connection for {server.name}: {e}")
            
            # Close existing session if any
            if server.name in self._sessions:
                try:
                    old_session = self._sessions[server.name]
                    if hasattr(old_session, '__aexit__'):
                        await old_session.__aexit__(None, None, None)
                except Exception as e:
                    logger.warning(f"Error closing existing session for {server.name}: {e}")
            
            # Connect based on transport type
            if transport_type == 'sse':
                # SSE connection
                if not server.url:
                    raise ValueError(f"SSE server {server.name} requires a URL")
                
                # Prepare headers: OAuth 2.0 (when configured) or API key
                headers = {}
                if _is_oauth2_configured(server):
                    token = await _get_oauth2_bearer_token(server)
                    headers["Authorization"] = f"Bearer {token}"
                elif server.api_key:
                    headers["Authorization"] = f"Bearer {server.api_key}"
                
                # Connect using SSE client (with 30s timeout to avoid indefinite hang)
                connection_ctx = sse_client(server.url, headers=headers)
                try:
                    read, write = await asyncio.wait_for(
                        connection_ctx.__aenter__(), timeout=30.0
                    )
                except asyncio.TimeoutError:
                    raise TimeoutError(f"SSE connection to {server.name} timed out after 30s")
                
                # Store the connection context manager
                self._connections[server.name] = {
                    'ctx': connection_ctx,
                    'read': read,
                    'write': write,
                    'transport': 'sse'
                }
            elif transport_type == 'streamable_http':
                # Streamable HTTP connection (e.g. https://mcp.deepwiki.com/mcp, OIC)
                if not server.url:
                    raise ValueError(f"Streamable HTTP server {server.name} requires a URL")
                headers = {}
                if _is_oauth2_configured(server):
                    logger.info(f"[{server.name}] OAuth2 configured — fetching bearer token …")
                    try:
                        token = await _get_oauth2_bearer_token(server)
                        headers["Authorization"] = f"Bearer {token}"
                        logger.info(f"[{server.name}] OAuth2 token obtained (len={len(token)})")
                    except Exception as tok_err:
                        logger.error(f"[{server.name}] OAuth2 token fetch failed: {tok_err}", exc_info=True)
                        raise
                elif server.api_key:
                    headers["Authorization"] = f"Bearer {server.api_key}"

                # Create httpx client with generous timeout for cloud-hosted MCP servers
                http_client = httpx.AsyncClient(
                    headers=headers,
                    timeout=httpx.Timeout(60.0, connect=30.0),
                )
                await http_client.__aenter__()
                logger.info(f"[{server.name}] Connecting streamable HTTP to {server.url}")
                connection_ctx = streamable_http_client(server.url, http_client=http_client)
                read, write, _ = await connection_ctx.__aenter__()
                self._connections[server.name] = {
                    'ctx': connection_ctx,
                    'read': read,
                    'write': write,
                    'transport': 'streamable_http',
                    'http_client': http_client,
                }
            else:
                raise ValueError(f"Unsupported transport type: {transport_type}")
            
            # Create and initialize session using the streams (common for both transport types)
            session = ClientSession(read, write)
            await session.__aenter__()
            await session.initialize()
            
            # Store session and server info
            self._sessions[server.name] = session
            self._server_info[server.name] = server
            self._connection_states[server.name] = 'connected'
            self._last_success[server.name] = datetime.utcnow()
            
            # Fetch tools from the server
            tools_response = await session.list_tools()
            
            # Store tool info per server (for display purposes)
            server_tools_list = []
            for mcp_tool in tools_response.tools:
                # Store tool metadata
                tool_info = {
                    "name": mcp_tool.name,
                    "description": mcp_tool.description or f"Tool from {server.name}",
                    "inputSchema": mcp_tool.inputSchema if hasattr(mcp_tool, 'inputSchema') else None,
                }
                server_tools_list.append(tool_info)
                
                # Convert to LangChain BaseTool and add to ACTIVE_TOOLS
                langchain_tool = self._convert_mcp_tool_to_langchain(mcp_tool, server)
                if langchain_tool:
                    ACTIVE_TOOLS.append(langchain_tool)
            
            # Store tools for this server
            self._server_tools[server.name] = server_tools_list
            
            logger.info(f"Fetched {len(tools_response.tools)} tools from {server.name}")
            logger.info(f"Connection state for {server.name}: connected")
        
        except Exception as e:
            logger.error(f"Error connecting to MCP server {server.name}: {str(e)}", exc_info=True)
            self._connection_states[server.name] = 'disconnected'
    
    def _convert_mcp_tool_to_langchain(
        self,
        mcp_tool: Any,
        server: MCPServer,
    ) -> BaseTool | None:
        """
        Convert an MCP tool to a LangChain BaseTool object.

        Args:
            mcp_tool: The MCP tool object
            server: MCPServer model (for name and description override)

        Returns:
            A LangChain BaseTool object or None if conversion fails
        """
        try:
            server_name = server.name
            # Extract tool information (original MCP tool name)
            tool_name = mcp_tool.name
            sanitized_name = _sanitize_tool_name_for_oci(server_name, tool_name)
            tool_description = (
                get_tool_description_override(sanitized_name)
                or mcp_tool.description
                or f"Tool from {server_name}"
            )

            # Create input schema from MCP tool input schema. Never use None:
            # OCI/OpenAI reject inferred "any" from **kwargs; use EmptyToolInput when no params.
            if hasattr(mcp_tool, "inputSchema") and mcp_tool.inputSchema:
                input_model = self._convert_json_schema_to_pydantic(
                    mcp_tool.inputSchema
                )
            else:
                input_model = EmptyToolInput
            
            # Pass Pydantic model as args_schema so get_input_schema() returns it and the agent
            # sends correct parameters (properties, required) to OCI; a dict would be replaced
            # by LangChain's internal schema (empty properties).
            
            # Closure over server_name and tool_name: we always call the *original*
            # MCP tool name on the *correct* server, regardless of OCI-safe api_name.
            _server = server_name
            _mcp_tool = tool_name

            async def tool_func_async(**kwargs):
                session = self._sessions.get(_server)
                if not session:
                    # Session may have been cleared after a connection error; try to reconnect once
                    server_info = self._server_info.get(_server)
                    if server_info:
                        try:
                            await self._fetch_tools_from_server(server_info)
                            session = self._sessions.get(_server)
                        except Exception as e:
                            logger.warning(f"Reconnect on missing session failed for {_server}: {e}")
                    if not session:
                        logger.error("MCP session not found for server=%r (tool=%s)", _server, _mcp_tool)
                        self._connection_states[_server] = 'disconnected'
                        raise ValueError(f"MCP session not found for server: {_server!r}. Try refreshing servers.")
                
                # Check if connection is healthy using comprehensive health check
                is_healthy = await self._is_connection_healthy(_server)
                
                if not is_healthy:
                    logger.warning(f"MCP connection for {_server} is not healthy, attempting reconnection...")
                    self._connection_states[_server] = 'reconnecting'
                    
                    # Reconnect by refreshing tools for this server
                    # This will use the stored server_info which includes transport_type
                    server_info = self._server_info.get(_server)
                    if server_info:
                        try:
                            await self._fetch_tools_from_server(server_info)
                            session = self._sessions.get(_server)
                            if not session:
                                self._connection_states[_server] = 'disconnected'
                                raise ValueError(f"Failed to reconnect to server: {_server!r}")
                            
                            # Validate the reconnected connection
                            if not await self._validate_connection(_server):
                                logger.warning(f"Reconnected but validation failed for {_server}")
                                self._connection_states[_server] = 'disconnected'
                                raise ValueError(f"Reconnected but connection validation failed for server: {_server!r}")
                            
                            logger.info(f"Successfully reconnected to {_server}")
                            self._connection_states[_server] = 'connected'
                        except Exception as e:
                            logger.error(f"Reconnection failed for {_server}: {e}")
                            self._connection_states[_server] = 'disconnected'
                            raise ValueError(f"Failed to reconnect to server {_server!r}: {str(e)}")
                    else:
                        self._connection_states[_server] = 'disconnected'
                        raise ValueError(f"Server info not found for {_server!r}, cannot reconnect")
                else:
                    # Connection appears healthy, but for critical operations we can optionally validate
                    # Skip validation for now to avoid overhead, but log state
                    if self._connection_states.get(_server) != 'connected':
                        self._connection_states[_server] = 'connected'
                
                logger.info("Calling MCP tool %r on server %r with args %s", _mcp_tool, _server, kwargs)
                try:
                    # Log the exact call we're making
                    logger.debug("About to call session.call_tool(name=%r, arguments=%r)", _mcp_tool, kwargs)
                    result = await session.call_tool(_mcp_tool, arguments=kwargs or None)
                    logger.debug("call_tool returned: type=%r, isError=%r", type(result).__name__, getattr(result, "isError", None))
                    
                    # Check if result indicates an error
                    if hasattr(result, "isError") and result.isError:
                        error_msg = _mcp_result_to_string(result)
                        error_msg_lower = error_msg.lower()
                        
                        # Detect database connection errors
                        is_db_connection_error = any(phrase in error_msg_lower for phrase in [
                            "not connected to a database",
                            "not connected to database",
                            "not connected",
                            "no connection established",
                            "connection lost",
                            "database connection",
                            "ora-03113",
                            "ora-03114",
                            "connection terminated",
                            "unable to execute",
                            "cannot execute"
                        ])
                        
                        if is_db_connection_error:
                            logger.warning(f"Database connection error detected for {_server}: {error_msg}")
                            logger.info(f"Attempting immediate reconnection for {_server}")
                            self._connection_states[_server] = 'reconnecting'
                            
                            # Attempt immediate reconnection
                            server_info = self._server_info.get(_server)
                            if server_info:
                                try:
                                    await self._fetch_tools_from_server(server_info)
                                    session = self._sessions.get(_server)
                                    if session and await self._validate_connection(_server):
                                        logger.info(f"Successfully reconnected to {_server}, retrying tool call")
                                        self._connection_states[_server] = 'connected'
                                        
                                        # Retry the tool call once after reconnection
                                        try:
                                            retry_result = await session.call_tool(_mcp_tool, arguments=kwargs or None)
                                            if hasattr(retry_result, "isError") and retry_result.isError:
                                                retry_error = _mcp_result_to_string(retry_result)
                                                logger.error(f"Tool call still failed after reconnection: {retry_error}")
                                                return retry_error
                                            else:
                                                result_str = _mcp_result_to_string(retry_result)
                                                self._last_success[_server] = datetime.utcnow()
                                                logger.info(f"Tool call succeeded after reconnection: {_mcp_tool}")
                                                return result_str
                                        except Exception as retry_e:
                                            logger.error(f"Tool call failed after reconnection: {retry_e}")
                                            return f"Database connection was re-established, but tool call failed: {str(retry_e)}"
                                    else:
                                        logger.error(f"Reconnection validation failed for {_server}")
                                        self._connection_states[_server] = 'disconnected'
                                        return f"Database connection error: {error_msg}. Reconnection attempted but failed. Please try again."
                                except Exception as reconnect_e:
                                    logger.error(f"Reconnection failed for {_server}: {reconnect_e}")
                                    self._connection_states[_server] = 'disconnected'
                                    return f"Database connection error: {error_msg}. Reconnection failed: {str(reconnect_e)}. Please try again."
                            else:
                                logger.error(f"Server info not found for {_server}, cannot reconnect")
                                self._connection_states[_server] = 'disconnected'
                                return f"Database connection error: {error_msg}. Cannot reconnect (server info missing)."
                        else:
                            logger.error("MCP tool returned error: server=%r tool=%r error=%s", _server, _mcp_tool, error_msg)
                            return error_msg
                    
                    # Convert result to string
                    result_str = _mcp_result_to_string(result)
                    logger.info("MCP tool succeeded: server=%r tool=%r result=%r", _server, _mcp_tool, result_str[:200])
                    
                    # Update last success timestamp and connection state
                    self._last_success[_server] = datetime.utcnow()
                    self._connection_states[_server] = 'connected'
                    
                    return result_str
                except Exception as e:
                    # Capture full exception details including traceback
                    import traceback
                    exc_type = type(e).__name__
                    exc_msg = str(e) if str(e) else "(no message)"
                    exc_tb = traceback.format_exc()
                    error_details = f"{exc_type}: {exc_msg}"
                    
                    # Check if this is a connection-related exception (e.g. SSE peer closed, anyio ClosedResourceError)
                    exc_msg_lower = exc_msg.lower()
                    exc_type_name = type(e).__name__
                    is_connection_exception = (
                        exc_type_name == "ClosedResourceError"
                        or any(phrase in exc_msg_lower for phrase in [
                            "closed",
                            "connection",
                            "broken pipe",
                            "connection reset",
                            "connection aborted"
                        ])
                    )
                    
                    if is_connection_exception:
                        logger.warning(f"Connection exception for {_server}: {error_details}; clearing connection and attempting reconnection")
                        await self._clear_server_connection(_server)
                        server_info = self._server_info.get(_server)
                        if server_info:
                            try:
                                await self._fetch_tools_from_server(server_info)
                                session = self._sessions.get(_server)
                                if session and await self._validate_connection(_server):
                                    self._connection_states[_server] = 'connected'
                                    try:
                                        retry_result = await session.call_tool(_mcp_tool, arguments=kwargs or None)
                                        if hasattr(retry_result, "isError") and retry_result.isError:
                                            retry_err = _mcp_result_to_string(retry_result)
                                            return f"Tool error after reconnection ({_server}::{_mcp_tool}): {retry_err}"
                                        result_str = _mcp_result_to_string(retry_result)
                                        self._last_success[_server] = datetime.utcnow()
                                        logger.info(f"Tool call succeeded after reconnection: {_mcp_tool}")
                                        return result_str
                                    except Exception as retry_e:
                                        logger.error(f"Tool call failed after reconnection: {retry_e}")
                                        return f"Reconnected to {_server}, but tool call failed: {type(retry_e).__name__}: {retry_e}"
                                else:
                                    return f"Connection to {_server} was lost. Reconnection attempted but failed. Please try again or refresh servers."
                            except Exception as reconnect_e:
                                logger.error(f"Reconnection failed for {_server}: {reconnect_e}")
                                return f"Connection to {_server} was lost. Reconnection failed: {reconnect_e}. Please try again or refresh servers."
                        else:
                            return f"Connection to {_server} was lost. Cannot reconnect (server info missing). Please refresh servers."
                    else:
                        logger.exception("MCP call_tool raised exception: server=%r tool=%r error=%s", _server, _mcp_tool, error_details)
                        logger.debug("Full traceback:\n%s", exc_tb)
                    
                    return f"Tool error ({_server}::{_mcp_tool}): {error_details}"
            
            def tool_func_sync(**kwargs):
                """Synchronous wrapper: run async implementation on main loop from executor thread so MCP sessions are used."""
                main_loop = _MAIN_LOOP.get()
                try:
                    if main_loop is not None:
                        future = asyncio.run_coroutine_threadsafe(tool_func_async(**kwargs), main_loop)
                        out = future.result(timeout=120)
                    else:
                        out = asyncio.run(tool_func_async(**kwargs))
                    return out
                except Exception:
                    raise
            
            # OCI only allows tool names A-Za-z0-9_, no leading digit
            api_name = _sanitize_tool_name_for_oci(server_name, tool_name)
            tool = StructuredTool.from_function(
                func=tool_func_sync,
                coroutine=tool_func_async,
                name=api_name,
                description=tool_description,
                args_schema=input_model,
            )
            # Attach server identity so agent can apply server-level include gate.
            try:
                tool.metadata = {**(getattr(tool, "metadata", {}) or {}), "mcp_server": server_name}
            except Exception:
                pass
            
            return tool
        
        except Exception as e:
            logger.error(f"Error converting MCP tool {mcp_tool.name} to LangChain: {str(e)}", exc_info=True)
            return None
    
    def _normalize_json_schema_type(self, prop_schema: Dict[str, Any]) -> type:
        """
        Map JSON Schema type to a concrete Python type. Never use 'any'.
        OCI/OpenAI only allow string|number|integer|boolean|object|array.
        """
        prop_type = prop_schema.get("type")
        # oneOf/anyOf/allOf: use string to avoid "any" or unsupported unions
        if prop_schema.get("oneOf") or prop_schema.get("anyOf") or prop_schema.get("allOf"):
            return str
        # type is "any" or array containing "any"
        if prop_type == "any":
            return str
        if isinstance(prop_type, list):
            if "any" in prop_type or not prop_type:
                return str
            # e.g. ["string","null"] -> use first non-null
            for t in prop_type:
                if t != "null" and t in ("string", "integer", "number", "boolean", "array", "object"):
                    prop_type = t
                    break
            else:
                return str
        # missing or invalid type
        if prop_type not in ("string", "integer", "number", "boolean", "array", "object"):
            return str
        if prop_type == "string":
            return str
        if prop_type == "integer":
            return int
        if prop_type == "number":
            return float
        if prop_type == "boolean":
            return bool
        # array/object: use str to avoid complex schemas that may emit "any"
        return str

    def _convert_json_schema_to_pydantic(
        self, json_schema: Dict[str, Any]
    ) -> type[BaseModel]:
        """
        Convert JSON Schema to a Pydantic model for tool arguments.
        Always returns a model (EmptyToolInput when no properties) so we never
        pass args_schema=None; OCI/OpenAI reject inferred "any".
        """
        try:
            if not json_schema or not isinstance(json_schema, dict):
                return EmptyToolInput

            properties = json_schema.get("properties") or {}
            required = json_schema.get("required") or []
            if not isinstance(properties, dict):
                return EmptyToolInput

            field_definitions: Dict[str, Any] = {}
            for prop_name, prop_schema in properties.items():
                if not isinstance(prop_schema, dict):
                    continue
                # Skip properties that only have additionalProperties (no usable type)
                if "properties" not in prop_schema and "type" not in prop_schema and "oneOf" not in prop_schema and "anyOf" not in prop_schema and "allOf" not in prop_schema:
                    if prop_schema.get("additionalProperties") is not None:
                        continue
                python_type = self._normalize_json_schema_type(prop_schema)
                description = prop_schema.get("description") or ""
                is_required = prop_name in required
                if is_required:
                    field_definitions[prop_name] = (Annotated[python_type, Field(description=description)], ...)
                else:
                    field_definitions[prop_name] = (Annotated[python_type | None, Field(description=description)], None)

            if field_definitions:
                return create_model("ToolInput", **field_definitions)
            return EmptyToolInput

        except Exception as e:
            logger.error(f"Error converting JSON schema to Pydantic: {str(e)}", exc_info=True)
            return EmptyToolInput
    
    async def close_all_sessions(self, graceful: bool = True) -> None:
        """
        Close all active MCP sessions and connections.

        When graceful=True (e.g. reconnect), exit context managers so connections
        close cleanly. When graceful=False (process shutdown), only cancel tasks
        and clear state; do not call __aexit__ from this task, to avoid
        "Attempted to exit cancel scope in a different task" from MCP/anyio.
        """
        # Cancel background tasks first so they stop using connections
        for server_name in list(self._connection_tasks.keys()):
            try:
                task = self._connection_tasks[server_name]
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
            except Exception as e:
                logger.warning("Error cancelling connection task for %s: %s", server_name, e)
            finally:
                self._connection_tasks.pop(server_name, None)

        if graceful:
            # Close sessions and connection contexts (same task as opener would be ideal; used for reconnect)
            for server_name, session in list(self._sessions.items()):
                try:
                    await session.__aexit__(None, None, None)
                except Exception as e:
                    logger.error("Error closing session for %s: %s", server_name, e)

            for server_name, conn_data in list(self._connections.items()):
                try:
                    if "ctx" in conn_data and hasattr(conn_data["ctx"], "__aexit__"):
                        await conn_data["ctx"].__aexit__(None, None, None)
                except Exception as e:
                    logger.error("Error closing connection for %s: %s", server_name, e)

        self._sessions.clear()
        self._connections.clear()
        self._server_info.clear()
        self._server_tools.clear()
        self._connection_states.clear()
        self._last_success.clear()
    
    def get_server_tools(self, server_name: str) -> List[Dict[str, Any]]:
        """Get tools for a specific server."""
        return self._server_tools.get(server_name, [])
    
    def get_all_server_tools(self) -> Dict[str, List[Dict[str, Any]]]:
        """Get all tools organized by server."""
        return self._server_tools.copy()
    
    def get_connection_status(self, server_name: str) -> Dict[str, Any]:
        """
        Get connection status information for a server.
        
        Returns:
            Dict with connection state, last success time, and health status
        """
        state = self._connection_states.get(server_name, 'unknown')
        last_success = self._last_success.get(server_name)
        
        return {
            'server_name': server_name,
            'state': state,
            'last_success': last_success.isoformat() if last_success else None,
            'has_session': server_name in self._sessions,
            'has_connection': server_name in self._connections,
        }
    
    def get_all_connection_statuses(self) -> Dict[str, Dict[str, Any]]:
        """Get connection status for all servers."""
        return {
            server_name: self.get_connection_status(server_name)
            for server_name in self._server_info.keys()
        }


# Global instance
mcp_manager = MCPClientManager()
