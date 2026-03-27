"""MCP server -- JSON-RPC based, stdio transport."""

from __future__ import annotations

import json
import sys

from app.mcp.tools import get_tool_definitions, execute_tool


class MCPServer:
    """Lightweight MCP-compatible server using JSON-RPC 2.0."""

    def __init__(self) -> None:
        self._initialized = False
        self._server_info = {
            "name": "linux-ai-server",
            "version": "0.1.0",
        }

    def handle_message(self, raw: str) -> str:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            })

        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            return self._handle_initialize(msg_id, params)
        elif method == "notifications/initialized":
            return ""  # notification, no response
        elif method == "tools/list":
            return self._handle_tools_list(msg_id)
        elif method == "tools/call":
            return self._handle_tools_call(msg_id, params)
        elif method == "ping":
            return json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        else:
            return json.dumps({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })

    def _handle_initialize(self, msg_id: int, params: dict) -> str:
        self._initialized = True
        return json.dumps({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": self._server_info,
            },
        })

    def _handle_tools_list(self, msg_id: int) -> str:
        tools = get_tool_definitions()
        return json.dumps({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": tools},
        })

    def _handle_tools_call(self, msg_id: int, params: dict) -> str:
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        result_text = execute_tool(name, arguments)
        return json.dumps({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": result_text}],
            },
        })

    def run_stdio(self) -> None:
        """Run server on stdio (for Claude integration)."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            response = self.handle_message(line)
            if response:
                sys.stdout.write(response + "\n")
                sys.stdout.flush()
