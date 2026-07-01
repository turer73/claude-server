"""MCP tools paketi — facade. Geriye-uyum: `from app.mcp.tools import execute_tool,
get_tool_definitions` korunur (handlers=registry-dispatch, definitions=şema)."""

from app.mcp.tools.definitions import get_tool_definitions
from app.mcp.tools.handlers import _run_async, execute_tool

__all__ = ["_run_async", "execute_tool", "get_tool_definitions"]
