"""
NL2SQL MCP Server for Talk2Everything application.

Provides placeholder tools for natural-language-to-SQL operations on an Oracle database.
Tools can be expanded with real implementations (schema search, DDL, sample data,
read-only SQL execution, insights, and Vega-Lite chart generation).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv

# Load .env.nl2sql for independent service configuration.
# Priority:
# 1. NL2SQL_CONFIG_PATH env var (if set)
# 2. .env.nl2sql in current working directory
# 3. .env.nl2sql in script directory (fallback for local dev)

_env_filename = ".env.nl2sql"
_custom_path = os.environ.get("NL2SQL_CONFIG_PATH")

if _custom_path:
    _env_path = Path(_custom_path)
else:
    # Try current directory first (deployment standard)
    _cwd_env = Path.cwd() / _env_filename
    if _cwd_env.exists():
        _env_path = _cwd_env
    else:
        # Fallback to script directory (local dev convenience)
        _env_path = Path(__file__).resolve().parent / _env_filename

if _env_path.exists():
    # logger not configured yet, use print for startup diag
    print(f"Loading configuration from {_env_path}")
    load_dotenv(_env_path)
else:
    print(f"Configuration file {_env_filename} not found. Relying on existing environment variables.")

import oracledb
from fastmcp import FastMCP
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# --- Global Oracle connection pool (env-based) ---
_oracle_pool: oracledb.ConnectionPool | None = None
_oracle_pool_error: str | None = None  # Set when config present but pool creation failed
_oracle_pool_lock = threading.Lock()


def _get_oracle_pool() -> oracledb.ConnectionPool | None:
    """Return the global Oracle connection pool, creating it from env if needed. Returns None if not configured or on failure."""
    global _oracle_pool, _oracle_pool_error
    with _oracle_pool_lock:
        if _oracle_pool is not None:
            return _oracle_pool
        _oracle_pool_error = None
        dsn = (os.environ.get("ORACLE_NL2SQL_DSN") or "").strip()
        user = (os.environ.get("ORACLE_NL2SQL_USER") or "").strip()
        password = (os.environ.get("ORACLE_NL2SQL_PASSWORD") or "").strip()
        wallet_path = (os.environ.get("ORACLE_NL2SQL_WALLET_PATH") or "").strip()
        wallet_password = (os.environ.get("ORACLE_NL2SQL_WALLET_PASSWORD") or "").strip() or None
        if not dsn or not user or not password:
            return None
        # config_dir: where tnsnames.ora is found (required when DSN is a TNS alias).
        # With Oracle Wallet, tnsnames.ora is in the wallet directory.
        params = oracledb.PoolParams(
            min=1,
            max=5,
            wait_timeout=30000,      # Max 30s wait to acquire a connection (ms); raises if pool exhausted
            ping_interval=60,        # Ping idle connections every 60s to detect stale ones (Oracle ADB drops idle ~5min)
            wallet_location=wallet_path or None,
            config_dir=wallet_path or None,
            wallet_password=wallet_password,
        )
        try:
            _oracle_pool = oracledb.create_pool(
                user=user,
                password=password,
                dsn=dsn,
                params=params,
            )
            return _oracle_pool
        except Exception as e:
            logger.exception("Failed to create Oracle connection pool: %s", e)
            _oracle_pool_error = str(e)
            return None

# Create MCP server for NL2SQL / Talk2Everything
# Note: this FastMCP version does not support a description keyword argument.
mcp = FastMCP("NL2SQL")


# --- Tool 1: list_subject_areas (run first to check if question fits a known domain) ---

@mcp.tool()
def list_subject_areas() -> list[str]:
    """
    Returns the list of known subject areas (domains) in the data model.
    Use this as the first step before table search: check if the user's question
    fits a known pattern (e.g. Human Resources, Supply Chain, Financials).
    """
    # Placeholder: return example domains
    return ["Human Resources", "Supply Chain", "Financials", "Sales", "Inventory"]


# --- Tool 2: search_schema_objects ---

@mcp.tool()
def search_schema_objects(query: str) -> list[dict[str, Any]]:
    """
    Performs semantic search to find relevant tables based on the user's natural language query.

    Purpose: Performs semantic search to find relevant tables based on the user's natural language query.
    Input: query (str) – e.g. "net profit trends", "employee attrition".
    Output: List of dicts, each with table_name, similarity_score, and reasoning.
    """
    # Placeholder: no logic; return stub so output shape is clear.
    return [
        {"table_name": "AI_FCCS_RATIOS_V", "similarity_score": 1.0, "reasoning": "Placeholder; implement semantic search."},
        {"table_name": "AI_FCCS_ENTITY_VALUES_V", "similarity_score": 1.0, "reasoning": "Placeholder; implement semantic search."},
        {"table_name": "AI_FCCS_BUDGET_ACTUAL_V", "similarity_score": 1.0, "reasoning": "Placeholder; implement semantic search."}
    ]


# --- Tool 3: get_table_metadata (Enhanced DDL) ---


def _format_data_type(row: dict[str, Any]) -> str:
    """Build a readable Oracle data type from ALL_TAB_COLUMNS row."""
    data_type = (row.get("DATA_TYPE") or "UNKNOWN").upper()
    if data_type in ("VARCHAR2", "CHAR", "RAW"):
        length = row.get("DATA_LENGTH")
        return f"{data_type}({length})" if length is not None else data_type
    if data_type in ("NUMBER", "FLOAT"):
        prec = row.get("DATA_PRECISION")
        scale = row.get("DATA_SCALE")
        if prec is not None:
            return f"NUMBER({prec},{scale})" if scale is not None else f"NUMBER({prec})"
        return "NUMBER"
    return data_type


def _wrap_text(text: str, width: int = 78) -> str:
    """Wrap long text at word boundaries for readability."""
    if not text or len(text) <= width:
        return text
    out: list[str] = []
    rest = text
    while rest:
        if len(rest) <= width:
            out.append(rest.strip())
            break
        chunk = rest[: width + 1]
        last_space = chunk.rfind(" ")
        if last_space > width // 2:
            line, rest = rest[:last_space], rest[last_space + 1 :].lstrip()
        else:
            line, rest = rest[:width], rest[width:].lstrip()
        out.append(line)
    return "\n".join(out)


def _fetch_table_metadata(conn: oracledb.Connection, table_name: str) -> str:
    """Fetch metadata for one table and return a Markdown section. On error raises."""
    # A. Columns + comments
    cols_sql = """
        SELECT c.COLUMN_NAME, c.DATA_TYPE, c.DATA_LENGTH, c.DATA_PRECISION, c.DATA_SCALE, c.NULLABLE, cc.COMMENTS
        FROM ALL_TAB_COLUMNS c
        LEFT JOIN ALL_COL_COMMENTS cc ON c.OWNER = cc.OWNER AND c.TABLE_NAME = cc.TABLE_NAME AND c.COLUMN_NAME = cc.COLUMN_NAME
        WHERE c.TABLE_NAME = :tn AND c.OWNER = USER
        ORDER BY c.COLUMN_ID
    """
    cur = conn.cursor()
    cur.execute(cols_sql, tn=table_name)
    col_rows = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
    cur.close()

    if not col_rows:
        raise ValueError(f"Table not found or no columns: {table_name}")

    # Table comment
    cur = conn.cursor()
    cur.execute(
        "SELECT COMMENTS FROM ALL_TAB_COMMENTS WHERE TABLE_NAME = :tn AND OWNER = USER",
        tn=table_name,
    )
    tab_comment_row = cur.fetchone()
    table_comment = (tab_comment_row[0] or "").strip() if tab_comment_row else ""
    cur.close()

    # B. Constraints P and R
    cur = conn.cursor()
    cur.execute(
        """
        SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE, R_OWNER, R_CONSTRAINT_NAME
        FROM ALL_CONSTRAINTS
        WHERE TABLE_NAME = :tn AND OWNER = USER AND CONSTRAINT_TYPE IN ('P','R')
        ORDER BY CONSTRAINT_TYPE, CONSTRAINT_NAME
        """,
        tn=table_name,
    )
    constraint_rows = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
    cur.close()

    # C. FK column -> parent table.column
    cur = conn.cursor()
    cur.execute(
        """
        SELECT child.COLUMN_NAME AS CHILD_COL, parent_ac.TABLE_NAME AS PARENT_TABLE, parent_cc.COLUMN_NAME AS PARENT_COL
        FROM ALL_CONSTRAINTS child_ac
        JOIN ALL_CONS_COLUMNS child ON child_ac.OWNER = child.OWNER AND child_ac.CONSTRAINT_NAME = child.CONSTRAINT_NAME
        JOIN ALL_CONSTRAINTS parent_ac ON child_ac.R_OWNER = parent_ac.OWNER AND child_ac.R_CONSTRAINT_NAME = parent_ac.CONSTRAINT_NAME
        JOIN ALL_CONS_COLUMNS parent_cc ON parent_ac.OWNER = parent_cc.OWNER AND parent_ac.CONSTRAINT_NAME = parent_cc.CONSTRAINT_NAME AND child.POSITION = parent_cc.POSITION
        WHERE child_ac.TABLE_NAME = :tn AND child_ac.OWNER = USER AND child_ac.CONSTRAINT_TYPE = 'R'
        ORDER BY child.CONSTRAINT_NAME, child.POSITION
        """,
        tn=table_name,
    )
    fk_rows = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
    cur.close()

    # D. Annotations (Oracle 23c+; view may not exist)
    annotation_rows: list[dict[str, Any]] = []
    annotations_available = True
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COLUMN_NAME, ANNOTATION_NAME, ANNOTATION_VALUE
            FROM ALL_ANNOTATIONS_USAGE
            WHERE OBJECT_NAME = :tn AND OBJECT_TYPE = 'TABLE'
            ORDER BY COLUMN_NAME NULLS FIRST, ANNOTATION_NAME
            """,
            tn=table_name,
        )
        annotation_rows = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
        cur.close()
    except oracledb.DatabaseError:
        annotations_available = False  # View not available (e.g. pre-23c)

    table_anns = [a for a in annotation_rows if a.get("COLUMN_NAME") is None]
    col_anns: dict[str, list[dict[str, Any]]] = {}
    for a in annotation_rows:
        if a.get("COLUMN_NAME") is not None:
            col = a["COLUMN_NAME"]
            col_anns.setdefault(col, []).append(a)

    # Build Markdown (human-readable, neatly formatted with explicit headings)
    lines = [
        "",
        f"**Table Name:** {table_name}",
        "",
    ]
    if table_comment:
        wrapped = _wrap_text(table_comment)
        lines.append("**Table Description:**")
        lines.append("")
        for para in wrapped.split("\n"):
            lines.append(para)
        lines.append("")
    if annotations_available and table_anns:
        lines.append("**Table Annotations:**")
        lines.append("")
        for a in table_anns:
            lines.append(f"- {a.get('ANNOTATION_NAME')} = {a.get('ANNOTATION_VALUE') or '(empty)'}")
        lines.append("")
    lines.append("**Column Metadata:**")
    lines.append("")
    # Build table so each row is on its own line; wrap in code block so UI preserves newlines
    table_rows = [
        "Column Name | Data Type | Nullable | Description | Annotations",
        "------------|-----------|----------|------------|-------------",
    ]
    for r in col_rows:
        name = r.get("COLUMN_NAME") or ""
        dtype = _format_data_type(r)
        nullable = (r.get("NULLABLE") or "Y").upper()
        nullable_h = "No" if nullable == "N" else "Yes"
        comment_raw = (r.get("COMMENTS") or "").replace("\n", " ").strip()
        comment = comment_raw if comment_raw else "(No description)"
        comment_esc = comment.replace("|", "(pipe)")  # keep table columns intact if description contains |
        if not annotations_available:
            ann_cell = "N/A"
        else:
            anns = col_anns.get(name, [])
            if not anns:
                ann_cell = "-"
            else:
                parts = [f"{a.get('ANNOTATION_NAME')}={a.get('ANNOTATION_VALUE') or '(empty)'}" for a in anns]
                ann_cell = ", ".join(parts).replace("|", "(pipe)")
        table_rows.append(f"{name} | {dtype} | {nullable_h} | {comment_esc} | {ann_cell}")
    lines.append("```")
    lines.append("\n".join(table_rows))
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")

    pk_constraints = [r for r in constraint_rows if (r.get("CONSTRAINT_TYPE") or "") == "P"]
    if pk_constraints:
        lines.append("### Primary key")
        lines.append("")
        for r in pk_constraints:
            cname = r.get("CONSTRAINT_NAME") or ""
            # PK columns for this constraint
            cur = conn.cursor()
            cur.execute(
                "SELECT COLUMN_NAME FROM ALL_CONS_COLUMNS WHERE OWNER = USER AND CONSTRAINT_NAME = :cn ORDER BY POSITION",
                cn=cname,
            )
            pk_cols = [row[0] for row in cur.fetchall()]
            cur.close()
            lines.append(f"- **{cname}**: {', '.join(pk_cols)}")
        lines.append("")
        lines.append("---")
        lines.append("")

    if fk_rows:
        lines.append("### Foreign keys")
        lines.append("")
        for r in fk_rows:
            child_col = r.get("CHILD_COL") or ""
            parent_t = r.get("PARENT_TABLE") or ""
            parent_c = r.get("PARENT_COL") or ""
            lines.append(f"- `{child_col}` → **{parent_t}.{parent_c}**")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def get_table_metadata(table_names: str) -> str:
    """
    Retrieves detailed structural information, foreign keys, and annotations for
    specific tables to help the LLM write correct SQL.
    Input: comma-separated list of table names (string). Output: structured text/markdown (columns, types, relationships).
    """
    if not table_names:
        return "No tables requested."

    pool = _get_oracle_pool()
    if pool is None:
        if _oracle_pool_error:
            return f"Could not connect to Oracle: {_oracle_pool_error}"
        return (
            "Oracle is not configured. Set environment variables: "
            "**ORACLE_NL2SQL_DSN**, **ORACLE_NL2SQL_USER**, **ORACLE_NL2SQL_PASSWORD** "
            "(and optionally **ORACLE_NL2SQL_WALLET_PATH**, **ORACLE_NL2SQL_WALLET_PASSWORD** for wallet/mTLS)."
        )

    # Support both comma-separated string and list-like values defensively.
    if isinstance(table_names, str):
        raw_names = [
            part.strip()
            for part in table_names.split(",")
            if part.strip()
        ]
    elif isinstance(table_names, list):
        raw_names = [(str(name) or "").strip() for name in table_names if str(name).strip()]
    else:
        raw_names = [str(table_names).strip()] if str(table_names).strip() else []

    if not raw_names:
        return "No tables requested."

    sections: list[str] = []
    for raw_name in raw_names:
        table_name = (raw_name or "").strip().upper()
        if not table_name:
            continue
        try:
            conn = pool.acquire()
            try:
                section = _fetch_table_metadata(conn, table_name)
                sections.append(section)
            finally:
                conn.close()
        except Exception as e:
            sections.append(f"## Table: {table_name}\n\n*Error: table not found or no access.* ({e})")

    if not sections:
        return "No valid table names provided."
    return "\n\n---\n\n".join(sections)


# --- Tool 4: get_sample_data ---

SAMPLE_DATA_GUIDANCE = (
    "Dates in this sample are formatted as ISO strings (YYYY-MM-DD) for JSON compatibility. "
    "In generated Oracle SQL, you MUST treat these columns as DATE/TIMESTAMP types and use "
    "TO_DATE() or ANSI date literals (DATE '2023-01-01'). DO NOT compare them as raw strings."
)


def _serialize_cell(value: Any) -> Any:
    """Make a cell value JSON-serializable: dates to ISO strings, Decimal to int/float."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    return value


@mcp.tool()
def get_sample_data(table_name: str, limit: int = 3) -> dict[str, Any]:
    """
    Fetches actual row values from a table to resolve ambiguity (e.g. is 'Country'
    stored as 'USA' or 'US'?). Use before writing or refining SQL.
    """
    raw = (table_name or "").strip()
    if not re.match(r"^[A-Za-z0-9_]+$", raw):
        return {
            "data": [],
            "guidance": SAMPLE_DATA_GUIDANCE,
            "error": "Invalid table name: only letters, numbers, and underscores allowed.",
        }
    table_name = raw.upper()
    limit = max(1, min(int(limit), 100))

    pool = _get_oracle_pool()
    if pool is None:
        err = _oracle_pool_error or "Oracle is not configured."
        return {"data": [], "guidance": SAMPLE_DATA_GUIDANCE, "error": err}

    try:
        conn = pool.acquire()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS
                WHERE TABLE_NAME = :tn AND OWNER = USER
                  AND DATA_TYPE NOT IN ('BLOB', 'CLOB', 'NCLOB', 'BFILE', 'LONG')
                ORDER BY COLUMN_ID
                """,
                tn=table_name,
            )
            col_rows = cur.fetchall()
            cur.close()
            if not col_rows:
                return {
                    "data": [],
                    "guidance": SAMPLE_DATA_GUIDANCE,
                    "error": "Table not found or no non-LOB columns.",
                }
            cols = [r[0] for r in col_rows]
            col_list = ", ".join(f'"{c}"' for c in cols)
            sql = f"SELECT {col_list} FROM {table_name} FETCH FIRST :lim ROWS ONLY"
            cur = conn.cursor()
            cur.execute(sql, lim=limit)
            rows_data = cur.fetchall()
            col_names = [d[0] for d in cur.description]
            cur.close()
            data = [
                {col_names[i]: _serialize_cell(val) for i, val in enumerate(row)}
                for row in rows_data
            ]
            return {"data": data, "guidance": SAMPLE_DATA_GUIDANCE}
        finally:
            conn.close()
    except Exception as e:
        logger.exception("get_sample_data failed: %s", e)
        return {
            "data": [],
            "guidance": SAMPLE_DATA_GUIDANCE,
            "error": str(e),
        }


# --- Tool 5: execute_read_only_sql ---

_READ_ONLY_FORBIDDEN = re.compile(
    r"(?i)\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|EXECUTE)\b"
)


def _validate_read_only_sql(sql: str) -> str | None:
    """Return None if the query passes read-only checks; otherwise a short reason string."""
    s = (sql or "").strip()
    if not s:
        return "query is empty"
    if ";" in sql:
        return "semicolon not allowed (statement chaining forbidden)"
    if not re.search(r"^\s*(SELECT|WITH)\s", sql, re.IGNORECASE):
        return "must start with SELECT or WITH"
    if _READ_ONLY_FORBIDDEN.search(sql):
        return "read-only violation: forbidden keyword"
    return None


@mcp.tool()
def execute_read_only_sql(sql_query: str) -> dict[str, Any]:
    """
    Safely executes read-only SQL and returns results. Only SELECT and WITH
    queries are allowed; results are capped at 100 rows. Returns a JSON result
    set or a structured error message for LLM self-correction.

    IMPORTANT: Unless the user explicitly requests a specific number of rows,
    always use FETCH FIRST 100 ROWS ONLY (not 10) in Oracle SQL queries.
    Do NOT add an arbitrary small limit like FETCH FIRST 10 ROWS ONLY.
    """
    sql = (sql_query or "").strip()
    validation_fail = _validate_read_only_sql(sql_query)
    if validation_fail is not None:
        return {
            "status": "error",
            "code": None,
            "message": f"Read-only check failed: {validation_fail}",
        }

    pool = _get_oracle_pool()
    if pool is None:
        return {
            "status": "error",
            "code": None,
            "message": _oracle_pool_error or "Oracle is not configured.",
        }

    try:
        conn = pool.acquire()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchmany(100)
            col_names = [d[0] for d in cur.description] if cur.description else []
            cur.close()
            data = [
                {col_names[i]: _serialize_cell(val) for i, val in enumerate(row)}
                for row in rows
            ]
            return {"status": "ok", "columns": col_names, "data": data}
        finally:
            conn.close()
    except oracledb.Error as e:
        code: Any = None
        message = str(e)
        if len(e.args) > 0:
            obj = e.args[0]
            if hasattr(obj, "code"):
                code = obj.code
            if hasattr(obj, "message"):
                message = obj.message
        return {"status": "error", "code": code, "message": message}


# --- Tool 6: analyze_data_insights ---

def _build_data_briefing(df: pd.DataFrame, numeric_cols: list[str], date_cols: list[str]) -> str:
    """Build the Markdown Data Briefing from a prepared DataFrame and column lists."""
    lines = ["### AUTOMATED DATA ANALYSIS REPORT", ""]
    n_rows, n_cols = df.shape
    lines.append("**1. Overview:**")
    lines.append(f"- {n_rows} records analyzed.")
    lines.append(f"- Columns: {', '.join(df.columns.tolist())}.")
    lines.append("")

    # Section 2: Key Statistics (numeric only)
    lines.append("**2. Key Statistics:**")
    if not numeric_cols:
        lines.append("No numeric columns for statistics.")
    else:
        desc = df[numeric_cols].describe()
        header = "| Column | Count | Mean | Std Dev | Min | 25% | 50% | 75% | Max |"
        sep = "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        lines.append(header)
        lines.append(sep)
        for col in numeric_cols:
            r = desc[col]
            count = int(r.get("count", 0))
            mean = r.get("mean", np.nan)
            std = r.get("std", np.nan)
            mn = r.get("min", np.nan)
            p25 = r.get("25%", np.nan)
            p50 = r.get("50%", np.nan)
            p75 = r.get("75%", np.nan)
            mx = r.get("max", np.nan)
            def _fmt(x: float) -> str:
                if np.isnan(x):
                    return "—"
                if abs(x) >= 1e4 or (abs(x) < 0.01 and x != 0):
                    return f"{x:.2e}"
                return f"{x:.2f}"
            lines.append(
                f"| {col} | {count} | {_fmt(mean)} | {_fmt(std)} | {_fmt(mn)} | {_fmt(p25)} | {_fmt(p50)} | {_fmt(p75)} | {_fmt(mx)} |"
            )
    lines.append("")

    # Section 3: Anomalies (Z-score > 3)
    lines.append("**3. Anomalies Detected (Z-Score > 3):**")
    # per_col: col -> (total_count, list of (row_id, value, z, context) up to 5 examples)
    per_col_outliers: dict[str, tuple[int, list[tuple[int, float, float, str]]]] = {}
    for col in numeric_cols:
        s = df[col]
        mean, std = s.mean(), s.std()
        if std == 0 or np.isnan(std):
            continue
        z = (s - mean) / std
        mask = np.abs(z) > 3
        indices = np.where(mask)[0]
        total = len(indices)
        examples: list[tuple[int, float, float, str]] = []
        for idx in indices[:5]:
            row_id = int(idx)
            val = float(s.iloc[idx])
            z_val = float(z.iloc[idx])
            context_parts = [
                f"{c}={df[c].iloc[idx]}"
                for c in df.columns
                if c != col and df[c].iloc[idx] is not None and pd.notna(df[c].iloc[idx])
            ][:5]
            context = ", ".join(str(p) for p in context_parts) if context_parts else "—"
            examples.append((row_id, val, z_val, context))
        if total > 0:
            per_col_outliers[col] = (total, examples)
    if not per_col_outliers:
        lines.append("No significant anomalies detected (Z-Score > 3).")
    else:
        for col, (total, examples) in per_col_outliers.items():
            lines.append(f"- **{col}:** {total} outlier(s) found.")
            for row_id, val, z_val, context in examples:
                lines.append(f"  - Row {row_id}: Value = {val:.2f} (Z-Score: {z_val:.2f}). Context: {context}.")
    lines.append("")

    # Section 4: Trends (time series)
    lines.append("**4. Time Series Analysis:**")
    if not date_cols or not numeric_cols:
        lines.append("No date column or no numeric column available for trend analysis.")
    else:
        date_col = date_cols[0]
        value_col = numeric_cols[0]
        s_date = pd.to_datetime(df[date_col], errors="coerce")
        valid = s_date.notna()
        if valid.sum() < 2:
            lines.append("Insufficient valid dates for trend analysis.")
        else:
            d = df.loc[valid].copy()
            d = d.sort_values(date_col)
            d["_period"] = d[date_col].dt.to_period("M")
            period_means = d.groupby("_period", observed=True)[value_col].mean()
            if len(period_means) < 2:
                lines.append("Insufficient periods for trend.")
            else:
                x = np.arange(len(period_means))
                y = period_means.values.astype(float)
                slope = float(np.polyfit(x, y, 1)[0])
                start_d = d[date_col].min()
                end_d = d[date_col].max()
                lines.append(f"- Date Range: {start_d} to {end_d}.")
                if slope > 1e-6:
                    lines.append(f"- **{value_col}:** Strong Positive Trend.")
                elif slope < -1e-6:
                    lines.append(f"- **{value_col}:** Strong Negative Trend.")
                else:
                    lines.append(f"- **{value_col}:** Flat trend.")
                # Optional seasonality: which quarter has highest mean
                d["_q"] = d[date_col].dt.quarter
                q_means = d.groupby("_q", observed=True)[value_col].mean()
                if len(q_means) >= 2:
                    peak_q = int(q_means.idxmax())
                    lines.append(f"- **Seasonality:** Peak values observed in Q{peak_q}.")
    lines.append("")

    # Section 5: Key Correlations
    lines.append("**5. Key Correlations:**")
    if len(numeric_cols) < 2:
        lines.append("Need at least two numeric columns for correlation.")
    else:
        corr = df[numeric_cols].corr()
        strong: list[tuple[str, str, float]] = []
        for i, a in enumerate(numeric_cols):
            for b in numeric_cols[i + 1 :]:
                r = corr.loc[a, b]
                if np.isnan(r):
                    continue
                if abs(r) > 0.8:
                    strong.append((a, b, float(r)))
        if not strong:
            lines.append("No strong correlations (|r| > 0.8) found.")
        else:
            for a, b, r in strong:
                direction = "positive" if r > 0 else "negative"
                lines.append(f"- Strong {direction} correlation ({r:.2f}) between '{a}' and '{b}'.")
    return "\n".join(lines)


@mcp.tool()
def analyze_data_insights(data_rows: list[dict[str, Any]] | str, user_question: str) -> str:
    """
    Performs statistical analysis (describe, correlations, Z-score outliers,
    time-series trend) on SQL result rows and returns a Markdown 'Data Briefing'
    for the LLM to use when writing its final response.
    Accepts data_rows as a list of dicts or a JSON string of that list.
    """
    rows: list[dict[str, Any]]
    if isinstance(data_rows, str):
        try:
            rows = json.loads(data_rows)
        except (json.JSONDecodeError, TypeError):
            return "### AUTOMATED DATA ANALYSIS REPORT\n\nInvalid data_rows: expected a list or JSON array string."
        if not isinstance(rows, list):
            return "### AUTOMATED DATA ANALYSIS REPORT\n\nInvalid data_rows: expected a list or JSON array string."
    else:
        rows = data_rows if isinstance(data_rows, list) else []
    if len(rows) == 0:
        return "### AUTOMATED DATA ANALYSIS REPORT\n\nNo data provided for analysis."

    try:
        df = pd.DataFrame(rows)
        if df.empty:
            return "### AUTOMATED DATA ANALYSIS REPORT\n\nNo data provided for analysis."

        numeric_cols: list[str] = []
        for col in df.columns:
            ser = pd.to_numeric(df[col], errors="coerce")
            if ser.notna().any():
                df[col] = ser
                numeric_cols.append(col)
            else:
                # Try datetime (e.g. ISO strings from execute_read_only_sql)
                as_dt = pd.to_datetime(df[col], errors="coerce")
                if as_dt.notna().any():
                    df[col] = as_dt

        date_cols = [
            c
            for c in df.columns
            if pd.api.types.is_datetime64_any_dtype(df[c])
        ]
        # Re-identify numeric columns after coercion (in case we overwrote)
        numeric_cols = [
            c
            for c in df.columns
            if c not in date_cols and pd.api.types.is_numeric_dtype(df[c])
        ]

        return _build_data_briefing(df, numeric_cols, date_cols)
    except Exception as e:
        logger.exception("analyze_data_insights failed: %s", e)
        return f"### AUTOMATED DATA ANALYSIS REPORT\n\n**Error:** Analysis failed: {e!s}"


# --- Tool 7: generate_vega_spec ---

_VEGA_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"
_MAX_VEGA_ROWS = 2000


def _vega_tooltip_fields(
    chart_type: str,
    x_field: str,
    y_field: str,
    category_field: str | None = None,
    secondary_y_field: str | None = None,
) -> list[dict[str, Any]]:
    x_type = "nominal" if chart_type in ("bar", "pie", "stacked_bar", "grouped_bar") else "ordinal"
    tooltip: list[dict[str, Any]] = [
        {"field": x_field, "type": x_type},
        {"field": y_field, "type": "quantitative"},
    ]
    if category_field:
        tooltip.append({"field": category_field, "type": "nominal"})
    if secondary_y_field:
        tooltip.append({"field": secondary_y_field, "type": "quantitative"})
    return tooltip


@mcp.tool()
def generate_vega_spec(
    data: list[dict[str, Any]] | str,
    chart_type: Literal["bar", "line", "pie", "scatter", "stacked_bar", "grouped_bar", "combo"],
    x_field: str,
    y_field: str,
    title: str = "Data Visualization",
    category_field: str | None = None,
    secondary_y_field: str | None = None,
) -> str:
    """
    Generates a complete, self-contained Vega-Lite v5 JSON specification for common chart types.
    Returns a JSON string that the web app can parse and render. Use when the user asks for a chart.
    For complex visualizations (e.g. Sankey, geospatial), the LLM should skip this tool and generate
    Vega-Lite JSON directly in the response.
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            data = []
    if not isinstance(data, list):
        data = []
    values = data[: _MAX_VEGA_ROWS]
    truncated = len(data) > _MAX_VEGA_ROWS
    spec: dict[str, Any] = {
        "$schema": _VEGA_SCHEMA,
        "title": title or "Data Visualization",
        "data": {"values": values},
    }
    if truncated:
        spec["meta"] = {"truncated": True, "total_rows": len(data), "displayed_rows": _MAX_VEGA_ROWS}
    spec["width"] = "container"
    spec["height"] = 400

    tooltip = _vega_tooltip_fields(chart_type, x_field, y_field, category_field, secondary_y_field)

    if chart_type == "pie":
        spec["mark"] = {"type": "arc", "tooltip": True}
        spec["encoding"] = {
            "theta": {"field": y_field, "type": "quantitative"},
            "color": {"field": x_field, "type": "nominal"},
            "order": {"field": y_field, "sort": "descending"},
            "tooltip": _vega_tooltip_fields("pie", x_field, y_field, category_field),
        }
        return json.dumps(spec)

    if chart_type == "combo":
        if not secondary_y_field:
            spec["description"] = "Combo chart requires secondary_y_field."
            spec["mark"] = "bar"
            spec["encoding"] = {"x": {"field": x_field}, "y": {"field": y_field}}
            return json.dumps(spec)
        spec["layer"] = [
            {
                "mark": "bar",
                "encoding": {
                    "x": {"field": x_field, "type": "nominal"},
                    "y": {"field": y_field, "type": "quantitative"},
                    "tooltip": tooltip,
                },
            },
            {
                "mark": {"type": "line", "point": True},
                "encoding": {
                    "x": {"field": x_field, "type": "nominal"},
                    "y": {"field": secondary_y_field, "type": "quantitative"},
                    "color": {"value": "#e74c3c"},
                    "tooltip": _vega_tooltip_fields("combo", x_field, secondary_y_field, category_field),
                },
            },
        ]
        spec["resolve"] = {"scale": {"y": "independent"}}
        spec["selection"] = {"grid": {"type": "interval", "bind": "scales"}}
        return json.dumps(spec)

    mark = "bar"
    if chart_type == "line":
        mark = "line"
    elif chart_type == "scatter":
        mark = "point"

    encoding: dict[str, Any] = {
        "x": {"field": x_field, "type": "nominal"},
        "y": {"field": y_field, "type": "quantitative"},
        "tooltip": tooltip,
    }
    if category_field:
        encoding["color"] = {"field": category_field, "type": "nominal"}
        if chart_type == "stacked_bar":
            encoding["x"] = {"field": x_field, "type": "nominal"}
            encoding["y"] = {"field": y_field, "type": "quantitative"}
            # Vega-Lite stacks by default when color is set on bar
        elif chart_type == "grouped_bar":
            encoding["xOffset"] = {"field": category_field}

    spec["mark"] = mark
    spec["encoding"] = encoding
    spec["selection"] = {"grid": {"type": "interval", "bind": "scales"}}
    return json.dumps(spec)


if __name__ == "__main__":
    # Run with SSE on a dedicated port for Talk2Everything (e.g. 8082 to avoid clashing with Agentic Tools on 8081)
    mcp.run(transport="sse", host="0.0.0.0", port=8082)
