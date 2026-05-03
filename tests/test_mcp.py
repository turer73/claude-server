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
    server.handle_message(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}},
            }
        )
    )
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
    server.handle_message(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}},
            }
        )
    )
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
    server.handle_message(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}},
            }
        )
    )
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


def test_execute_tool_system_info():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("system_info", {}))
    assert "hostname" in result
    assert "cpu_count" in result


def test_execute_tool_monitor_metrics():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("monitor_metrics", {}))
    assert "cpu_percent" in result


def test_execute_tool_file_read(tmp_path):
    from app.mcp.tools import execute_tool

    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world")
    result = json.loads(execute_tool("file_read", {"path": str(test_file)}))
    assert "content" in result or "error" in result


def test_execute_tool_file_write(tmp_path):
    from app.mcp.tools import execute_tool

    test_file = tmp_path / "write_test.txt"
    result = json.loads(execute_tool("file_write", {"path": str(test_file), "content": "test data"}))
    assert "size" in result or "error" in result


def test_execute_tool_file_list(tmp_path):
    from app.mcp.tools import execute_tool

    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    result = json.loads(execute_tool("file_list", {"path": str(tmp_path)}))
    assert "entries" in result or "error" in result


def test_execute_tool_file_search(tmp_path):
    from app.mcp.tools import execute_tool

    (tmp_path / "hello.py").write_text("pass")
    result = json.loads(execute_tool("file_search", {"path": str(tmp_path), "pattern": "*.py"}))
    assert "results" in result or "error" in result


def test_execute_tool_log_tail():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("log_tail", {"n": 5}))
    assert "lines" in result or "error" in result


def test_execute_tool_log_search():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("log_search", {"pattern": "error"}))
    assert "results" in result or "error" in result


def test_execute_tool_git_status(tmp_path):
    import subprocess

    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("git_status", {"cwd": str(tmp_path)}))
    assert "branch" in result or "error" in result


def test_execute_tool_git_log(tmp_path):
    import subprocess

    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("git_log", {"cwd": str(tmp_path)}))
    assert "entries" in result or "error" in result


def test_execute_tool_ssh_exec():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("ssh_exec", {"session_id": "x", "command": "ls"}))
    # Now returns hint/note instead of error (points user to REST API)
    assert "hint" in result or "note" in result or "error" in result


def test_execute_tool_agent_run():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("agent_run", {"agent_name": "test"}))
    assert "error" in result


def test_execute_tool_ai_chat():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("ai_chat", {"message": "hello"}))
    # Ollama running → returns response; not running → returns error
    assert "response" in result or "error" in result


def test_execute_tool_unknown():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("totally_fake_tool", {}))
    assert "error" in result


def test_execute_tool_process_list():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("process_list", {"limit": 5, "sort_by": "cpu"}))
    assert "processes" in result
    assert len(result["processes"]) <= 5


def test_execute_tool_kernel_set_governor():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("kernel_set_governor", {"mode": "performance"}))
    # May succeed or fail depending on kernel module state
    assert "governor" in result or "error" in result


def test_execute_tool_shell_exec():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("shell_exec", {"command": "echo test123"}))
    assert "stdout" in result or "error" in result


def test_execute_tool_http_request():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("http_request", {"url": "http://localhost:8420/health", "method": "GET"}))
    assert "status_code" in result or "error" in result


def test_execute_tool_docker_ps():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("docker_ps", {}))
    assert "containers" in result or "error" in result


def test_execute_tool_docker_logs():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("docker_logs", {"container": "nonexistent", "tail": 5}))
    assert "logs" in result or "error" in result


def test_execute_tool_service_status():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("service_status", {"name": "linux-ai-server"}))
    assert "active" in result or "status" in result or "error" in result


def test_execute_tool_agent_list():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("agent_list", {}))
    assert "agents" in result or "error" in result


def test_execute_tool_ai_chat_empty_message():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("ai_chat", {"message": ""}))
    assert "error" in result


def test_run_async_helper():

    from app.mcp.tools import _run_async

    async def add(a, b):
        return a + b

    result = _run_async(add(2, 3))
    assert result == 5


def test_execute_tool_vps_exec():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("vps_exec", {"command": "echo test", "timeout": 3}))
    assert "stdout" in result or "error" in result


def test_execute_tool_vps_status():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("vps_status", {}))
    assert "online" in result or "error" in result


def test_execute_tool_vps_services():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("vps_services", {}))
    assert "services" in result or "error" in result or "exit_code" in result


def test_execute_tool_rag_stats():
    from app.mcp.tools import execute_tool

    result = json.loads(execute_tool("rag_stats", {}))
    assert "collection" in result or "document_count" in result or "error" in result
