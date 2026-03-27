import pytest
import json
from app.mcp.server import MCPServer
from app.mcp.tools import get_tool_definitions


def test_tool_definitions():
    tools = get_tool_definitions()
    assert isinstance(tools, list)
    assert len(tools) > 10  # we have many tools
    # Each tool has required fields
    for tool in tools:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool


def test_tool_names():
    tools = get_tool_definitions()
    names = [t["name"] for t in tools]
    assert "kernel_status" in names
    assert "system_info" in names
    assert "file_read" in names
    assert "shell_exec" in names
    assert "http_request" in names
    assert "monitor_metrics" in names
    assert "log_search" in names
    assert "ssh_exec" in names
    assert "agent_list" in names


def test_mcp_server_init():
    server = MCPServer()
    assert server is not None


def test_handle_initialize():
    server = MCPServer()
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
    }
    response = server.handle_message(json.dumps(request))
    data = json.loads(response)
    assert data["id"] == 1
    assert "result" in data
    assert data["result"]["protocolVersion"] == "2024-11-05"
    assert "serverInfo" in data["result"]


def test_handle_tools_list():
    server = MCPServer()
    # Initialize first
    server.handle_message(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}},
    }))
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
    }
    response = server.handle_message(json.dumps(request))
    data = json.loads(response)
    assert "result" in data
    assert "tools" in data["result"]
    assert len(data["result"]["tools"]) > 10


def test_handle_tools_call_kernel_status():
    server = MCPServer()
    server.handle_message(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}},
    }))
    request = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "kernel_status",
            "arguments": {},
        },
    }
    response = server.handle_message(json.dumps(request))
    data = json.loads(response)
    assert "result" in data
    assert "content" in data["result"]
    content = data["result"]["content"][0]
    assert content["type"] == "text"
    # Should contain kernel status info
    text = json.loads(content["text"])
    assert "state" in text


def test_handle_tools_call_system_info():
    server = MCPServer()
    server.handle_message(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}},
    }))
    request = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "system_info", "arguments": {}},
    }
    response = server.handle_message(json.dumps(request))
    data = json.loads(response)
    text = json.loads(data["result"]["content"][0]["text"])
    assert "hostname" in text
    assert "cpu_count" in text


def test_handle_unknown_method():
    server = MCPServer()
    request = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "unknown/method",
    }
    response = server.handle_message(json.dumps(request))
    data = json.loads(response)
    assert "error" in data


def test_handle_invalid_json():
    server = MCPServer()
    response = server.handle_message("not valid json")
    data = json.loads(response)
    assert "error" in data
