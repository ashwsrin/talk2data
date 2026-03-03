from typing import List, Dict, Any, Optional, Tuple
from queue import Queue
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Response, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select, delete, desc, func
from sqlalchemy.ext.asyncio import AsyncSession
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage, ToolMessage

import json
import base64
import logging
import re

from app.config import settings
from app.database import get_db, MCPServer, ChatMessage, Conversation, ChatMessageAttachment
from app.mcp_manager import mcp_manager, _sanitize_tool_name_for_oci
from app.tool_visibility import is_tool_enabled, set_tool_visibility
from app.tool_description import (
    get_tool_description_override,
    set_tool_description_override,
    delete_tool_description_override,
)
from app.agent import (
    run_graph_streaming,
    STREAM_GRAPH_DONE,
    ensure_graph_uses_current_tools,
    get_graph_mermaid,
    _collect_function_call_items,
    MAX_TOOL_INVOCATIONS,
    build_system_prompt,
    DEFAULT_SYSTEM_PROMPT_TEMPLATE,
)
from app.services.title_generator import generate_and_save_conversation_title
from app.app_settings import get_app_settings, set_app_settings
from datetime import datetime, timedelta


def _safe_json_default(obj: Any) -> Any:
    """
    Fallback serializer for json.dumps used on execution metadata.

    Some providers (e.g. Gemini / DeepWiki) may return rich Python objects
    like Citation instances inside response metadata. Those are not directly
    JSON-serializable, so we defensively convert them here.
    """
    # Prefer explicit "dict-like" representations if available
    for attr in ("model_dump", "dict", "to_dict"):
        if hasattr(obj, attr) and callable(getattr(obj, attr)):
            try:
                return getattr(obj, attr)()
            except Exception:
                pass
    # As a last resort, store the string representation
    return str(obj)


router = APIRouter(prefix="/api", tags=["api"])


# --- App settings (GET/PUT) ---
class AppSettingsUpdate(BaseModel):
    system_prompt: Optional[str] = None


@router.get("/settings")
async def get_settings():
    """Return current app settings. DB-stored: system_prompt. Read-only from env: database_url, oci_*, cors_origins, backend_url."""
    data = await get_app_settings()
    
    # Read-only env values
    if settings.oracle_db_dsn:
        data["database_url"] = f"oracle+oracledb://{settings.oracle_db_user}:***@{settings.oracle_db_dsn}"
    else:
        data["database_url"] = settings.database_url
    data["oci_config_file"] = settings.oci_config_file
    data["oci_profile"] = settings.oci_profile
    data["backend_url"] = settings.backend_url
    data["cors_origins"] = settings.cors_origins
    # Return the default system prompt template when not yet configured
    if not data.get("system_prompt"):
        data["system_prompt"] = DEFAULT_SYSTEM_PROMPT_TEMPLATE.strip()
    return data


@router.put("/settings")
async def put_settings(body: AppSettingsUpdate):
    """Update app settings. Currently only system_prompt is writable."""
    data = body.model_dump(exclude_unset=True)
    if "system_prompt" in data:
        data["system_prompt"] = (data["system_prompt"] or "").strip()
    await set_app_settings(data)
    return await get_app_settings()





ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PDF_MIME_TYPE = "application/pdf"
MAX_ATTACHMENTS_PER_MESSAGE = 6  # Combined images + PDFs
# Keep this conservative to protect DB and request size. (UI already limits count; backend enforces size.)
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5MB
MAX_PDF_BYTES = 10 * 1024 * 1024  # 10MB
# OCI input_file file_url has a length limit; above this we fall back to text extraction
MAX_PDF_BYTES_FOR_DATA_URL = 2 * 1024 * 1024  # 2MB


def _mime_to_ext(mime_type: str | None) -> str:
    m = (mime_type or "").lower().strip()
    if m == "image/jpeg":
        return "jpg"
    if m == "image/png":
        return "png"
    if m == "image/webp":
        return "webp"
    if m == "image/gif":
        return "gif"
    return "bin"


def _ext_from_data_url(data_url: str) -> str:
    """
    Best-effort extension from `data:image/<type>;base64,...`.
    Falls back to bin when parsing fails.
    """
    if not isinstance(data_url, str):
        return "bin"
    s = data_url.strip()
    if not s.startswith("data:"):
        return "bin"
    header = s.split(",", 1)[0]
    mime = header[5:].split(";", 1)[0].strip()
    return _mime_to_ext(mime)


def _replace_data_image_urls_with_filenames(text: str) -> str:
    """
    Replace any `data:image/...` data URLs inside an arbitrary string with
    `[Image: image-<n>.<ext>]` placeholders.
    """
    if not isinstance(text, str) or "data:image/" not in text:
        return text

    # Match data URLs (stop at first whitespace or quote)
    pattern = re.compile(r"data:image/[^\\s\"']+")
    counter = {"n": 0}

    def repl(match: re.Match) -> str:
        counter["n"] += 1
        ext = _ext_from_data_url(match.group(0))
        return f"[Image: image-{counter['n']}.{ext}]"

    return pattern.sub(repl, text)


def _parse_image_data_url(data_url: str) -> Tuple[str, bytes]:
    """
    Parse a data URL like: data:image/png;base64,AAAA...
    Returns (mime_type, decoded_bytes). Raises HTTPException on invalid input.
    """
    if not isinstance(data_url, str) or not data_url.strip():
        raise HTTPException(status_code=400, detail="Invalid image data_url")
    s = data_url.strip()
    if not s.startswith("data:"):
        raise HTTPException(status_code=400, detail="Invalid image data_url (must start with data:)")
    try:
        header, b64 = s.split(",", 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid image data_url (missing comma)")
    header_lower = header.lower()
    if ";base64" not in header_lower:
        raise HTTPException(status_code=400, detail="Invalid image data_url (must be base64)")
    # header: data:<mime>;base64
    mime_part = header[5:].split(";", 1)[0].strip()
    if mime_part not in ALLOWED_IMAGE_MIME_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported image type: {mime_part}")
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image base64 payload")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail=f"Image too large (max {MAX_IMAGE_BYTES} bytes)")
    return mime_part, data


def _parse_pdf_data_url(data_url: str) -> Tuple[str, bytes]:
    """
    Parse a data URL like: data:application/pdf;base64,AAAA...
    Returns (mime_type, decoded_bytes). Raises HTTPException on invalid input.
    """
    if not isinstance(data_url, str) or not data_url.strip():
        raise HTTPException(status_code=400, detail="Invalid PDF data_url")
    s = data_url.strip()
    if not s.startswith("data:"):
        raise HTTPException(status_code=400, detail="Invalid PDF data_url (must start with data:)")
    try:
        header, b64 = s.split(",", 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid PDF data_url (missing comma)")
    header_lower = header.lower()
    if ";base64" not in header_lower:
        raise HTTPException(status_code=400, detail="Invalid PDF data_url (must be base64)")
    mime_part = header[5:].split(";", 1)[0].strip()
    if mime_part != PDF_MIME_TYPE:
        raise HTTPException(status_code=400, detail=f"Expected {PDF_MIME_TYPE}, got {mime_part}")
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid PDF base64 payload")
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(status_code=400, detail=f"PDF too large (max {MAX_PDF_BYTES} bytes)")
    return mime_part, data


def _attachment_to_data_url(mime_type: str, data_bytes: bytes) -> str:
    """
    Convert stored attachment bytes to a data URL for use in image_url content parts.
    """
    b64 = base64.b64encode(data_bytes).decode("ascii")
    return f"data:{mime_type};base64,{b64}"


def _extract_text_from_pdf(data_bytes: bytes) -> str:
    """
    Extract text from PDF bytes. Returns empty string on failure.
    Used to send PDF content as text to OCI (data URLs are too long for input_file).
    """
    try:
        from pypdf import PdfReader  # type: ignore
        import io
        reader = PdfReader(io.BytesIO(data_bytes))
        parts = []
        for page in reader.pages:
            text = page.extract_text()
            if text and isinstance(text, str):
                parts.append(text.strip())
        return "\n\n".join(parts).strip() if parts else ""
    except (ImportError, Exception):
        return ""


def _make_thumbnail_bytes(mime_type: str, data: bytes) -> Optional[bytes]:
    """
    Best-effort thumbnail generator. Uses Pillow if available; otherwise returns None.
    """
    try:
        from PIL import Image  # type: ignore
        import io
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGBA") if im.mode not in ("RGB", "RGBA") else im
            max_size = (320, 320)
            im.thumbnail(max_size)
            out = io.BytesIO()
            # Prefer keeping original mime when possible
            if mime_type == "image/png":
                im.save(out, format="PNG", optimize=True)
                return out.getvalue()
            if mime_type == "image/webp":
                im.save(out, format="WEBP", quality=70, method=6)
                return out.getvalue()
            if mime_type == "image/gif":
                # GIF thumbnails can be tricky; fallback to PNG thumbnail
                im.save(out, format="PNG", optimize=True)
                return out.getvalue()
            # default jpeg
            im_rgb = im.convert("RGB")
            im_rgb.save(out, format="JPEG", quality=75, optimize=True)
            return out.getvalue()
    except Exception:
        return None


def _normalize_tool_call_id(tool_id: str) -> str:
    """Canonical form for tool call id so 'fc_xxx' and 'xxx' match."""
    if not tool_id:
        return ""
    s = str(tool_id).strip()
    if s.startswith("fc_"):
        return s[3:] if len(s) > 3 else s
    return s


def _tool_calls_from_state_messages(messages: List[BaseMessage]) -> List[Dict[str, Any]]:
    """Derive tool_calls list from graph state messages (AIMessage with tool_calls + ToolMessages)."""
    tool_calls: List[Dict[str, Any]] = []
    pending_calls: List[Dict[str, Any]] = []  # {id, canonical_id, name, args}
    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for i, tc in enumerate(msg.tool_calls):
                tc_id = tc.get("id", f"tc-{i}") if isinstance(tc, dict) else getattr(tc, "id", f"tc-{i}")
                name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                pending_calls.append({
                    "id": tc_id,
                    "canonical_id": _normalize_tool_call_id(tc_id),
                    "name": name,
                    "args": args,
                })
        elif isinstance(msg, ToolMessage):
            call_id = getattr(msg, "tool_call_id", None) or ""
            canonical_call_id = _normalize_tool_call_id(call_id)
            content = getattr(msg, "content", "") or ""
            extra = getattr(msg, "additional_kwargs", None) or {}
            invoked_at = extra.get("invoked_at")
            duration_ms = extra.get("duration_ms")
            matched = False
            for j, pc in enumerate(pending_calls):
                if pc["id"] == call_id or pc["canonical_id"] == canonical_call_id:
                    tool_calls.append({
                        "sequence": len(tool_calls) + 1,
                        "name": pc["name"],
                        "args": pc["args"],
                        "output": content,
                        "invoked_at": invoked_at,
                        "duration_ms": duration_ms,
                    })
                    pending_calls.pop(j)
                    matched = True
                    break
            if not matched and pending_calls:
                # Second pass: match by position (first unmatched ToolMessage -> first pending call)
                tool_calls.append({
                    "sequence": len(tool_calls) + 1,
                    "name": pending_calls[0]["name"],
                    "args": pending_calls[0]["args"],
                    "output": content,
                    "invoked_at": invoked_at,
                    "duration_ms": duration_ms,
                })
                pending_calls.pop(0)
    return tool_calls


def _tool_calls_from_raw_response(
    raw_tool_calls: List[Dict[str, Any]],
    messages: List[BaseMessage],
) -> List[Dict[str, Any]]:
    """Build tool_calls list from capture raw_response.tool_calls and match outputs to ToolMessages."""
    if not raw_tool_calls:
        return []
    tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
    used_tool_msg_indices: set = set()
    result: List[Dict[str, Any]] = []
    for seq, raw in enumerate(raw_tool_calls, start=1):
        name = raw.get("name") or ""
        raw_args = raw.get("arguments") or raw.get("args") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args if isinstance(raw_args, dict) else {})
        except json.JSONDecodeError:
            args = {}
        raw_id = raw.get("id") or ""
        canonical_raw = _normalize_tool_call_id(raw_id)
        output = ""
        invoked_at = None
        duration_ms = None
        for ti, tm in enumerate(tool_messages):
            if ti in used_tool_msg_indices:
                continue
            call_id = getattr(tm, "tool_call_id", None) or ""
            if _normalize_tool_call_id(call_id) == canonical_raw or call_id == raw_id:
                output = getattr(tm, "content", "") or ""
                extra = getattr(tm, "additional_kwargs", None) or {}
                invoked_at = extra.get("invoked_at")
                duration_ms = extra.get("duration_ms")
                used_tool_msg_indices.add(ti)
                break
        if not output and len(tool_messages) > 0:
            for ti in range(len(tool_messages)):
                if ti not in used_tool_msg_indices:
                    tm = tool_messages[ti]
                    output = getattr(tm, "content", "") or ""
                    extra = getattr(tm, "additional_kwargs", None) or {}
                    invoked_at = extra.get("invoked_at")
                    duration_ms = extra.get("duration_ms")
                    used_tool_msg_indices.add(ti)
                    break
        result.append({
            "sequence": seq,
            "name": name,
            "args": args,
            "output": output,
            "invoked_at": invoked_at,
            "duration_ms": duration_ms,
        })
    return result


def _tool_calls_from_raw_response_dict(raw_response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build tool_calls list from a raw_response dict (no messages available).
    Used when serving execution-details and meta.tool_calls is empty.
    Checks raw_response['tool_calls'] first (accumulated list from agent), then raw_response['output'].
    Returns list in execution order with sequence, name, args, output (output empty when no messages).
    """
    if not isinstance(raw_response, dict):
        return []
    # 1) Prefer explicit accumulated tool_calls from agent
    raw_tc = raw_response.get("tool_calls")
    if isinstance(raw_tc, list) and raw_tc:
        result: List[Dict[str, Any]] = []
        for seq, raw in enumerate(raw_tc, start=1):
            name = raw.get("name") or ""
            raw_args = raw.get("arguments") or raw.get("args") or "{}"
            try:
                args = (
                    json.loads(raw_args)
                    if isinstance(raw_args, str)
                    else (raw_args if isinstance(raw_args, dict) else {})
                )
            except json.JSONDecodeError:
                args = {}
            result.append({"sequence": seq, "name": name, "args": args, "output": ""})
        return result
    # 2) Fallback: collect function_call items from response.output
    output = raw_response.get("output")
    items = _collect_function_call_items(output)
    if not items:
        return []
    result = []
    for seq, item in enumerate(items, start=1):
        name = item.get("name") or ""
        args = item.get("args") or {}
        result.append({"sequence": seq, "name": name, "args": args, "output": ""})
    return result


def _serialize_messages_for_execution_metadata(messages: List[BaseMessage]) -> List[Dict[str, Any]]:
    """Convert LangChain messages to a list of plain dicts (role, content, optional tool_calls) for storage."""
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, list):
                # Multimodal content: replace images with filenames, keep text.
                parts_out: List[str] = []
                img_idx = 0
                for part in content:
                    if isinstance(part, dict):
                        ptype = part.get("type")
                        if ptype == "text":
                            txt = part.get("text")
                            if isinstance(txt, str) and txt.strip():
                                parts_out.append(txt)
                            continue
                        if ptype == "image_url":
                            img_idx += 1
                            url = None
                            image_url = part.get("image_url")
                            if isinstance(image_url, dict):
                                url = image_url.get("url")
                            ext = _ext_from_data_url(url) if isinstance(url, str) else "bin"
                            parts_out.append(f"[Image: image-{img_idx}.{ext}]")
                            continue
                        if ptype == "file_url":
                            file_name = part.get("file_name") or "document.pdf"
                            parts_out.append(f"[File: {file_name}]")
                            continue
                    # Unknown part type: stringify but still redact any data URLs inside
                    parts_out.append(_replace_data_image_urls_with_filenames(str(part)))
                out.append({"role": "user", "content": "\n".join(parts_out).strip()})
            else:
                out.append({"role": "user", "content": content if isinstance(content, str) else _replace_data_image_urls_with_filenames(str(content))})
        elif isinstance(msg, AIMessage):
            content = msg.content
            entry: Dict[str, Any] = {
                "role": "assistant",
                "content": content if isinstance(content, str) else (str(content) if content else ""),
            }
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls and isinstance(tool_calls, list):
                entry["tool_calls"] = [
                    {"name": (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")), "id": (tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", ""))}
                    for tc in tool_calls
                ]
            out.append(entry)
        else:
            # ToolMessage or other: store as assistant follow-up or skip; for simplicity store role+content
            role = getattr(msg, "type", "assistant")
            if hasattr(role, "lower"):
                role = "assistant" if role != "human" else "user"
            else:
                role = "assistant"
            content = getattr(msg, "content", "") or ""
            out.append({"role": role, "content": content if isinstance(content, str) else str(content)})
    return out


# Pydantic models for request/response
class ServerCreate(BaseModel):
    name: str
    transport_type: str = 'sse'  # 'sse', 'stdio', or 'streamable_http'
    url: str | None = None
    api_key: str | None = None
    command: str | None = None
    args: str | None = None  # JSON string for arguments array
    env_vars: str | None = None  # JSON string for environment variables dict
    cwd: str | None = None
    include_in_llm: bool = True
    system_instruction: str | None = None
    oauth2_access_token_url: str | None = None
    oauth2_client_id: str | None = None
    oauth2_client_secret: str | None = None
    oauth2_scope: str | None = None

    @field_validator('transport_type')
    @classmethod
    def validate_transport_type(cls, v):
        if v not in ['sse', 'stdio', 'streamable_http']:
            raise ValueError("transport_type must be 'sse', 'stdio', or 'streamable_http'")
        return v
    
    @field_validator('args', 'env_vars')
    @classmethod
    def validate_json_fields(cls, v, info):
        if v is None or v == '':
            return None
        try:
            parsed = json.loads(v)
            if info.field_name == 'args' and not isinstance(parsed, list):
                raise ValueError("args must be a JSON array")
            if info.field_name == 'env' and not isinstance(parsed, dict):
                raise ValueError("env must be a JSON object")
            return v  # Return original string, we'll parse it later
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {info.field_name}: {str(e)}")
    
    @model_validator(mode='after')
    def validate_transport_specific_fields(self):
        if self.transport_type in ('sse', 'streamable_http'):
            if not self.url:
                raise ValueError("url is required for SSE and streamable_http transport")
        elif self.transport_type == 'stdio':
            if not self.command:
                raise ValueError("command is required for Stdio transport")
        return self

    @model_validator(mode='after')
    def validate_oauth_fields(self):
        oauth_fields = [
            self.oauth2_access_token_url,
            self.oauth2_client_id,
            self.oauth2_client_secret,
            self.oauth2_scope,
        ]
        has_any = any(f and (f.strip() if isinstance(f, str) else True) for f in oauth_fields)
        if has_any:
            if not (self.oauth2_access_token_url and (self.oauth2_access_token_url or "").strip()):
                raise ValueError("oauth2_access_token_url is required when using OAuth 2.0")
            if not (self.oauth2_client_id and (self.oauth2_client_id or "").strip()):
                raise ValueError("oauth2_client_id is required when using OAuth 2.0")
            # oauth2_client_secret optional on update (leave blank to keep current)
        return self


class ServerResponse(BaseModel):
    id: int
    name: str
    transport_type: str
    url: str | None = None
    api_key: str | None = None
    command: str | None = None
    args: str | None = None
    env: str | None = None
    cwd: str | None = None
    is_active: bool
    include_in_llm: bool = True
    system_instruction: str | None = None
    oauth2_access_token_url: str | None = None
    oauth2_client_id: str | None = None
    oauth2_scope: str | None = None
    # oauth2_client_secret is intentionally omitted from response

    class Config:
        from_attributes = True


class ConversationCreate(BaseModel):
    title: str = "New Conversation"
    user_name: str = "User"


class ConversationResponse(BaseModel):
    id: int
    title: str
    user_name: str
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class ConversationUpdate(BaseModel):
    title: str


class MessageResponse(BaseModel):
    id: int
    role: str
    content: str
    created_at: str
    conversation_id: int
    attachments: Optional[List[Dict[str, Any]]] = None
    model_id: Optional[str] = None

    class Config:
        from_attributes = True


@router.get("/servers", response_model=List[ServerResponse])
async def get_servers(db: AsyncSession = Depends(get_db)):
    """Get all MCP servers."""
    result = await db.execute(select(MCPServer))
    servers = result.scalars().all()
    return servers


class ToolVisibilityUpdate(BaseModel):
    tool_name: str
    enabled: bool


@router.get("/servers/tools")
async def get_server_tools(db: AsyncSession = Depends(get_db)):
    """Get tools for all servers. Each tool includes full_name, enabled, description (display), and original_description."""
    try:
        all_tools = mcp_manager.get_all_server_tools()
        result = await db.execute(select(MCPServer))
        servers = result.scalars().all()
        include_by_server: Dict[str, bool] = {s.name: bool(getattr(s, "include_in_llm", True)) for s in servers}
        # Enrich each tool with full_name, enabled, description (override or original), original_description
        enriched: Dict[str, List[Dict[str, Any]]] = {}
        for server_name, tools_list in all_tools.items():
            server_include = include_by_server.get(server_name, True)
            enriched[server_name] = []
            for t in tools_list:
                full_name = _sanitize_tool_name_for_oci(server_name, t.get("name", ""))
                original_desc = t.get("description") or f"Tool from {server_name}"
                display_desc = get_tool_description_override(full_name) or original_desc
                tool_enabled = is_tool_enabled(full_name)
                enriched[server_name].append({
                    **t,
                    "full_name": full_name,
                    "enabled": tool_enabled,
                    "server_include_in_llm": server_include,
                    "effective_enabled": bool(server_include and tool_enabled),
                    "description": display_desc,
                    "original_description": original_desc,
                })
        return {
            "status": "success",
            "server_tools": enriched
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting server tools: {str(e)}")


@router.patch("/servers/tools/visibility")
async def patch_tool_visibility(body: ToolVisibilityUpdate, db: AsyncSession = Depends(get_db)):
    """Update whether a tool is visible to the LLM. Persisted in DB."""
    try:
        await set_tool_visibility(body.tool_name, body.enabled, db)
        return {"status": "success", "tool_name": body.tool_name, "enabled": body.enabled}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating tool visibility: {str(e)}")


class ToolDescriptionUpdate(BaseModel):
    tool_name: str
    description: str


@router.patch("/servers/tools/description")
async def patch_tool_description(body: ToolDescriptionUpdate, db: AsyncSession = Depends(get_db)):
    """Update tool description override. Persisted in DB; used in LLM payload."""
    try:
        await set_tool_description_override(body.tool_name, body.description, db)
        return {"status": "success", "tool_name": body.tool_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating tool description: {str(e)}")


@router.delete("/servers/tools/description/{tool_name:path}")
async def delete_tool_description(tool_name: str, db: AsyncSession = Depends(get_db)):
    """Remove tool description override; tool reverts to original MCP description."""
    try:
        await delete_tool_description_override(tool_name, db)
        return {"status": "success", "tool_name": tool_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting tool description override: {str(e)}")


@router.post("/servers", response_model=ServerResponse)
async def create_server(server: ServerCreate, db: AsyncSession = Depends(get_db)):
    """Create a new MCP server."""
    try:
        print(f"[API] Creating server: name={server.name}, transport_type={server.transport_type}")
        
        # Check if server with same name already exists
        existing = await db.execute(
            select(MCPServer).where(MCPServer.name == server.name)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Server with this name already exists")
        
        # Normalize API key: empty string becomes None
        api_key = server.api_key if server.api_key and server.api_key.strip() else None
        
        # Normalize JSON fields: empty strings become None
        args = server.args if server.args and server.args.strip() else None
        env_vars = server.env_vars if server.env_vars and server.env_vars.strip() else None
        
        # Normalize system_instruction: empty string becomes None
        system_instruction = (server.system_instruction or "").strip() or None
        oauth2_access_token_url = (server.oauth2_access_token_url or "").strip() or None
        oauth2_client_id = (server.oauth2_client_id or "").strip() or None
        oauth2_client_secret = (server.oauth2_client_secret or "").strip() or None
        oauth2_scope = (server.oauth2_scope or "").strip() or None

        # Create new server
        db_server = MCPServer(
            name=server.name,
            transport_type=server.transport_type,
            url=server.url,
            api_key=api_key,
            command=server.command,
            args=args,
            env_vars=env_vars,
            cwd=server.cwd if server.cwd and server.cwd.strip() else None,
            is_active=True,
            include_in_llm=getattr(server, "include_in_llm", True),
            system_instruction=system_instruction,
            oauth2_access_token_url=oauth2_access_token_url,
            oauth2_client_id=oauth2_client_id,
            oauth2_client_secret=oauth2_client_secret,
            oauth2_scope=oauth2_scope,
        )
        db.add(db_server)
        await db.commit()
        await db.refresh(db_server)
        
        print(f"[API] Server created successfully: id={db_server.id}, name={db_server.name}")
        return db_server
    except HTTPException:
        raise
    except Exception as e:
        print(f"[API] Error creating server: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error creating server: {str(e)}")


@router.put("/servers/{server_id}", response_model=ServerResponse)
async def update_server(server_id: int, server: ServerCreate, db: AsyncSession = Depends(get_db)):
    """Update an existing MCP server."""
    # Get the server
    result = await db.execute(select(MCPServer).where(MCPServer.id == server_id))
    db_server = result.scalar_one_or_none()
    
    if not db_server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    # Check if another server with the same name exists (excluding current server)
    existing = await db.execute(
        select(MCPServer).where(MCPServer.name == server.name, MCPServer.id != server_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Server with this name already exists")
    
    # Normalize API key: empty string becomes None
    api_key = server.api_key if server.api_key and server.api_key.strip() else None
    
    # Normalize JSON fields: empty strings become None
    args = server.args if server.args and server.args.strip() else None
    env = server.env if server.env and server.env.strip() else None

    # OAuth fields: update only when provided; leave client_secret unchanged when blank (keep current)
    db_server.oauth2_access_token_url = (server.oauth2_access_token_url or "").strip() or None
    db_server.oauth2_client_id = (server.oauth2_client_id or "").strip() or None
    if server.oauth2_client_secret is not None and (server.oauth2_client_secret or "").strip():
        db_server.oauth2_client_secret = (server.oauth2_client_secret or "").strip()
    elif not (server.oauth2_access_token_url or "").strip() and not (server.oauth2_client_id or "").strip():
        db_server.oauth2_client_secret = None  # Clear when OAuth is disabled
    db_server.oauth2_scope = (server.oauth2_scope or "").strip() or None

    # Update server
    db_server.name = server.name
    db_server.transport_type = server.transport_type
    db_server.url = server.url
    db_server.api_key = api_key
    db_server.command = server.command
    db_server.args = args
    db_server.env = env
    db_server.cwd = server.cwd if server.cwd and server.cwd.strip() else None
    db_server.include_in_llm = getattr(server, "include_in_llm", True)
    db_server.system_instruction = (server.system_instruction or "").strip() or None

    await db.commit()
    await db.refresh(db_server)
    
    return db_server


@router.delete("/servers/{server_id}")
async def delete_server(server_id: int, db: AsyncSession = Depends(get_db)):
    """Delete an MCP server."""
    result = await db.execute(select(MCPServer).where(MCPServer.id == server_id))
    db_server = result.scalar_one_or_none()
    
    if not db_server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    await db.execute(delete(MCPServer).where(MCPServer.id == server_id))
    await db.commit()
    
    return {"status": "success", "message": "Server deleted successfully"}


@router.post("/servers/refresh")
async def refresh_tools():
    """Refresh tools from all active MCP servers."""
    try:
        await mcp_manager.refresh_tools()
        from app.mcp_manager import ACTIVE_TOOLS
        return {
            "status": "success",
            "message": f"Refreshed tools from {len(mcp_manager._sessions)} servers",
            "tools_count": len(ACTIVE_TOOLS)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error refreshing tools: {str(e)}")


@router.get("/servers/connection-status")
async def get_connection_status():
    """Get connection status for all MCP servers."""
    try:
        statuses = mcp_manager.get_all_connection_statuses()
        return {
            "status": "success",
            "connections": statuses
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting connection status: {str(e)}")


@router.get("/servers/{server_name}/connection-status")
async def get_server_connection_status(server_name: str):
    """Get connection status for a specific MCP server."""
    try:
        status = mcp_manager.get_connection_status(server_name)
        if not status.get('has_session'):
            raise HTTPException(status_code=404, detail=f"Server {server_name} not found")
        return {
            "status": "success",
            "connection": status
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting connection status: {str(e)}")


@router.post("/conversations", response_model=ConversationResponse)
async def create_conversation(
    conversation: ConversationCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a new conversation."""
    db_conversation = Conversation(
        title=conversation.title,
        user_name=conversation.user_name,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(db_conversation)
    await db.commit()
    await db.refresh(db_conversation)
    
    return {
        "id": db_conversation.id,
        "title": db_conversation.title,
        "user_name": db_conversation.user_name,
        "created_at": db_conversation.created_at.isoformat(),
        "updated_at": db_conversation.updated_at.isoformat(),
    }


@router.get("/conversations", response_model=List[ConversationResponse])
async def get_conversations(db: AsyncSession = Depends(get_db)):
    """Get all conversations, ordered by updated_at (most recent first)."""
    result = await db.execute(
        select(Conversation)
        .order_by(desc(Conversation.updated_at))
    )
    conversations = result.scalars().all()
    
    return [
        {
            "id": conv.id,
            "title": conv.title,
            "user_name": conv.user_name,
            "created_at": conv.created_at.isoformat(),
            "updated_at": conv.updated_at.isoformat(),
        }
        for conv in conversations
    ]


@router.post("/conversations/cleanup")
async def cleanup_old_conversations(
    older_than_days: int = 7,
    use_created_at: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Delete conversations (and their messages and attachments) older than the given number of days."""
    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    column = Conversation.created_at if use_created_at else Conversation.updated_at
    result = await db.execute(select(Conversation).where(column < cutoff))
    conversations = result.scalars().all()
    for conversation in conversations:
        await db.delete(conversation)
    await db.commit()
    return {"deleted_count": len(conversations)}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Delete a conversation and all its messages."""
    result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    await db.delete(conversation)
    await db.commit()
    return {"message": "Conversation deleted"}


@router.patch("/conversations/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    conversation_id: int,
    body: ConversationUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update a conversation's title."""
    result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation.title = body.title
    conversation.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(conversation)
    return {
        "id": conversation.id,
        "title": conversation.title,
        "user_name": conversation.user_name,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
    }


@router.get("/conversations/{conversation_id}/messages", response_model=List[MessageResponse])
async def get_conversation_messages(
    conversation_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get all messages for a specific conversation."""
    # Verify conversation exists
    conv_result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conversation = conv_result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # Get messages for this conversation
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.created_at)
    )
    messages = result.scalars().all()

    # Fetch attachments for all messages in one query, then group by message_id
    msg_ids = [m.id for m in messages]
    attachments_by_msg: Dict[int, List[Dict[str, Any]]] = {}
    if msg_ids:
        base_url = (settings.backend_url or "").strip().rstrip("/") or "http://localhost:8001"
        att_result = await db.execute(
            select(ChatMessageAttachment).where(ChatMessageAttachment.message_id.in_(msg_ids))
        )
        atts = att_result.scalars().all()
        for a in atts:
            att_dict: Dict[str, Any] = {
                "id": a.id,
                "mime_type": a.mime_type,
                "full_url": f"{base_url}/api/attachments/{a.id}",
            }
            if getattr(a, "file_name", None):
                att_dict["file_name"] = a.file_name
            if a.mime_type in ALLOWED_IMAGE_MIME_TYPES:
                att_dict["thumbnail_url"] = f"{base_url}/api/attachments/{a.id}/thumbnail"
            attachments_by_msg.setdefault(a.message_id, []).append(att_dict)
    
    out: List[Dict[str, Any]] = []
    for msg in messages:
        item: Dict[str, Any] = {
            "id": msg.id,
            "role": msg.role,
            "content": msg.content,
            "created_at": msg.created_at.isoformat(),
            "conversation_id": msg.conversation_id,
            "attachments": attachments_by_msg.get(msg.id) or [],
        }
        if msg.execution_metadata and msg.role == "assistant":
            try:
                meta = json.loads(msg.execution_metadata)
                if isinstance(meta, dict) and meta.get("model_id"):
                    item["model_id"] = meta["model_id"]
            except (TypeError, ValueError):
                pass
        out.append(item)
    return out


@router.get("/attachments/{attachment_id}")
async def get_attachment_full(
    attachment_id: int,
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(ChatMessageAttachment).where(ChatMessageAttachment.id == attachment_id))
    att = res.scalar_one_or_none()
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return Response(content=att.data_bytes, media_type=att.mime_type)


@router.get("/attachments/{attachment_id}/thumbnail")
async def get_attachment_thumbnail(
    attachment_id: int,
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(ChatMessageAttachment).where(ChatMessageAttachment.id == attachment_id))
    att = res.scalar_one_or_none()
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if att.mime_type not in ALLOWED_IMAGE_MIME_TYPES:
        raise HTTPException(status_code=404, detail="No thumbnail for non-image attachment")
    content = att.thumbnail_bytes if att.thumbnail_bytes else att.data_bytes
    return Response(content=content, media_type=att.mime_type)


@router.get("/conversations/{conversation_id}/messages/{message_id}/execution-details")
async def get_message_execution_details(
    conversation_id: int,
    message_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get execution metadata (prompt, tool invocations) for an assistant message. On demand for popup."""
    conv_result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    if conv_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    msg_result = await db.execute(
        select(ChatMessage).where(
            ChatMessage.id == message_id,
            ChatMessage.conversation_id == conversation_id,
        )
    )
    msg = msg_result.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.role != "assistant":
        raise HTTPException(status_code=400, detail="Execution details are only available for assistant messages")
    if not msg.execution_metadata:
        raise HTTPException(status_code=404, detail="No execution details for this message")
    try:
        meta = json.loads(msg.execution_metadata)
    except (TypeError, ValueError):
        raise HTTPException(status_code=500, detail="Invalid execution metadata")
    # Defensive: sanitize any old rows that stored image data URLs in prompt_messages
    if isinstance(meta, dict) and isinstance(meta.get("prompt_messages"), list):
        for pm in meta["prompt_messages"]:
            if isinstance(pm, dict) and isinstance(pm.get("content"), str):
                pm["content"] = _replace_data_image_urls_with_filenames(pm["content"])
    # If tool_calls is empty but raw_response exists, derive tool_calls (in execution order) and persist
    if not meta.get("tool_calls") and isinstance(meta.get("raw_response"), dict):
        derived = _tool_calls_from_raw_response_dict(meta["raw_response"])
        if derived:
            meta["tool_calls"] = derived
            msg.execution_metadata = json.dumps(meta, default=_safe_json_default)
            await db.commit()
    return meta


@router.post("/chat")
async def chat_endpoint(request: dict, background_tasks: BackgroundTasks):
    """Chat endpoint that uses the agent with MCP tools and streams simple text."""
    async def generate_chat_responses():
        full_response = ""
        user_message_content = ""
        conversation_id = None
        is_first_message = False
        
        # Create a new database session for this request
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            yield_limit_message = False
            try:
                # Extract conversation_id and messages from request
                conversation_id = request.get("conversation_id")
                messages_data = request.get("messages", [])
                user_message_id: Optional[int] = None
                
                # If no conversation_id provided, create a new conversation
                if not conversation_id:
                    db_conversation = Conversation(
                        title="New Conversation",
                        user_name="User",
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow()
                    )
                    db.add(db_conversation)
                    await db.commit()
                    await db.refresh(db_conversation)
                    conversation_id = db_conversation.id
                    is_first_message = True
                else:
                    # Verify conversation exists and update its updated_at
                    conv_result = await db.execute(
                        select(Conversation).where(Conversation.id == conversation_id)
                    )
                    conversation = conv_result.scalar_one_or_none()
                    if conversation:
                        conversation.updated_at = datetime.utcnow()
                        await db.commit()
                    else:
                        yield "\n\nError: The conversation you are trying to reply to was not found (it may have been deleted). Please refresh the page or start a new conversation."
                        return

                # If conversation_id was provided by client, check whether this is the first saved message.
                # This covers the web app flow that creates conversations via POST /api/conversations.
                if not is_first_message and conversation_id:
                    count_result = await db.execute(
                        select(func.count(ChatMessage.id)).where(ChatMessage.conversation_id == conversation_id)
                    )
                    existing_count = int(count_result.scalar() or 0)
                    is_first_message = existing_count == 0
                
                # Save the last user message to database
                if messages_data:
                    last_message = messages_data[-1]
                    if last_message.get("role") == "user":
                        user_message_content = last_message.get("content", "")
                        # Save user message to database
                        user_msg = ChatMessage(
                            role="user",
                            content=user_message_content,
                            created_at=datetime.utcnow(),
                            conversation_id=conversation_id
                        )
                        db.add(user_msg)
                        await db.commit()
                        await db.refresh(user_msg)
                        user_message_id = user_msg.id

                        if is_first_message and user_message_content.strip() and conversation_id:
                            background_tasks.add_task(
                                generate_and_save_conversation_title,
                                int(conversation_id),
                                user_message_content,
                            )

                        # Persist images and PDFs on the last user message as attachments
                        images = last_message.get("images") or []
                        files = last_message.get("files") or []
                        if not isinstance(images, list):
                            images = []
                        if not isinstance(files, list):
                            files = []
                        total_attachments = len(images) + len(files)
                        if total_attachments > MAX_ATTACHMENTS_PER_MESSAGE:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Too many attachments (max {MAX_ATTACHMENTS_PER_MESSAGE} total)",
                            )
                        for img in images:
                            data_url = img.get("data_url") if isinstance(img, dict) else img
                            if not isinstance(data_url, str) or not data_url.strip():
                                continue
                            mime_type, data = _parse_image_data_url(data_url)
                            thumb = _make_thumbnail_bytes(mime_type, data)
                            db.add(
                                ChatMessageAttachment(
                                    message_id=user_msg.id,
                                    mime_type=mime_type,
                                    data_bytes=data,
                                    thumbnail_bytes=thumb,
                                    created_at=datetime.utcnow(),
                                )
                            )
                        for f in files:
                            data_url = f.get("data_url") if isinstance(f, dict) else f
                            file_name = (f.get("file_name") or "document.pdf") if isinstance(f, dict) else "document.pdf"
                            if not isinstance(data_url, str) or not data_url.strip():
                                continue
                            mime_type, data = _parse_pdf_data_url(data_url)
                            db.add(
                                ChatMessageAttachment(
                                    message_id=user_msg.id,
                                    mime_type=mime_type,
                                    data_bytes=data,
                                    thumbnail_bytes=None,
                                    file_name=file_name,
                                    created_at=datetime.utcnow(),
                                )
                            )
                        if images or files:
                            await db.commit()
                
                model_id = "openai.gpt-4o"
                model_image_input = True
                model_pdf_input = True

                # Build full conversation history for the LLM from the database so every user message
                # includes its image content (Base64) from attachments; UI continues to show filenames.
                # Skip image/PDF parts when the selected model does not support them (avoids provider errors e.g. ZDR).
                langchain_messages = []
                msg_result = await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == conversation_id)
                    .order_by(ChatMessage.created_at)
                )
                db_messages = msg_result.scalars().all()
                msg_ids = [m.id for m in db_messages]
                attachments_by_msg: Dict[int, List[Any]] = {}
                if msg_ids:
                    att_result = await db.execute(
                        select(ChatMessageAttachment).where(ChatMessageAttachment.message_id.in_(msg_ids))
                    )
                    atts = att_result.scalars().all()
                    for a in atts:
                        attachments_by_msg.setdefault(a.message_id, []).append(a)
                backend_url = (settings.backend_url or "").rstrip("/")
                for msg in db_messages:
                    if msg.role == "user":
                        content = msg.content or ""
                        atts = attachments_by_msg.get(msg.id) or []
                        if atts:
                            content_parts: List[dict] = [{"type": "text", "text": content}]
                            for a in atts:
                                if a.mime_type == PDF_MIME_TYPE:
                                    if not model_pdf_input:
                                        continue
                                    file_name = getattr(a, "file_name", None) or "document.pdf"
                                    if len(a.data_bytes) <= MAX_PDF_BYTES_FOR_DATA_URL:
                                        # Pass PDF as base64 data URL (same mechanism as images); OCI accepts up to length limit
                                        content_parts.append({"type": "file_url", "file_url": _attachment_to_data_url(a.mime_type, a.data_bytes), "file_name": file_name})
                                    else:
                                        # Large PDF: fall back to text extraction to avoid OCI "string too long"
                                        pdf_text = _extract_text_from_pdf(a.data_bytes)
                                        if pdf_text:
                                            content_parts.append({"type": "text", "text": f"Content of attached PDF ({file_name}):\n\n{pdf_text}"})
                                        else:
                                            # Send PDF as input_file (filename + file_data) instead of fallback text
                                            content_parts.append({"type": "file_url", "file_url": _attachment_to_data_url(a.mime_type, a.data_bytes), "file_name": file_name})
                                else:
                                    if not model_image_input:
                                        continue
                                    data_url = _attachment_to_data_url(a.mime_type, a.data_bytes)
                                    content_parts.append({"type": "image_url", "image_url": {"url": data_url}})
                            langchain_messages.append(HumanMessage(content=content_parts))
                        else:
                            langchain_messages.append(HumanMessage(content=content))
                    elif msg.role == "assistant":
                        langchain_messages.append(AIMessage(content=msg.content or ""))
                
                # 1. Refresh tools from all active MCP servers (mutates ACTIVE_TOOLS in place)
                await mcp_manager.refresh_tools()
                # 2. Recreate graph so chatbot + ToolNode use the updated tool list
                ensure_graph_uses_current_tools()
                
                # Build system prompt from MCP server settings (include_in_llm, system_instruction)
                mcp_result = await db.execute(select(MCPServer))
                servers = mcp_result.scalars().all()
                # Read user-configured custom system prompt from app settings
                app_settings_data = await get_app_settings()
                custom_system_prompt = app_settings_data.get("system_prompt", "")
                system_prompt = build_system_prompt(servers, template=custom_system_prompt)
                
                # Log tools passed to LLM for verification
                from app.mcp_manager import ACTIVE_TOOLS
                from app.agent import get_all_tools
                all_tools = get_all_tools()
                print(f"[CHAT] Tools passed to LLM: {len(all_tools)} total ({len(ACTIVE_TOOLS)} from MCP)")
                for i, t in enumerate(all_tools):
                    print(f"  [{i+1}] {t.name}")
                
                # Serialize prompt for execution metadata (provider-agnostic)
                prompt_messages = _serialize_messages_for_execution_metadata(langchain_messages)

                # 3. Run agent with queue-based streaming (tokens from OCI path; non-OCI yields nothing until done)
                web_search_enabled = bool(request.get("web_search_enabled", False))
                stream_queue: Queue = Queue()
                thread_id = f"chat_{conversation_id}_{user_message_id or 0}"
                stream_config = {"configurable": {"thread_id": thread_id}}
                loop = asyncio.get_running_loop()
                future = loop.run_in_executor(
                    None,
                    lambda: run_graph_streaming(
                        stream_queue,
                        langchain_messages,
                        main_loop=loop,
                        web_search_enabled=web_search_enabled,
                        system_prompt=system_prompt,
                        model_id=model_id,
                        stream_config=stream_config,
                    ),
                )
                while True:
                    item = await loop.run_in_executor(None, stream_queue.get)
                    if item is STREAM_GRAPH_DONE:
                        break

                    if item is not None and not isinstance(item, dict):
                        full_response += item
                        yield item
                last_state, capture = await future

                # If no tokens were streamed (e.g. non-OCI or empty reply), yield full content once from final state
                if not full_response and last_state and last_state.get("messages"):
                    msgs = last_state["messages"]
                    if msgs:
                        last = msgs[-1]
                        if isinstance(last, AIMessage):
                            content = getattr(last, "content", None) or ""
                            if isinstance(content, str) and content.strip():
                                full_response = content
                                yield content
                # If still no content (model returned empty), yield a fallback so the user always sees a response
                if not full_response or not full_response.strip():
                    fallback = (
                        "I couldn't generate a response for that request. Please try again or rephrase your question."
                    )
                    full_response = fallback
                    yield fallback

                # When MAX_TOOL_INVOCATIONS was hit, show user-friendly message and still save with execution_metadata.
                # We set full_response here but yield it only after saving, so the client's loadMessages() sees the message.
                yield_limit_message = False
                messages = last_state.get("messages") or [] if last_state else []
                tool_invocation_count = sum(1 for m in messages if isinstance(m, ToolMessage))
                if tool_invocation_count >= MAX_TOOL_INVOCATIONS and (not full_response or not full_response.strip()):
                    full_response = (
                        f"Maximum number of tool invocations ({MAX_TOOL_INVOCATIONS}) reached for this question. "
                        "Execution has been stopped. Use the execution details icon below to view the full trace of prompts, responses, and tool calls."
                    )
                    yield_limit_message = True

                # Derive tool_calls from final state messages (AIMessage + ToolMessage pairs)
                tool_calls = _tool_calls_from_state_messages(last_state.get("messages") or [])

                # Fallback: if derived list is empty but capture has raw_response.tool_calls, build from that
                if not tool_calls and isinstance(capture, dict):
                    raw_response = capture.get("raw_response") or {}
                    raw_tc = raw_response.get("tool_calls") if isinstance(raw_response, dict) else None
                    if raw_tc and isinstance(raw_tc, list):
                        tool_calls = _tool_calls_from_raw_response(
                            raw_tc,
                            last_state.get("messages") or [],
                        )
                # Second fallback: derive from raw_response dict (e.g. accumulated list or output) so we persist order
                if not tool_calls and isinstance(capture, dict) and isinstance(capture.get("raw_response"), dict):
                    tool_calls = _tool_calls_from_raw_response_dict(capture["raw_response"])

                # Save assistant response to database after streaming completes.
                # Always persist execution_metadata (at least prompt_messages, tool_calls, model_id)
                # so the execution details popup can show content instead of 404.
                meta: Dict[str, Any] = {"prompt_messages": prompt_messages, "tool_calls": tool_calls, "model_id": model_id}
                if isinstance(capture, dict):
                    if "full_prompt" in capture:
                        meta["full_prompt"] = capture["full_prompt"]
                    if "raw_response" in capture:
                        meta["raw_response"] = capture["raw_response"]
                try:
                    graph_mermaid = get_graph_mermaid()
                    if graph_mermaid:
                        meta["graph_mermaid"] = graph_mermaid
                except Exception:
                    pass
                execution_metadata_value = json.dumps(meta, default=_safe_json_default)
                if full_response and conversation_id:
                    assistant_msg = ChatMessage(
                        role="assistant",
                        content=full_response,
                        created_at=datetime.utcnow(),
                        conversation_id=conversation_id,
                        execution_metadata=execution_metadata_value,
                    )
                    db.add(assistant_msg)
                    # Update conversation's updated_at timestamp
                    conv_result = await db.execute(
                        select(Conversation).where(Conversation.id == conversation_id)
                    )
                    conversation = conv_result.scalar_one_or_none()
                    if conversation:
                        conversation.updated_at = datetime.utcnow()
                    await db.commit()

                # Yield limit message after save so the client sees it and loadMessages() will find it in DB
                if yield_limit_message and full_response:
                    yield full_response
                
            except Exception as e:
                import traceback
                logger = logging.getLogger("app.api")
                logger.exception("Chat request failed: %s", e)
                error_msg = f"Error in chat: {str(e)}\n{traceback.format_exc()}"
                # Send error as text so streaming client sees it
                yield f"\n\nError: {error_msg}"
                # Persist an assistant message with real error so user and Execution details see it
                if conversation_id:
                    fallback_content = (full_response or "").strip()
                    if not fallback_content:
                        fallback_content = (
                            "The request encountered an error. Please try again.\n\nError: "
                            + str(e)[:800]
                        )
                    execution_metadata_value = json.dumps({
                        "error": error_msg[:10000],
                        "error_type": type(e).__name__,
                    })
                    try:
                        assistant_msg = ChatMessage(
                            role="assistant",
                            content=fallback_content[:10000],
                            created_at=datetime.utcnow(),
                            conversation_id=conversation_id,
                            execution_metadata=execution_metadata_value,
                        )
                        db.add(assistant_msg)
                        conv_result = await db.execute(
                            select(Conversation).where(Conversation.id == conversation_id)
                        )
                        conv = conv_result.scalar_one_or_none()
                        if conv:
                            conv.updated_at = datetime.utcnow()
                        await db.commit()
                    except Exception:
                        pass
    
    return StreamingResponse(
        generate_chat_responses(),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )




@router.get("/messages")
async def get_messages(
    conversation_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db)
):
    """Get chat messages from database, optionally filtered by conversation_id."""
    query = select(ChatMessage)
    
    if conversation_id:
        query = query.where(ChatMessage.conversation_id == conversation_id)
    
    result = await db.execute(
        query
        .order_by(ChatMessage.created_at)
        .limit(limit)
        .offset(offset)
    )
    messages = result.scalars().all()
    
    return [
        {
            "id": msg.id,
            "role": msg.role,
            "content": msg.content,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
            "conversation_id": msg.conversation_id,
        }
        for msg in messages
    ]

