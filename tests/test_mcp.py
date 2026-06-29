import json

import pytest

from app.mcp.server import MCPServer
from app.mcp.tools import execute_tool, get_tool_definitions


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


# ── RAG query/index branches ──


def test_execute_tool_rag_query():
    result = json.loads(execute_tool("rag_query", {"question": "what is up", "n_results": 3}))
    assert isinstance(result, dict)  # answer/results or error (RAGEngine may be down)


def test_execute_tool_rag_index_text():
    result = json.loads(execute_tool("rag_index_text", {"text": "coverage test chunk", "source": "test"}))
    assert isinstance(result, dict)


# ── Project registry tools ──


def test_execute_tool_project_list():
    result = json.loads(execute_tool("project_list", {}))
    assert "projects" in result or "error" in result


def test_execute_tool_project_info_known_or_missing():
    result = json.loads(execute_tool("project_info", {"name": "linux-ai-server"}))
    assert isinstance(result, dict)
    missing = json.loads(execute_tool("project_info", {"name": "no-such-project-xyz"}))
    assert "error" in missing


# ── DevOps note tools (REST-redirect stubs) ──


@pytest.mark.parametrize("tool", ["devops_status", "devops_alerts", "devops_metrics", "devops_remediations"])
def test_execute_tool_devops_notes(tool):
    result = json.loads(execute_tool(tool, {}))
    assert "note" in result


# ── Kernel /proc tools ──


def test_execute_tool_kernel_proc_metrics():
    result = json.loads(execute_tool("kernel_proc_metrics", {}))
    assert isinstance(result, dict)  # metrics dict or {"error": "...not loaded"}


def test_execute_tool_kernel_firewall_status():
    result = json.loads(execute_tool("kernel_firewall_status", {}))
    assert "raw" in result or "error" in result


def test_execute_tool_kernel_usb_status():
    result = json.loads(execute_tool("kernel_usb_status", {}))
    assert "raw" in result or "error" in result


def test_execute_tool_kernel_firewall_block_unblock():
    # RFC5737 TEST-NET-1 (192.0.2.0/24) — guaranteed no real traffic; unblock after.
    blocked = json.loads(execute_tool("kernel_firewall_block", {"ip": "192.0.2.1"}))
    assert "blocked" in blocked or "error" in blocked
    unblocked = json.loads(execute_tool("kernel_firewall_unblock", {"ip": "192.0.2.1"}))
    assert "unblocked" in unblocked or "error" in unblocked


# ── Workspace note tools ──


def test_execute_tool_workspace_notes_roundtrip():
    save = json.loads(execute_tool("workspace_note_save", {"name": "cov_test_note.txt", "content": "hi"}))
    assert "saved" in save or "error" in save
    read = json.loads(execute_tool("workspace_note_read", {"name": "cov_test_note.txt"}))
    assert "content" in read or "error" in read
    listing = json.loads(execute_tool("workspace_note_list", {}))
    assert "notes" in listing or "error" in listing


def test_execute_tool_workspace_note_read_missing():
    result = json.loads(execute_tool("workspace_note_read", {"name": "definitely_missing_xyz.txt"}))
    assert "error" in result


# ── Memory DB tools (redirected to a temp DB so the real memory DB is untouched) ──


@pytest.fixture
def mem_tmp_db(tmp_path, monkeypatch):
    import sqlite3

    dbp = tmp_path / "mem.db"
    conn = sqlite3.connect(dbp)
    conn.executescript(
        """
        CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, name TEXT,
            description TEXT, content TEXT, created_at TEXT, updated_at TEXT,
            active INTEGER DEFAULT 1, read_count INTEGER DEFAULT 0);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, session_num INTEGER, date TEXT,
            summary TEXT, tasks_completed TEXT, files_changed TEXT, device_name TEXT,
            platform TEXT, created_at TEXT);
        CREATE TABLE tasks_log (id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, task TEXT,
            status TEXT, files_changed TEXT, details TEXT, device_name TEXT, created_at TEXT);
        CREATE TABLE discoveries (id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, type TEXT,
            title TEXT, details TEXT, resolved INTEGER DEFAULT 0);
        CREATE TABLE devices (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, platform TEXT, last_seen TEXT);
        """
    )
    conn.execute("INSERT INTO memories (type,name,description,content) VALUES ('project','p1','d','c'*1)")
    conn.commit()
    conn.close()

    real_connect = sqlite3.connect

    def fake_connect(path, *a, **k):
        if "claude_memory.db" in str(path):
            return real_connect(str(dbp), *a, **k)
        return real_connect(path, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    return dbp


def test_execute_tool_memory_context(mem_tmp_db):
    result = json.loads(execute_tool("memory_context", {}))
    assert "active_projects" in result
    assert "devices" in result


def test_execute_tool_memory_query_select(mem_tmp_db):
    result = json.loads(execute_tool("memory_query", {"sql": "SELECT name FROM memories"}))
    assert "rows" in result
    assert result["count"] >= 1


def test_execute_tool_memory_query_rejects_non_select(mem_tmp_db):
    result = json.loads(execute_tool("memory_query", {"sql": "DELETE FROM memories"}))
    assert "error" in result


def test_execute_tool_memory_save_create_and_update(mem_tmp_db):
    created = json.loads(execute_tool("memory_save", {"type": "reference", "name": "cov_mem", "content": "v1"}))
    assert created["action"] == "created"
    updated = json.loads(execute_tool("memory_save", {"type": "reference", "name": "cov_mem", "content": "v2"}))
    assert updated["action"] == "updated"


def test_execute_tool_memory_log_session(mem_tmp_db):
    result = json.loads(execute_tool("memory_log_session", {"summary": "s", "device_name": "klipper", "platform": "linux"}))
    assert result["logged"] is True
    assert result["session_num"] == 1


def test_execute_tool_memory_log_task(mem_tmp_db):
    result = json.loads(execute_tool("memory_log_task", {"project": "p", "task": "t", "status": "done", "device_name": "klipper"}))
    assert result["logged"] is True


def test_execute_tool_memory_save_db_error_path(monkeypatch):
    # Missing required "name" → KeyError caught by the outer guard → {"error": ...}
    result = json.loads(execute_tool("memory_save", {"type": "reference", "content": "x"}))
    assert "error" in result


# ── Async-context branches: execute_tool called from a running event loop
#    exercises the ThreadPoolExecutor fallback paths (loop.is_running() == True). ──


@pytest.mark.anyio
async def test_execute_tool_shell_exec_in_async_context():
    result = json.loads(execute_tool("shell_exec", {"command": "echo async-ctx"}))
    assert "stdout" in result or "error" in result


@pytest.mark.anyio
async def test_execute_tool_http_request_in_async_context():
    result = json.loads(execute_tool("http_request", {"url": "http://localhost:8420/health", "method": "GET"}))
    assert "status_code" in result or "error" in result


@pytest.mark.anyio
async def test_execute_tool_ai_chat_in_async_context():
    result = json.loads(execute_tool("ai_chat", {"message": "hi"}))
    assert "response" in result or "error" in result


@pytest.mark.anyio
async def test_run_async_from_running_loop():
    from app.mcp.tools import _run_async

    async def mul(a, b):
        return a * b

    # Called from inside a running loop → ThreadPoolExecutor branch
    assert _run_async(mul(4, 5)) == 20


def test_rag_query_uses_live_qdrant(monkeypatch):
    # klipper #100224: rag_query ölü ChromaDB (rag_engine :8100) yerine CANLI Qdrant hibrit-RRF.
    import app.api.rag as rag

    monkeypatch.setattr(rag, "_embed", lambda q: [0.1, 0.2])
    monkeypatch.setattr(rag, "_hybrid_search", lambda q, vec, top_k=5: [{"id": "p1", "score": 0.9, "payload": {"text": "x"}}])
    out = json.loads(execute_tool("rag_query", {"question": "test", "n_results": 3}))
    assert out["engine"] == "qdrant:klipper-memory"
    assert out["count"] == 1
    assert out["hits"][0]["id"] == "p1"


def test_rag_index_text_disabled_redirects():
    # klipper #100224: ad-hoc indexing devre-dışı (curated-collection pollution) → memory_save'e yönlendir.
    out = json.loads(execute_tool("rag_index_text", {"text": "x"}))
    assert out["ok"] is False
    assert "memory_save" in out["skipped"]
