from typing import Annotated, Sequence, TypedDict, Any, List, Optional, Dict, Tuple
from datetime import datetime
from contextvars import ContextVar
from queue import Queue
import os
import re
import json
import time

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage, SystemMessage
from langchain_core.tools import BaseTool, tool
from oci_openai import OciOpenAI, OciUserPrincipalAuth
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from app.tool_visibility import is_tool_enabled
from app.config import settings
from app.mcp_manager import ACTIVE_TOOLS, mcp_manager, _ensure_no_any_in_schema, set_main_loop_for_tools

# Context var for API to capture full prompt and raw response (set by API before run, read after)
_execution_capture_var: ContextVar[dict | None] = ContextVar("execution_capture", default=None)

# Context var for queue-based token streaming (set by run_graph_streaming in executor thread)
_stream_queue_var: ContextVar[Optional[Queue]] = ContextVar("stream_queue", default=None)

# Context var for web search tool: when True, include {"type": "web_search"} in OCI tools (set by API via run_graph_streaming)
_web_search_enabled_var: ContextVar[bool] = ContextVar("web_search_enabled", default=False)

# Context var for pre-built system prompt (set by API before run_graph_streaming so executor thread sees it)
_system_prompt_var: ContextVar[str | None] = ContextVar("system_prompt", default=None)

# Context var for per-request model id (set by API before run_graph_streaming so executor thread sees it)
_model_id_var: ContextVar[str | None] = ContextVar("model_id", default=None)

# Sentinel for "graph finished" so API stops reading from queue
STREAM_GRAPH_DONE = object()


# Maximum tool invocations allowed per user question. Change this value to adjust the limit.
MAX_TOOL_INVOCATIONS = 10

# Placeholder keys for ACTIVE SERVER INSTRUCTIONS (server name hint = key with _INSTRUCTIONS removed)
_SYSTEM_PROMPT_PLACEHOLDER_KEYS = (
    "NL2SQL_INSTRUCTIONS",
    "AGENTIC_TOOLS_INSTRUCTIONS",
    "DEEPWIKI_INSTRUCTIONS",
)


# The default system prompt template with placeholder markers.
# Users can override this entirely via Settings → Application → System Prompt.
# Supported placeholders (substituted at runtime):
#   {{TODAY_DATE}}               → e.g. "Monday, February 24, 2026"
#   {{CURRENT_YEAR}}             → e.g. "2026"
#   {{NL2SQL_INSTRUCTIONS}}      → MCP server system_instruction (if active)
#   {{AGENTIC_TOOLS_INSTRUCTIONS}} → MCP server system_instruction (if active)
#   {{DEEPWIKI_INSTRUCTIONS}}    → MCP server system_instruction (if active)
DEFAULT_SYSTEM_PROMPT_TEMPLATE = """# TEMPORAL CONTEXT
Today's Date: {{TODAY_DATE}}
Current Year: {{CURRENT_YEAR}}

You are **Talk2Everything**, an AI assistant with access to external tools and services through MCP (Model Context Protocol) servers. Your goal is to help users accomplish tasks safely and effectively using available tools.

# CORE PRINCIPLES

1. **Tool Discovery First**
   - Before attempting any task, explore what tools are available.
   - Understand tool capabilities, parameters, and requirements.
   - Read tool descriptions carefully to determine relevance.

2. **Context-Aware Execution**
   - Gather necessary information before taking action.
   - Use exploration/discovery tools to understand the environment.
   - Build context progressively rather than making assumptions.

3. **Adaptive Workflow**
   - Let the task requirements guide your tool selection.
   - Combine multiple tools intelligently when needed.
   - Adjust your approach based on tool outputs and errors.

# SAFETY & GUARDRAILS

**Data Protection**
- Never expose sensitive credentials, API keys, or authentication tokens.
- Do not log or display sensitive personal information.
- Respect data privacy and confidentiality.

**Destructive Operations**
- Exercise extreme caution with any tool that modifies, deletes, or updates data.
- When possible, prefer read-only operations.
- Warn users before executing potentially destructive actions.
- Ask for explicit confirmation before making irreversible changes.

**Rate Limiting & Resources**
- Be mindful of API rate limits and resource consumption.
- Avoid excessive or redundant tool calls.
- Batch operations when appropriate.

**Error Handling**
- If a tool fails, analyze the error message carefully.
- Don't repeatedly retry the same failing operation.
- Provide clear explanations of errors to users.
- Suggest alternative approaches when tools fail.

**Input Validation**
- Validate that required parameters are present and properly formatted.
- Don't execute commands with malformed or suspicious inputs.
- Sanitize user inputs when passing them to external tools.

# PROHIBITED ACTIONS

- Do not execute code or commands that could harm systems or data.
- Do not attempt to bypass security controls or authentication.
- Do not access resources you're not explicitly authorized to use.
- Do not ignore error messages or tool constraints.
- Do not make assumptions about tool behavior - read documentation.
- Do not invent, guess or predict URL's

# EXECUTION BEST PRACTICES & TRANSPARENCY

- **Read Before Write**: Understand the current state before making changes.
- **Verify Before Execute**: Confirm you have correct parameters and context.
- **Explain Your Actions**: Tell users what you're doing and why.
- **Respect Constraints**: Honor any limitations specified by tool descriptions.
- **Acknowledge Uncertainty**: If you are unsure about a tool's behavior or a result, state it clearly.

---

# WEB SEARCH RULES
1. **Always include the current year** in search queries for time-sensitive topics (stocks, news, weather).
2. If the user asks for "recent" or "near term" info, ensure the search results are from the last 7-30 days.

---

# DATA PRESENTATION RULES

When presenting query results or data tables to the user:
1. **Always present ALL rows** returned by the tool. Never truncate, abbreviate, or say "showing first few rows" or "truncated for brevity."
2. Format results as a complete markdown table with all rows.
3. If the result set is large, still include every row — the UI handles pagination.
4. The SQL Sandbox provides an interactive paginated table, so you must include all data for it to work correctly.

---

# SQL QUERY DEFAULTS

When generating SQL queries for Oracle:
1. **Default row limit is 100.** Unless the user explicitly asks for a specific number of rows, always use `FETCH FIRST 100 ROWS ONLY`.
2. **Never use FETCH FIRST 10 ROWS ONLY** unless the user specifically requests 10 rows.
3. If the user says "show me all" or "list all", use `FETCH FIRST 100 ROWS ONLY` (the tool caps results at 100 regardless).

---

# ACTIVE SERVER INSTRUCTIONS

Below are specific operating instructions for the currently active MCP tools. You must adhere to these strictly when using the corresponding tools.

{{NL2SQL_INSTRUCTIONS}}

{{AGENTIC_TOOLS_INSTRUCTIONS}}

{{DEEPWIKI_INSTRUCTIONS}}
"""


def build_system_prompt(servers: list, template: str = "") -> str:
    """
    Build the full system prompt by substituting placeholders in the template.

    If template is empty, DEFAULT_SYSTEM_PROMPT_TEMPLATE is used.
    Substitutions performed:
      - {{TODAY_DATE}}  → current date
      - {{CURRENT_YEAR}} → current year
      - {{<SERVER>_INSTRUCTIONS}} → matching MCP server's system_instruction (if active)
    """
    prompt = (template or "").strip() or DEFAULT_SYSTEM_PROMPT_TEMPLATE

    # Temporal substitutions
    now = datetime.now()
    prompt = prompt.replace("{{TODAY_DATE}}", now.strftime("%A, %B %d, %Y"))
    prompt = prompt.replace("{{CURRENT_YEAR}}", str(now.year))

    def normalize_name(name: str) -> str:
        return (name or "").upper().replace(" ", "_")

    # MCP server instruction placeholders
    for key in _SYSTEM_PROMPT_PLACEHOLDER_KEYS:
        hint = key.replace("_INSTRUCTIONS", "")
        placeholder = "{{" + key + "}}"
        replacement = ""
        for s in servers:
            if normalize_name(getattr(s, "name", "") or "") != hint:
                continue
            include = bool(getattr(s, "include_in_llm", True))
            instruction = (getattr(s, "system_instruction", None) or "").strip()
            if include and instruction:
                replacement = instruction
            break
        prompt = prompt.replace(placeholder, replacement)

    # Collapse multiple newlines and trim
    prompt = re.sub(r"\n{3,}", "\n\n", prompt)
    return prompt.strip()


def get_system_prompt() -> str:
    """Return the system prompt: pre-set via context var (by API) or build from template with no server instructions."""
    cached = _system_prompt_var.get()
    if cached is not None:
        return cached
    return build_system_prompt([])


# Set OCI environment variables from settings
os.environ['OCI_CONFIG_FILE'] = settings.oci_config_file
os.environ['OCI_CONFIG_PROFILE'] = settings.oci_profile


def create_llm(
    model_id: str,
    compartment_id: str,
    oci_config_file: str,
    oci_profile: str,
    region: str,
    model_kwargs: dict,
    is_stream: bool = True
):
    """
    Factory function to create appropriate LLM/client.
    Returns OciOpenAI (native oci_openai client; use client.responses.create for chat).
    """
    return OciOpenAI(
        region=region,
        auth=OciUserPrincipalAuth(config_file=oci_config_file, profile_name=oci_profile),
        compartment_id=compartment_id,
    )


# Model ID for OpenAI path (used in client.responses.create)
OPENAI_MODEL_ID = "meta.llama-3.1-70b-instruct"

# Initialize OpenAI-compatible client (OCI OpenAI Responses API)
_openai_client: OciOpenAI = create_llm(
    model_id=OPENAI_MODEL_ID,
    compartment_id=settings.compartment_id,
    oci_config_file=settings.oci_config_file,
    oci_profile=settings.oci_profile,
    region=settings.region,
    model_kwargs={"max_tokens": 4096},  # Allow vega-lite code blocks (large JSON) to complete
    #is_stream=True
)

def _get_requested_model_id() -> str:
    """Return the per-request model_id (always OPENAI_MODEL_ID)."""
    return OPENAI_MODEL_ID


def _extract_tool_calls_from_message(msg: BaseMessage) -> List[dict]:
    """
    Normalize tool calls from various provider-specific locations into a list of dicts:
    { name: str, args: dict, id: str }
    """
    tool_calls: Any = None

    if hasattr(msg, "tool_calls"):
        tool_calls = getattr(msg, "tool_calls", None)

    if (not tool_calls) and hasattr(msg, "additional_kwargs"):
        additional = getattr(msg, "additional_kwargs", None) or {}
        if isinstance(additional, dict):
            tool_calls = additional.get("tool_calls") or additional.get("toolCalls")

    if (not tool_calls) and hasattr(msg, "response_metadata"):
        metadata = getattr(msg, "response_metadata", None) or {}
        if isinstance(metadata, dict):
            tool_calls = metadata.get("tool_calls")

    if not tool_calls or not isinstance(tool_calls, list):
        return []

    normalized: List[dict] = []
    for i, tc in enumerate(tool_calls):
        # LangChain standard: {"name","args","id"}
        if isinstance(tc, dict) and "name" in tc and "args" in tc:
            name = str(tc.get("name") or "").strip()
            args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            tc_id = str(tc.get("id") or "")
            normalized.append({"name": name, "args": args, "id": tc_id})
            continue

        # OpenAI-style: {"id","type":"tool_call","function":{"name","arguments": "<json>"}}
        if isinstance(tc, dict) and isinstance(tc.get("function"), dict):
            fn = tc["function"]
            name = str(fn.get("name") or "").strip()
            raw_args = fn.get("arguments")
            args_dict: dict = {}
            if isinstance(raw_args, str) and raw_args.strip():
                try:
                    parsed = json.loads(raw_args)
                    args_dict = parsed if isinstance(parsed, dict) else {}
                except json.JSONDecodeError:
                    args_dict = {}
            tc_id = str(tc.get("id") or f"no-id-{i}")
            normalized.append({"name": name, "args": args_dict, "id": tc_id})
            continue

        # Fallback: try attribute access (some providers may use objects)
        try:
            name = str(getattr(tc, "name", "") or "").strip()
            args = getattr(tc, "args", {}) or {}
            if not isinstance(args, dict):
                args = {}
            tc_id = str(getattr(tc, "id", "") or f"no-id-{i}")
            if name:
                normalized.append({"name": name, "args": args, "id": tc_id})
        except Exception:
            continue

    return normalized


def _normalize_tool_call_id_for_match(tool_call_id: str) -> str:
    """Return a canonical form for matching (fc_xxx and xxx treated as same)."""
    s = (tool_call_id or "").strip()
    if s.startswith("fc_"):
        return s[3:]
    return s


def _ensure_tool_outputs_for_all_calls(messages: Sequence[BaseMessage]) -> List[BaseMessage]:
    """
    Ensure every AIMessage tool call has a corresponding ToolMessage so the OCI API does not
    return 400 (No tool output found for function call). If the graph state was restored from
    a checkpoint and a run was interrupted (e.g. user navigated away or conversation deleted),
    we may have an AIMessage with tool_calls but missing ToolMessages; add placeholders for those.
    """
    result: List[BaseMessage] = []
    pending_ids: set = set()  # normalized tool_call_ids still needing an output

    for msg in messages:
        if isinstance(msg, ToolMessage):
            tid = getattr(msg, "tool_call_id", None) or ""
            if tid:
                pending_ids.discard(_normalize_tool_call_id_for_match(str(tid)))
            result.append(msg)
        elif isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            # Flush any missing outputs from a previous AIMessage before starting this turn
            if pending_ids:
                for raw_id in pending_ids:
                    result.append(
                        ToolMessage(
                            content="Tool execution was interrupted or cancelled.",
                            tool_call_id=raw_id if raw_id.startswith("fc_") else f"fc_{raw_id}",
                        )
                    )
                pending_ids = set()
            # Collect all tool call ids from this AIMessage (normalized for matching)
            for tc in (msg.tool_calls or []):
                raw_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                if raw_id:
                    pending_ids.add(_normalize_tool_call_id_for_match(str(raw_id)))
            result.append(msg)
        else:
            # HumanMessage, SystemMessage, or AIMessage without tool_calls: flush any missing tool outputs first
            if pending_ids:
                for raw_id in pending_ids:
                    result.append(
                        ToolMessage(
                            content="Tool execution was interrupted or cancelled.",
                            tool_call_id=raw_id if raw_id.startswith("fc_") else f"fc_{raw_id}",
                        )
                    )
                pending_ids = set()
            result.append(msg)

    if pending_ids:
        for raw_id in pending_ids:
            result.append(
                ToolMessage(
                    content="Tool execution was interrupted or cancelled.",
                    tool_call_id=raw_id if raw_id.startswith("fc_") else f"fc_{raw_id}",
                )
            )
    return result


# Define a dummy tool for testing
@tool
def get_local_time() -> str:
    """Get the current local time."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_all_tools():
    """
    Get all available tools (dummy tool + active MCP tools from all configured servers).
    Only includes MCP tools that are enabled in tool visibility settings.

    Returns:
        List of LangChain BaseTool objects (for ToolNode and for OCI function declarations).
    """
    #tools = [get_local_time]
    tools: List[BaseTool] = []
    for t in ACTIVE_TOOLS:
        if not is_tool_enabled(t.name):
            continue
        server_name = None
        try:
            server_name = (getattr(t, "metadata", None) or {}).get("mcp_server")
        except Exception:
            server_name = None
        if server_name and not mcp_manager.is_server_included_in_llm(str(server_name)):
            continue
        tools.append(t)
    return tools


def messages_to_oci_input(messages: Sequence[BaseMessage], tools: Optional[List[BaseTool]] = None) -> List[dict]:
    """
    Convert LangChain messages to OCI Responses API input list.

    - HumanMessage -> {"role": "user", "content": content}
    - AIMessage (no tool_calls) -> {"role": "assistant", "content": content}
    - AIMessage (with tool_calls): previous turn is represented by response.output
      (message + function_call items); we only have LangChain state so we emit
      assistant content then function_call_output for each ToolMessage that follows.
    - ToolMessage -> {"type": "function_call_output", "call_id": tool_call_id, "output": content}

    When tools is provided, any tool_call with empty name is inferred from args (OCI rejects empty name).
    If name cannot be inferred, that function_call is skipped so we do not send it to OCI.
    We also skip any function_call_output whose call_id was skipped (OCI 400 if output has no preceding function_call).
    """
    input_list: List[dict] = []
    # Call IDs we skipped (empty name) so we must not send their ToolMessage as function_call_output
    skipped_call_ids: set = set()

    for msg in messages:
        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, list):
                # Multimodal: OCI content array with input_text + input_image
                oci_parts: List[dict] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    part_type = part.get("type")
                    if part_type == "text":
                        oci_parts.append({"type": "input_text", "text": part.get("text", "") or ""})
                    elif part_type == "image_url":
                        image_url_obj = part.get("image_url")
                        url = image_url_obj.get("url", "") if isinstance(image_url_obj, dict) else (image_url_obj or "")
                        if url and isinstance(url, str) and url.strip():
                            oci_parts.append({"type": "input_image", "image_url": url.strip(), "detail": "high"})
                    elif part_type == "file_url":
                        file_url = part.get("file_url")
                        if file_url and isinstance(file_url, str) and file_url.strip():
                            file_name = part.get("file_name") or "document.pdf"
                            url_stripped = file_url.strip()
                            if url_stripped.startswith("data:"):
                                oci_parts.append({"type": "input_file", "filename": file_name, "file_data": url_stripped})
                            else:
                                oci_parts.append({"type": "input_file", "file_url": url_stripped})
                if oci_parts:
                    input_list.append({"role": "user", "content": oci_parts})
                else:
                    input_list.append({"role": "user", "content": ""})
            else:
                content_str = content if isinstance(content, str) else str(content or "")
                input_list.append({"role": "user", "content": content_str})
        elif isinstance(msg, AIMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
            tool_calls = getattr(msg, "tool_calls", None) or []
            if not tool_calls:
                input_list.append({"role": "assistant", "content": content})
            else:
                # Assistant turn with tool calls: emit assistant content then function_call items
                # (same shape as OCI response.output so next request has full conversation)
                if content:
                    input_list.append({"role": "assistant", "content": content})
                for tc in tool_calls:
                    name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                    name = (name or "").strip()
                    args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                    if not isinstance(args, dict):
                        try:
                            args = json.loads(args) if isinstance(args, str) else {}
                        except json.JSONDecodeError:
                            args = {}
                    # OCI 400 if name is empty; infer from args when possible
                    if not name and tools:
                        name = _infer_tool_name_from_args(args, tools)
                    call_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                    if not name:
                        # Skip this function_call and do not send its output later (OCI 400 otherwise)
                        if call_id:
                            skipped_call_ids.add(str(call_id))
                            c = str(call_id)
                            if c.startswith("fc_"):
                                skipped_call_ids.add(c[3:])
                            else:
                                skipped_call_ids.add(f"fc_{c}")
                        continue  # skip this function_call so we do not send empty name to OCI
                    # OCI/OpenAI Responses API requires function_call input items to have id starting with "fc"
                    fc_id = f"fc_{call_id}" if call_id and not str(call_id).startswith("fc_") else (call_id or "fc_")
                    input_list.append({
                        "type": "function_call",
                        "id": fc_id,
                        "name": name,
                        "arguments": json.dumps(args) if isinstance(args, dict) else str(args or "{}"),
                        "call_id": call_id,
                    })
        elif isinstance(msg, ToolMessage):
            call_id = getattr(msg, "tool_call_id", None) or ""
            if call_id and str(call_id) in skipped_call_ids:
                continue  # Do not send output for a call we skipped (OCI 400 if output has no preceding function_call)
            content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
            # Truncate very long tool output to reduce OCI 500 (payload/context limits)
            _max_tool_output_chars = 20000
            if len(content) > _max_tool_output_chars:
                content = content[:_max_tool_output_chars] + "\n\n[Output truncated for length.]"
            # OCI/OpenAI Responses API requires function_call_output input items to have id starting with "fc"
            fc_id = f"fc_{call_id}" if call_id and not str(call_id).startswith("fc_") else (call_id or "fc_")
            input_list.append({
                "type": "function_call_output",
                "id": fc_id,
                "call_id": call_id,
                "output": content,
            })
        else:
            content = getattr(msg, "content", str(msg))
            input_list.append({"role": "user", "content": str(content)})
    return input_list


# Top-level keys OCI disallows in tools[].parameters (must be plain type "object")
_OCI_PARAMS_DISALLOWED_TOP_LEVEL = frozenset({"oneOf", "anyOf", "allOf", "enum", "not"})


def _normalize_oci_parameters_schema(params: dict) -> dict:
    """
    Ensure params is a valid JSON Schema object for OCI: type "object", properties dict,
    and no oneOf/anyOf/allOf/enum/not at the top level (OCI rejects these).
    """
    if not isinstance(params, dict):
        return {"type": "object", "properties": {}}
    params = _ensure_no_any_in_schema(params.copy())
    # Remove top-level keys OCI does not allow
    for key in _OCI_PARAMS_DISALLOWED_TOP_LEVEL:
        params.pop(key, None)
    # OCI requires parameters to be a JSON Schema with type "object"
    if params.get("type") not in ("object",):
        params["type"] = "object"
    if "properties" not in params or not isinstance(params.get("properties"), dict):
        params["properties"] = {}
    return params


def tools_to_oci_functions(tools: List[BaseTool]) -> List[dict]:
    """
    Convert LangChain tools to OCI Responses API function tool list.
    Uses same tool names as LangChain so ToolNode can resolve function_call.name.
    """
    oci_tools: List[dict] = []
    for t in tools:
        name = getattr(t, "name", None) or "unknown"
        description = getattr(t, "description", None) or ""
        try:
            schema = t.get_input_schema() if hasattr(t, "get_input_schema") else {}
            if hasattr(schema, "model_json_schema"):
                params = schema.model_json_schema()
            elif isinstance(schema, dict):
                params = schema
            else:
                params = {"type": "object", "properties": {}}
            params = _normalize_oci_parameters_schema(params)
        except Exception:
            params = {"type": "object", "properties": {}}
        oci_tools.append({
            "type": "function",
            "name": name,
            "description": description or name,
            "parameters": params,
        })
    return oci_tools


def _message_to_dict(obj: Any) -> dict:
    """Convert a message or OCI response to a JSON-serializable dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return {k: _message_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (str, int, float, bool)):
        return obj  # type: ignore
    if isinstance(obj, list):
        return [_message_to_dict(x) for x in obj]
    out: dict = {}
    for key in ("content", "output_text", "output", "tool_calls", "additional_kwargs", "response_metadata"):
        val = getattr(obj, key, None)
        if val is not None:
            if callable(val):
                val = val()
            out[key] = _message_to_dict(val) if isinstance(val, (dict, list)) and not isinstance(val, str) else val
    if not out and hasattr(obj, "__dict__"):
        out = {k: getattr(obj, k) for k in dir(obj) if not k.startswith("_")}
        out = {k: v for k, v in out.items() if not callable(v)}
    return out


def _serialize_messages_and_tools(messages: Sequence[BaseMessage], tools: List[BaseTool]) -> dict:
    """Serializable form of cleaned messages and tool definitions for execution capture."""
    msg_list: List[dict] = []
    for m in messages:
        d: dict = {"type": type(m).__name__}
        if hasattr(m, "content"):
            d["content"] = m.content if isinstance(getattr(m, "content"), str) else str(getattr(m, "content", ""))
        if hasattr(m, "tool_calls") and getattr(m, "tool_calls"):
            d["tool_calls"] = [
                {"name": (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")), "id": (tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", ""))}
                for tc in (getattr(m, "tool_calls") or [])
            ]
        msg_list.append(d)
    tools_schema = tools_to_oci_functions(tools)
    return {"messages": msg_list, "tools": tools_schema}


def _get_item_attr(item: Any, key: str, default: Any = None) -> Any:
    """Get attribute or dict key from an output item (object or dict)."""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _infer_tool_name_from_args(args: dict, tools_list: List[BaseTool]) -> str:
    """Infer tool name from args by matching to tool schemas (fallback when stream event has no name)."""
    if not args or not isinstance(args, dict) or not tools_list:
        return ""
    arg_keys = set(args.keys())
    best_name = ""
    best_score = 0
    for t in tools_list:
        schema = getattr(t, "args_schema", None)
        if schema is None:
            continue
        try:
            if isinstance(schema, dict):
                schema_dict = schema
            else:
                js = getattr(schema, "model_json_schema", None)
                schema_dict = js() if callable(js) else {}
        except Exception:
            schema_dict = {}
        props = schema_dict.get("properties") or {}
        schema_keys = set(props.keys())
        if not schema_keys:
            continue
        overlap = len(arg_keys & schema_keys)
        if overlap > best_score and overlap >= len(arg_keys) * 0.5:
            best_score = overlap
            best_name = getattr(t, "name", "") or ""
    return best_name or ""


def _id_to_name_from_output(output: Any) -> Dict[str, str]:
    """
    Build a map from tool call id (and call_id) to tool name from response.output.
    OCI can use 'id' (fc_xxx) in stream events and 'call_id' in output; map both to name.
    """
    if output is None:
        return {}
    if isinstance(output, dict) and "data" in output:
        output = output.get("data") or []
    if not isinstance(output, list):
        return {}
    id_to_name: Dict[str, str] = {}
    items: List[Any] = []
    for item in output:
        if _get_item_attr(item, "type") == "function_call":
            items.append(item)
        elif _get_item_attr(item, "type") == "message":
            content = _get_item_attr(item, "content")
            if isinstance(content, list):
                for sub in content:
                    if _get_item_attr(sub, "type") == "function_call":
                        items.append(sub)
    for item in items:
        name = (_get_item_attr(item, "name") or "").strip()
        if not name:
            continue
        oid = _get_item_attr(item, "id") or ""
        call_id = _get_item_attr(item, "call_id") or ""
        if oid:
            id_to_name[oid] = name
        if call_id and call_id != oid:
            id_to_name[call_id] = name
    return id_to_name


def _collect_function_call_items(output: Any) -> List[dict]:
    """
    Collect all function_call items from response.output.
    Handles: output as list or dict with "data"; items as object or dict;
    function_call at top-level or nested inside message.content.
    """
    if output is None:
        return []
    if isinstance(output, dict) and "data" in output:
        output = output.get("data") or []
    if not isinstance(output, list):
        return []
    items: List[Any] = []
    for item in output:
        if _get_item_attr(item, "type") == "function_call":
            items.append(item)
        elif _get_item_attr(item, "type") == "message":
            content = _get_item_attr(item, "content")
            if isinstance(content, list):
                for sub in content:
                    if _get_item_attr(sub, "type") == "function_call":
                        items.append(sub)
    tool_calls = []
    for item in items:
        name = _get_item_attr(item, "name") or ""
        arguments = _get_item_attr(item, "arguments")
        if arguments is None:
            arguments = _get_item_attr(item, "input")
        # Prefer 'id' (fc_xxx) so collected ids match stream event item_id for name lookup
        call_id = _get_item_attr(item, "id") or _get_item_attr(item, "call_id") or ""
        try:
            if isinstance(arguments, str):
                args = json.loads(arguments) if arguments.strip() else {}
            elif isinstance(arguments, dict):
                args = arguments
            else:
                args = {}
        except json.JSONDecodeError:
            args = {}
        tool_calls.append({"id": call_id, "name": name, "args": args})
    return tool_calls


def oci_response_to_aimessage(response: Any) -> AIMessage:
    """
    Convert OCI Responses API response to LangChain AIMessage.
    Extracts output_text and function_call items from response.output.
    Handles both object-style and dict-style output; function_call at top-level
    or nested inside message.content.
    """
    output_text = getattr(response, "output_text", None)
    if callable(output_text):
        output_text = output_text()
    content = (output_text or "") if isinstance(output_text, str) else ""
    output = getattr(response, "output", None)
    if output is None and isinstance(response, dict):
        output = response.get("output")
    output = output or []
    tool_calls = _collect_function_call_items(output)
    return AIMessage(content=content, tool_calls=tool_calls if tool_calls else [])


# Define the state schema
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]


# Create the StateGraph
graph = StateGraph(AgentState)


def _remove_unsupported_fields(obj: Any, unsupported_fields: set = None) -> Any:
    """
    Recursively remove unsupported fields from an object.
    Unsupported fields include 'valid' and other metadata that LLM providers don't accept.
    """
    if unsupported_fields is None:
        unsupported_fields = {'valid'}
    
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            if k not in unsupported_fields:
                cleaned[k] = _remove_unsupported_fields(v, unsupported_fields)
        return cleaned
    elif isinstance(obj, list):
        return [_remove_unsupported_fields(item, unsupported_fields) for item in obj]
    elif hasattr(obj, 'model_dump'):
        # Pydantic model - convert to dict, clean, and return dict
        obj_dict = obj.model_dump()
        return _remove_unsupported_fields(obj_dict, unsupported_fields)
    else:
        return obj


def _clean_messages_for_llm(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """
    Clean messages to remove unsupported fields before sending to LLM.
    Some LLM providers (like OpenAI) don't support certain fields like 'valid'.
    Creates fresh message objects with only supported fields, ensuring no
    unsupported metadata is included.
    """
    cleaned = []
    for msg in messages:
        try:
            if isinstance(msg, HumanMessage):
                # Create clean HumanMessage with only content
                cleaned.append(HumanMessage(
                    content=msg.content,
                    additional_kwargs={}  # Explicitly empty to remove any unsupported fields
                ))
            elif isinstance(msg, AIMessage):
                # Preserve tool_calls but clean everything else
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    # Clean tool_calls to remove any unsupported fields
                    clean_tool_calls = []
                    for tc in tool_calls:
                        # Create a clean dict with only supported fields
                        if isinstance(tc, dict):
                            clean_tc = {
                                "name": tc.get("name", ""),
                                "args": tc.get("args", {}),
                                "id": tc.get("id", None),
                            }
                        else:
                            clean_tc = {
                                "name": getattr(tc, "name", ""),
                                "args": getattr(tc, "args", {}),
                                "id": getattr(tc, "id", None),
                            }
                        # Remove None values and recursively clean args
                        clean_tc = {k: _remove_unsupported_fields(v) if k == "args" else v 
                                   for k, v in clean_tc.items() if v is not None}
                        clean_tool_calls.append(clean_tc)
                    
                    cleaned.append(AIMessage(
                        content=msg.content or "",
                        tool_calls=clean_tool_calls,
                        additional_kwargs={}  # Explicitly empty
                    ))
                else:
                    cleaned.append(AIMessage(
                        content=msg.content or "",
                        additional_kwargs={}  # Explicitly empty
                    ))
            elif isinstance(msg, ToolMessage):
                cleaned.append(ToolMessage(
                    content=msg.content,
                    tool_call_id=getattr(msg, "tool_call_id", None) or "",
                    additional_kwargs={}  # Explicitly empty
                ))
            else:
                # For other message types, convert to HumanMessage
                content = getattr(msg, "content", str(msg))
                cleaned.append(HumanMessage(
                    content=str(content),
                    additional_kwargs={}
                ))
        except Exception as e:
            # Fallback: create new message from scratch
            print(f"[CLEAN] Error cleaning message, falling back to manual creation: {e}")
            if isinstance(msg, HumanMessage):
                cleaned.append(HumanMessage(content=msg.content, additional_kwargs={}))
            elif isinstance(msg, AIMessage):
                tool_calls = getattr(msg, "tool_calls", None)
                # Clean tool_calls if present
                clean_tool_calls = []
                if tool_calls:
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            clean_tc = {
                                "name": tc.get("name", ""),
                                "args": tc.get("args", {}),
                                "id": tc.get("id", None),
                            }
                        else:
                            clean_tc = {
                                "name": getattr(tc, "name", ""),
                                "args": getattr(tc, "args", {}),
                                "id": getattr(tc, "id", None),
                            }
                        clean_tc = {k: v for k, v in clean_tc.items() if v is not None}
                        clean_tool_calls.append(clean_tc)
                cleaned.append(AIMessage(
                    content=msg.content or "",
                    tool_calls=clean_tool_calls,
                    additional_kwargs={}
                ))
            elif isinstance(msg, ToolMessage):
                cleaned.append(ToolMessage(
                    content=msg.content,
                    tool_call_id=getattr(msg, "tool_call_id", ""),
                    additional_kwargs={}
                ))
            else:
                cleaned.append(HumanMessage(
                    content=str(getattr(msg, "content", "")), 
                    additional_kwargs={}
                ))
    return cleaned


# Chatbot node: calls the LLM
def chatbot(state: AgentState) -> AgentState:
    """Node that calls the LLM with the current messages."""
    tools = get_all_tools()

    model_for_request = _get_requested_model_id()

    # OpenAI-compatible path (OCI OpenAI Responses API)
    if True: # Bypass prefix check so Llama models work
        # Ensure every tool call has an output so OCI does not return 400 (e.g. after interrupted run / restored checkpoint)
        messages_for_input = _ensure_tool_outputs_for_all_calls(state["messages"])
        input_list = messages_to_oci_input(messages_for_input, tools=tools)
        input_list = [{"role": "system", "content": get_system_prompt()}] + input_list
        if _is_db_connected(state["messages"]):
            input_list.insert(1, {"role": "system", "content": CONNECTION_STATUS_SYSTEM})
        tools_oci = tools_to_oci_functions(tools)
        create_kwargs = {
            "model": model_for_request,
            "input": input_list,
            "store": False,
            "stream": True,  # Enable streaming for OpenAI models
        }
        if tools_oci:
            create_kwargs["tools"] = tools_oci
        if _web_search_enabled_var.get():
            create_kwargs["tools"] = list(create_kwargs.get("tools") or []) + [{"type": "web_search"}]
        create_kwargs["max_output_tokens"] = 4096  # Allow vega-lite code blocks (large JSON) to complete
        capture = _execution_capture_var.get()
        if isinstance(capture, dict):
            capture["full_prompt"] = create_kwargs

        # Stream the response and collect chunks
        stream = _openai_client.responses.create(**create_kwargs)
        full_text = ""
        tool_calls_data = []
        final_response = None
        stream_queue = _stream_queue_var.get()

        for event in stream:
            event_type = getattr(event, "type", None)

            # Collect text deltas and forward to API stream queue when set
            if event_type == "response.output_text.delta":
                delta = getattr(event, "delta", "")
                if delta:
                    full_text += delta
                    if stream_queue is not None:
                        stream_queue.put(delta)

            # Collect function call arguments (OCI/OpenAI can send name/item_id as None)
            elif event_type == "response.function_call_arguments.done":
                tool_calls_data.append({
                    "name": getattr(event, "name", None) or "",
                    "arguments": getattr(event, "arguments", None) or "{}",
                    "id": getattr(event, "item_id", None) or "",
                })

            # Store final response for capture
            elif event_type == "response.completed":
                final_response = getattr(event, "response", None)

        # Signal end of this message segment for queue-based streaming (supports tool loops)
        if stream_queue is not None:
            stream_queue.put(None)

        # Fallback: if stream didn't send function_call_arguments.done, get tool calls from final response.output
        if not tool_calls_data and final_response:
            output = getattr(final_response, "output", None)
            fc_from_output = _collect_function_call_items(output)
            if fc_from_output:
                for item in fc_from_output:
                    tool_calls_data.append({
                        "name": item.get("name") or "",
                        "arguments": json.dumps(item.get("args") or {}) if isinstance(item.get("args"), dict) else (item.get("args") or "{}"),
                        "id": item.get("id") or "",
                    })
        # Fill in missing tool call names (OCI stream can send name=None; use response.output which has correct name)
        if tool_calls_data:
            if final_response:
                output = getattr(final_response, "output", None)
                id_to_name = _id_to_name_from_output(output)
                for tc in tool_calls_data:
                    if not (tc.get("name") or "").strip() and tc.get("id"):
                        tid = tc["id"]
                        tc["name"] = id_to_name.get(tid) or id_to_name.get("fc_" + tid if tid and not str(tid).startswith("fc_") else (tid[3:] if str(tid).startswith("fc_") else tid)) or ""
            for tc in tool_calls_data:
                if not (tc.get("name") or "").strip():
                    try:
                        args = json.loads(tc["arguments"]) if isinstance(tc["arguments"], str) else (tc.get("arguments") or {})
                    except json.JSONDecodeError:
                        args = {}
                    inferred = _infer_tool_name_from_args(args if isinstance(args, dict) else {}, tools)
                    if inferred:
                        tc["name"] = inferred

        # Capture raw response; accumulate tool_calls across graph so final metadata has all invocations in order
        if isinstance(capture, dict):
            try:
                prev = capture.get("raw_response")
                prev_tool_calls = (
                    list(prev.get("tool_calls")) if isinstance(prev, dict) and prev.get("tool_calls") else []
                )
                accumulated_tool_calls = prev_tool_calls + list(tool_calls_data or [])
                if final_response:
                    raw = getattr(final_response, "model_dump", None)
                    capture["raw_response"] = raw() if callable(raw) else _message_to_dict(final_response)
                    if not isinstance(capture["raw_response"], dict):
                        capture["raw_response"] = {"raw": capture["raw_response"]}
                    if accumulated_tool_calls:
                        capture["raw_response"]["tool_calls"] = accumulated_tool_calls
                else:
                    capture["raw_response"] = {"full_text": full_text, "tool_calls": accumulated_tool_calls}
            except Exception:
                capture["raw_response"] = str(final_response) if final_response else {"full_text": full_text}
                if isinstance(capture["raw_response"], str) and tool_calls_data:
                    capture["raw_response"] = {"full_text": capture["raw_response"], "tool_calls": list(tool_calls_data)}

        # Build AIMessage from collected data (ensure name/id are strings for Pydantic)
        parsed_tool_calls = []
        for tc in tool_calls_data:
            try:
                args = json.loads(tc["arguments"]) if isinstance(tc["arguments"], str) else tc["arguments"]
            except json.JSONDecodeError:
                args = {}
            parsed_tool_calls.append({
                "name": tc.get("name") or "",
                "args": args,
                "id": tc.get("id") or "",
            })

        aimessage = AIMessage(content=full_text, tool_calls=parsed_tool_calls if parsed_tool_calls else [])
        return {"messages": [aimessage]}



# Define the routing logic
def should_continue(state: AgentState) -> str:
    """Determine whether to continue to tools or end."""
    messages = state["messages"]
    last_message = messages[-1]
    
    # Check for tool calls in a provider-agnostic way (Cohere may store them outside .tool_calls)
    tool_calls = _extract_tool_calls_from_message(last_message)
    
    # Check if tool_calls is a list and not empty
    if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
        # Enforce max tool invocations per user question
        messages = state.get("messages") or []
        tool_invocation_count = sum(1 for m in messages if isinstance(m, ToolMessage))
        if tool_invocation_count >= MAX_TOOL_INVOCATIONS:
            return END
        return "tools"
    
    # Otherwise, end
    return END


def _is_db_connected(messages: Sequence[BaseMessage]) -> bool:
    """
    Derive DB connection state from message history.
    Returns True if we have seen a successful SalesDB_connect result (content contains
    DATABASE CONNECTION ESTABLISHED or Successfully connected).
    """
    pending: List[str] = []
    connected = False
    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            pending = [
                (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", ""))
                for tc in msg.tool_calls
            ]
        elif isinstance(msg, ToolMessage):
            content = (getattr(msg, "content", "") or "") if hasattr(msg, "content") else ""
            if pending:
                name = pending.pop(0)
                if name == "SalesDB_connect" and (
                    "DATABASE CONNECTION ESTABLISHED" in content or "Successfully connected" in content
                ):
                    connected = True
    return connected


# Injected when connected so the model does not call SalesDB_connect again
CONNECTION_STATUS_SYSTEM = (
    "DATABASE STATUS: CONNECTED to Sales@TECPDATP01. Do NOT call SalesDB_connect again unless explicitly instructed."
)


def timed_tools_node(state: dict) -> dict:
    """
    Execute tool calls from the last AIMessage and return ToolMessages with timing.
    Records invoked_at (local time with ms) and duration_ms per tool in additional_kwargs.
    For tools with require_approval (human-in-the-loop), calls interrupt() and on resume
    either executes the tool or returns "User declined."
    """
    messages = state.get("messages") or []
    if not messages:
        return {"messages": []}
    last_msg = messages[-1]
    if not isinstance(last_msg, AIMessage):
        return {"messages": []}
    tool_calls = _extract_tool_calls_from_message(last_msg)
    if not tool_calls:
        return {"messages": []}

    tools = get_all_tools()
    tools_by_name = {t.name: t for t in tools}
    new_messages = []
    connected = _is_db_connected(messages)

    for tc in tool_calls:
        tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
        name = (tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")) or ""
        name = name.strip() if isinstance(name, str) else ""
        args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
        if not isinstance(args, dict):
            args = {}

        now = datetime.now()
        invoked_at = now.strftime("%Y-%m-%d %H:%M:%S") + f".{now.microsecond // 1000:03d}"

        # OCI can return tool_call with empty name; do not invoke and return clear error (output will be skipped in next OCI request)
        if not name:
            new_messages.append(
                ToolMessage(
                    content="Error: Tool name was empty. Please call a valid tool with a non-empty name (e.g. SalesDB_connect, SalesDB_schema_information).",
                    tool_call_id=tc_id,
                    additional_kwargs={"invoked_at": invoked_at, "duration_ms": 0},
                )
            )
            continue

        if name == "SalesDB_connect" and connected:
            new_messages.append(
                ToolMessage(
                    content="Connection already established. Proceed to SalesDB_schema_information.",
                    tool_call_id=tc_id,
                    additional_kwargs={"invoked_at": invoked_at, "duration_ms": 0},
                )
            )
            continue

        start = time.perf_counter()
        tool_obj = tools_by_name.get(name)
        if tool_obj is None:
            content = f"Error: Tool '{name}' not found."
        else:
            try:
                result = tool_obj.invoke(args)
                content = result if isinstance(result, str) else str(result)
            except Exception as e:
                content = f"Error: {e}"

        duration_ms = (time.perf_counter() - start) * 1000
        new_messages.append(
            ToolMessage(
                content=content,
                tool_call_id=tc_id,
                additional_kwargs={"invoked_at": invoked_at, "duration_ms": round(duration_ms)},
            )
        )

    return {"messages": new_messages}


def create_graph():
    """Create and compile the graph with current tools."""
    tools_node = timed_tools_node

    # Create the StateGraph
    graph = StateGraph(AgentState)

    # Add nodes
    graph.add_node("chatbot", chatbot)
    graph.add_node("tools", tools_node)
    
    # Add edges
    graph.set_entry_point("chatbot")
    graph.add_conditional_edges(
        "chatbot",
        should_continue,
        {
            "tools": "tools",
            END: END,
        },
    )
    graph.add_edge("tools", "chatbot")
    
    return graph.compile()


# Create initial graph
app = create_graph()


def get_graph_mermaid() -> str:
    """
    Return the current compiled graph as Mermaid diagram text for persistence/display.
    Returns empty string on any error so callers never fail.
    """
    try:
        graph_obj = app.get_graph()
        if graph_obj is not None and hasattr(graph_obj, "draw_mermaid"):
            out = graph_obj.draw_mermaid()
            return out if isinstance(out, str) else ""
    except Exception:
        pass
    return ""


def ensure_graph_uses_current_tools() -> None:
    """
    Recreate the compiled graph so both the chatbot node and ToolNode use the
    current tool list (get_all_tools()). Call this after refreshing MCP tools.
    """
    global app
    app = create_graph()


def run_graph_streaming(
    queue: Queue,
    messages: list[BaseMessage],
    main_loop: Optional[Any] = None,
    web_search_enabled: bool = False,
    system_prompt: Optional[str] = None,
    model_id: Optional[str] = None,
    stream_config: Optional[dict] = None,
) -> tuple[dict, dict | None]:
    """
    Run the graph in the current thread with queue-based token streaming.
    Call from an executor thread; sets _stream_queue_var so the chatbot node
    (OCI path) pushes each token delta to the queue and None at end of each message.
    Puts STREAM_GRAPH_DONE when the graph finishes.
    If main_loop is provided, MCP tool calls run on that loop (required for existing sessions).
    Returns (last_state, execution_capture) for the API to build full_response and metadata.
    """
    if main_loop is not None:
        set_main_loop_for_tools(main_loop)
    _stream_queue_var.set(queue)
    _execution_capture_var.set({})
    _web_search_enabled_var.set(web_search_enabled)
    if system_prompt is not None:
        _system_prompt_var.set(system_prompt)
    if model_id is not None:
        _model_id_var.set(model_id)
    config = stream_config or {"configurable": {"thread_id": f"run_{id(queue)}"}}
    last_state = None
    try:
        # Use stream_mode="values" to get full accumulated state; default "updates" yields only
        # node deltas, so last_state had 0 messages when END was reached from should_continue.
        for state in app.stream({"messages": messages}, config=config, stream_mode="values"):
            last_state = state
        queue.put(STREAM_GRAPH_DONE)
        return (last_state or {}, _execution_capture_var.get())
    except Exception as e:
        queue.put(STREAM_GRAPH_DONE)
        raise


# Expose streaming function
async def run_agent_stream(messages: list[BaseMessage], refresh_tools: bool = True):
    """
    Run the agent with streaming output.
    
    Args:
        messages: List of messages to process
        refresh_tools: If True, refresh tools from MCP servers before running
    """
    global app
    if refresh_tools:
        await mcp_manager.refresh_tools()
        # Recreate graph with updated tools
        app = create_graph()
    
    return app.stream({"messages": messages})


# Expose astream_events function for SSE streaming
async def run_agent_astream_events(messages: list[BaseMessage], refresh_tools: bool = True):
    """
    Run the agent with astream_events for fine-grained event streaming.
    
    Args:
        messages: List of messages to process
        refresh_tools: If True, refresh tools from MCP servers before running
    
    Yields:
        Events from the agent execution
    """
    global app
    if refresh_tools:
        await mcp_manager.refresh_tools()
        # Recreate graph with updated tools
        app = create_graph()
    
    # Use astream_events with version="v1" for structured events
    async for event in app.astream_events({"messages": messages}, version="v1"):
        yield event
