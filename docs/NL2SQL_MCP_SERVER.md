# NL2SQL MCP Server (Talk2Data)

MCP server that exposes placeholder tools for natural-language-to-SQL on an **Oracle** database. Intended for use with the Talk2Data application; tools can be implemented incrementally.

## Tools

| Tool | Purpose | Input | Output |
|------|---------|--------|--------|
| **list_subject_areas** | First step: check if the question fits a known domain | — | `List[str]` e.g. `['Human Resources', 'Supply Chain', 'Financials']` |
| **search_schema_objects** | Semantic search for relevant tables from natural language | `query: str` | `List[Dict]` with `table_name`, `similarity_score`, `reasoning` |
| **get_table_metadata** | Enhanced DDL: columns, types, FKs, annotations | `table_names: List[str]` | Structured text/markdown |
| **get_sample_data** | Sample rows to resolve values (e.g. "USA" vs "US") | `table_name: str`, `limit: int` (default 3) | `List[Dict]` (rows) |
| **execute_read_only_sql** | Safely run generated SQL | `sql_query: str` | JSON result set or structured error |
| **analyze_data_insights** | Statistical analysis of result rows | `data_rows: List[Dict]`, `user_question: str` | Narrative string |
| **generate_vega_spec** | Vega-Lite chart spec for web app | `data_summary: Dict`, `user_intent: str` | Vega-Lite JSON spec |

## Run the server

```bash
# From repo root, with fastmcp installed (e.g. same env as agentic_tools_mcp_server.py, or poetry install)
python nl2sql_mcp_server.py
```

Server runs with **SSE** on **port 8082** (Agentic Tools server uses 8081). Use the same virtualenv as `agentic_tools_mcp_server.py` if you already have FastMCP there.

## Configure in Talk2Data

1. Add an MCP server in settings.
2. **Transport:** SSE  
3. **URL:** `http://localhost:8082/sse` (or your host/port).
4. Enable the NL2SQL server and the tools you want to expose to the agent.

## Implementing tools

- **list_subject_areas:** Load from config or Oracle metadata (e.g. table comments / subject-area mapping).
- **search_schema_objects:** Use embeddings over table/column names and comments, or a small vector store.
- **get_table_metadata:** Query `ALL_TAB_COLUMNS`, `ALL_CONSTRAINTS`, `ALL_CONS_COLUMNS` (and optionally comments).
- **get_sample_data:** `SELECT * FROM &lt;table&gt; FETCH FIRST n ROWS ONLY` (or `ROWNUM`).
- **execute_read_only_sql:** Enforce read-only (e.g. block DML/DDL, use a read-only Oracle user/session).
- **analyze_data_insights:** Use pandas/numpy for stats, trends, and anomaly detection; return a short narrative.
- **generate_vega_spec:** Map column types and `user_intent` to Vega-Lite mark/encoding (bar, line, scatter, etc.).
