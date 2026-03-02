"""
MCP server exposing standard and essential tools for any agentic solution:
context (get_current_time), logic (calculate_math_expression), reader (fetch_url_content).
Demo/chaining tools (add, echo, get_weather, banking) are commented out.
"""
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import os
from pathlib import Path
from bs4 import BeautifulSoup
from fastmcp import FastMCP
from dotenv import load_dotenv

# Load .env.agentic_tools for independent service configuration.
_env_filename = ".env.agentic_tools"
_custom_path = os.environ.get("AGENTIC_TOOLS_CONFIG_PATH")

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
    print(f"Loading configuration from {_env_path}")
    load_dotenv(_env_path)
else:
    print(f"Configuration file {_env_filename} not found. Relying on existing environment variables.")

# Truncation limit for URL content to avoid blowing LLM context
FETCH_URL_MAX_CHARS = 8000

mcp = FastMCP("Agentic Tools")

# --- ORIGINAL TOOLS (commented out) ---
#
# @mcp.tool()
# def add(a: int, b: int) -> int:
#     """Add two numbers"""
#     return a + b
#
#
# @mcp.tool()
# def echo(message: str) -> str:
#     """Echo a message back"""
#     return f"Echo: {message}"
#
#
# @mcp.tool()
# def get_weather(city: str, date: str) -> str:
#     """Get the weather for a specific city on a specific date."""
#     city_lower = city.lower()
#     if "london" in city_lower:
#         return f"Weather in {city} on {date}: Rainy, 15°C"
#     elif "francisco" in city_lower:
#         return f"Weather in {city} on {date}: Foggy, 18°C"
#     elif "mumbai" in city_lower:
#         return f"Weather in {city} on {date}: Sunny, 32°C"
#     else:
#         return f"Weather in {city} on {date}: Partly Cloudy, 25°C"
#
#
# --- CHAINING TEST TOOLS (BANKING SCENARIO) (commented out) ---
#
#
# @mcp.tool()
# def get_customer_id(name: str) -> str:
#     """
#     Look up the unique Customer ID for a given name.
#     ALWAYS use this before checking balances or transferring funds.
#     """
#     name_map = {
#         "alice": "CUST-001",
#         "bob": "CUST-002",
#         "charlie": "CUST-003"
#     }
#     return name_map.get(name.lower(), "UNKNOWN_CUSTOMER")
#
#
# @mcp.tool()
# def get_account_balance(customer_id: str) -> str:
#     """
#     Get the account balance. Requires a valid Customer ID (starts with CUST-).
#     """
#     if not customer_id.startswith("CUST-"):
#         return "Error: Invalid Customer ID format. Please look up the ID first."
#
#     balances = {
#         "CUST-001": "$5,000",  # Alice
#         "CUST-002": "$150",    # Bob
#         "CUST-003": "$0"       # Charlie
#     }
#     return balances.get(customer_id, "Error: Account not found.")
#
#
# @mcp.tool()
# def get_last_transaction(customer_id: str) -> str:
#     """
#     Get the last transaction details. Requires a valid Customer ID.
#     """
#     if customer_id == "CUST-001":
#         return "2024-02-20: Deposit +$1000"
#     elif customer_id == "CUST-002":
#         return "2024-02-19: Withdrawal -$50"
#     return "No recent transactions"
#
#
# @mcp.tool()
# def transfer_funds(source_id: str, destination_id: str, amount: int) -> str:
#     """
#     Transfer money between two accounts.
#     Requires Valid Customer IDs for both source and destination.
#     """
#     if not source_id.startswith("CUST-") or not destination_id.startswith("CUST-"):
#         return "Error: Invalid ID. You must look up both customer IDs first."
#
#     return f"Success: Transferred ${amount} from {source_id} to {destination_id}."


# --- STANDARD AGENTIC TOOLS ---


@mcp.tool()
def get_current_time(timezone: str = "UTC") -> dict:
    """
    Returns the current date, time, day of the week, and timezone.
    Use when the user asks about 'now', 'today', market hours, or days left in a period.
    """
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    # Cross-platform 12-hour time: avoid %-I (not on Windows)
    hour = now.hour % 12 or 12
    minute = now.minute
    am_pm = "AM" if now.hour < 12 else "PM"
    time_str = f"{hour}:{minute:02d} {am_pm}"
    human_readable = f"{now.strftime('%A, %B %d, %Y')}, {time_str}"
    return {
        "iso_timestamp": now.isoformat(),
        "human_readable": human_readable,
        "day_of_week": now.strftime("%A"),
        "timezone": timezone,
    }


def _is_safe_math_expression(expression: str) -> bool:
    """Allow only digits, +, -, *, /, (, ), ., comma, spaces, and the identifiers pow and sqrt."""
    allowed_chars = set("0123456789+-*/()., ")
    for c in expression:
        if c not in allowed_chars and not c.isalpha():
            return False
    # Tokenize: split on non-alphanumeric, keep only identifiers
    tokens = re.findall(r"[a-zA-Z_]+|[0-9.]+|[+\-*/()]+", expression)
    allowed_ids = {"pow", "sqrt"}
    for t in tokens:
        if t.isalpha() or "_" in t:
            if t not in allowed_ids:
                return False
    return True


@mcp.tool()
def calculate_math_expression(expression: str):
    """
    Safely evaluates a mathematical string (e.g. 12.5 * 4500 / 100).
    Use for arithmetic, percentages, or currency conversions to avoid LLM number errors.
    """
    expression = expression.strip()
    if not expression:
        return "Error: empty expression"
    if not _is_safe_math_expression(expression):
        return "Error: expression contains disallowed characters or identifiers (only digits, +, -, *, /, (, ), ., pow, sqrt are allowed)"
    safe_dict = {"sqrt": math.sqrt, "pow": pow}
    try:
        result = eval(expression, {"__builtins__": {}}, safe_dict)
    except Exception as e:
        return f"Error evaluating expression: {e}"
    if not isinstance(result, (int, float)):
        return "Error: result is not a number"
    return result


@mcp.tool()
def fetch_url_content(url: str) -> str:
    """
    Fetches and returns the main text content from a single URL (e.g. docs or article).
    Use when the user provides a specific link and wants a summary or instructions.
    """
    if not url.strip().startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://"
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "MCP-AgenticTools/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"Error fetching URL: {e}"
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = " ".join(text.split())
    if len(text) > FETCH_URL_MAX_CHARS:
        text = text[:FETCH_URL_MAX_CHARS] + "\n[... truncated]"
    return text


if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=8081)
