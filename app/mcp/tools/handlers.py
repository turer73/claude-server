"""MCP tool definitions -- maps core services to MCP tool format."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from app.core.kernel_bridge import KernelBridge
from app.core.log_manager import LogManager
from app.core.monitor_agent import MonitorAgent
from app.core.system_manager import SystemManager

# MCP file araçlarının kök kapsamı. Bilinçli scope — "/" (tam dosya sistemi)
# DEĞİL: file_write shell-whitelist'i bypass ettiği için /etc, /root vb. yazımı
# açmaz. Proje + log + geçici dizinlerle sınırlı (güvenlik kararı, 2026-06-18).
_MCP_FILE_ROOTS = ["/opt/linux-ai-server", "/var/log", "/tmp"]  # noqa: S108 (kasıtlı kök kapsamı, geçici-dosya değil)


def _run_async(coro: Any) -> Any:
    """Run async coroutine from sync context."""
    import asyncio
    import concurrent.futures

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def handle_kernel_status(arguments: dict[str, Any]) -> str:
    bridge = KernelBridge()
    return json.dumps(bridge.get_status())


def handle_kernel_set_governor(arguments: dict[str, Any]) -> str:
    bridge = KernelBridge()
    bridge.set_governor(arguments["mode"])
    return json.dumps({"governor": arguments["mode"], "success": True})


def handle_system_info(arguments: dict[str, Any]) -> str:
    mgr = SystemManager()
    return json.dumps(mgr.get_system_info())


def handle_process_list(arguments: dict[str, Any]) -> str:
    mgr = SystemManager()
    procs = mgr.get_processes(
        limit=arguments.get("limit", 20),
        sort_by=arguments.get("sort_by", "cpu"),
    )
    return json.dumps({"processes": procs})


def handle_monitor_metrics(arguments: dict[str, Any]) -> str:
    monitor = MonitorAgent()
    return json.dumps(monitor.collect_metrics())


def handle_file_read(arguments: dict[str, Any]) -> str:
    from app.core.file_manager import FileManager

    fm = FileManager(allowed_paths=_MCP_FILE_ROOTS, max_file_size_mb=10)
    result = fm.read_file(
        arguments["path"],
        offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 1000),
    )
    return json.dumps(result)


def handle_file_write(arguments: dict[str, Any]) -> str:
    from app.core.file_manager import FileManager

    fm = FileManager(allowed_paths=_MCP_FILE_ROOTS, max_file_size_mb=10)
    result = fm.write_file(
        arguments["path"],
        arguments["content"],
        mode=arguments.get("mode", "write"),
    )
    return json.dumps(result)


def handle_file_list(arguments: dict[str, Any]) -> str:
    from app.core.file_manager import FileManager

    fm = FileManager(allowed_paths=_MCP_FILE_ROOTS, max_file_size_mb=10)
    entries = fm.list_directory(arguments["path"])
    return json.dumps({"path": arguments["path"], "entries": entries})


def handle_file_search(arguments: dict[str, Any]) -> str:
    from app.core.file_manager import FileManager

    fm = FileManager(allowed_paths=_MCP_FILE_ROOTS, max_file_size_mb=10)
    results = fm.search_files(
        arguments["path"],
        arguments["pattern"],
        max_results=arguments.get("max_results", 50),
    )
    return json.dumps({"results": results})


def handle_shell_exec(arguments: dict[str, Any]) -> str:
    from app.core.config import get_settings
    from app.core.shell_executor import ShellExecutor

    settings = get_settings()
    executor = ShellExecutor(whitelist=settings.shell_whitelist)
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # Already in async context — use thread pool
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(
                asyncio.run,
                executor.execute(arguments["command"], timeout=arguments.get("timeout", 30)),
            ).result()
    else:
        result = asyncio.run(
            executor.execute(
                arguments["command"],
                timeout=arguments.get("timeout", 30),
            )
        )
    return json.dumps(result)


def handle_http_request(arguments: dict[str, Any]) -> str:
    from app.core.network_proxy import NetworkProxy

    proxy = NetworkProxy()
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(
                asyncio.run,
                proxy.http_request(
                    method=arguments.get("method", "GET"),
                    url=arguments["url"],
                    headers=arguments.get("headers"),
                    body=arguments.get("body"),
                    timeout=arguments.get("timeout", 30),
                ),
            ).result()
    else:
        result = asyncio.run(
            proxy.http_request(
                method=arguments.get("method", "GET"),
                url=arguments["url"],
                headers=arguments.get("headers"),
                body=arguments.get("body"),
                timeout=arguments.get("timeout", 30),
            )
        )
    return json.dumps(result)


def handle_log_search(arguments: dict[str, Any]) -> str:
    lm = LogManager()
    results = lm.search(
        arguments["pattern"],
        source=arguments.get("source"),
        limit=arguments.get("limit", 100),
    )
    return json.dumps({"results": results})


def handle_log_tail(arguments: dict[str, Any]) -> str:
    lm = LogManager()
    lines = lm.tail(
        source=arguments.get("source"),
        n=arguments.get("n", 50),
    )
    return json.dumps({"lines": lines})


def handle_git_status(arguments: dict[str, Any]) -> str:
    from app.core.dev_manager import DevManager

    dm = DevManager()
    return json.dumps(dm.git_status(arguments["cwd"]))


def handle_git_log(arguments: dict[str, Any]) -> str:
    from app.core.dev_manager import DevManager

    dm = DevManager()
    entries = dm.git_log(arguments["cwd"], limit=arguments.get("limit", 10))
    return json.dumps({"entries": entries})


def handle_ssh_exec(arguments: dict[str, Any]) -> str:
    session_id = arguments.get("session_id", "")
    command = arguments.get("command", "")
    if not session_id or not command:
        return json.dumps({"error": "session_id and command required"})
    # MCP ssh_exec uses a fresh connection — for persistent sessions use REST API
    return json.dumps(
        {
            "note": "MCP ssh_exec requires active session via REST API",
            "hint": "POST /api/v1/ssh/connect first, then POST /api/v1/ssh/exec",
            "session_id": session_id,
            "command": command,
        }
    )


def handle_agent_list(arguments: dict[str, Any]) -> str:
    from app.core.agent_system import AgentRegistry

    registry = AgentRegistry()
    return json.dumps({"agents": registry.list_agents()})


def handle_agent_run(arguments: dict[str, Any]) -> str:
    from app.core.agent_system import AgentRegistry, AgentRunner

    registry = AgentRegistry()
    agent_name = arguments.get("agent_name", "")
    if not agent_name:
        return json.dumps({"error": "agent_name required"})
    try:
        registry.get(agent_name)
    except Exception:
        return json.dumps({"error": f"Agent not found: {agent_name}"})
    runner = AgentRunner(registry)  # type: ignore[arg-type]  # runtime registry-obj kabul eder; imza dict diyor (legacy)
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    params = arguments.get("params", {})
    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(asyncio.run, runner.run(agent_name, params)).result()
    else:
        result = asyncio.run(runner.run(agent_name, params))
    return json.dumps(result)


def handle_ai_chat(arguments: dict[str, Any]) -> str:
    from app.core.ai_inference import AIInference

    ai = AIInference()
    message = arguments.get("message", "")
    model: str = arguments.get("model") or "qwen3:1.7b"  # None → chat default (str-güvence)
    if not message:
        return json.dumps({"error": "message required"})
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(asyncio.run, ai.chat(message=message, model=model)).result()
    else:
        result = asyncio.run(ai.chat(message=message, model=model))
    return json.dumps(result)


def handle_rag_query(arguments: dict[str, Any]) -> str:
    from app.api.rag import _embed, _hybrid_search

    q = arguments["question"]
    n = int(arguments.get("n_results", 5))
    vec = _embed(q)  # type: ignore[no-untyped-call]  # app.api.rag helper'ları untyped (legacy)
    hits = _hybrid_search(q, vec, top_k=n)  # type: ignore[no-untyped-call]
    out = [{"id": h.get("id"), "score": round(float(h.get("score", 0)), 4), "payload": h.get("payload", {})} for h in hits]
    return json.dumps({"query": q, "count": len(out), "hits": out, "engine": "qdrant:klipper-memory"})


def handle_rag_index_text(arguments: dict[str, Any]) -> str:
    return json.dumps(
        {
            "ok": False,
            "skipped": "rag_index_text devre-dışı: canlı RAG memory-DB'den oto-indexlenir. İçerik için memory_save kullan; RAG otomatik reindex eder (rag-reindex cron).",
        }
    )


def handle_rag_stats(arguments: dict[str, Any]) -> str:
    import requests

    from app.api.rag import COLLECTION, QDRANT_URL

    try:
        r = requests.get(f"{QDRANT_URL}/collections/{COLLECTION}", timeout=5)
        res = r.json().get("result", {}) if r.ok else {}
        return json.dumps(
            {
                "collection": COLLECTION,
                "points": res.get("points_count"),
                "status": res.get("status"),
                "engine": "qdrant",
                "ok": r.ok,
            }
        )
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)[:200]})


def handle_deploy_self(arguments: dict[str, Any]) -> str:
    from app.core.config import get_settings
    from app.core.shell_executor import ShellExecutor

    settings = get_settings()
    executor = ShellExecutor(whitelist=settings.shell_whitelist)
    test_result = _run_async(
        executor.execute(
            "bash -c 'cd /opt/linux-ai-server && source venv/bin/activate && python -m pytest tests/ -q --ignore=tests/test_mcp.py 2>&1 | tail -5'",
            timeout=120,
        )
    )
    results = [{"step": "test", "exit_code": test_result["exit_code"], "output": test_result["stdout"]}]
    if arguments.get("restart", True) and test_result["exit_code"] == 0:
        restart = _run_async(executor.execute("systemctl restart linux-ai-server", timeout=15))
        results.append({"step": "restart", "exit_code": restart["exit_code"]})
    return json.dumps({"success": test_result["exit_code"] == 0, "results": results})


def handle_project_list(arguments: dict[str, Any]) -> str:
    from app.api.deploy import _load_registry

    return json.dumps(_load_registry())


def handle_project_info(arguments: dict[str, Any]) -> str:
    from app.api.deploy import _load_registry

    registry = _load_registry()
    project = registry["projects"].get(arguments.get("name", ""))
    return json.dumps(project or {"error": "Project not found"})


def handle_devops_status(arguments: dict[str, Any]) -> str:
    return json.dumps({"note": "Use REST API GET /api/v1/devops/status — agent state is in app memory"})


def handle_devops_alerts(arguments: dict[str, Any]) -> str:
    return json.dumps({"note": "Use REST API GET /api/v1/devops/alerts"})


def handle_devops_metrics(arguments: dict[str, Any]) -> str:
    return json.dumps({"note": "Use REST API GET /api/v1/devops/metrics/buffer"})


def handle_devops_remediations(arguments: dict[str, Any]) -> str:
    return json.dumps({"note": "Use REST API GET /api/v1/devops/remediation/log"})


def handle_kernel_proc_metrics(arguments: dict[str, Any]) -> str:
    try:
        with open("/proc/linux_ai") as f:
            lines = f.read().strip().split("\n")
        metrics = {}
        for line in lines:
            parts = line.split()
            if len(parts) == 2:
                metrics[parts[0]] = parts[1]
        return json.dumps(metrics)
    except FileNotFoundError:
        return json.dumps({"error": "Kernel module not loaded"})


def handle_kernel_firewall_status(arguments: dict[str, Any]) -> str:
    try:
        with open("/proc/linux_ai_firewall") as f:
            content = f.read()
        return json.dumps({"raw": content})
    except FileNotFoundError:
        return json.dumps({"error": "Firewall module not loaded"})


def handle_kernel_firewall_block(arguments: dict[str, Any]) -> str:
    ip = arguments.get("ip", "")
    try:
        with open("/proc/linux_ai_firewall", "w") as f:
            f.write(f"block {ip}")
        return json.dumps({"blocked": ip})
    except Exception as e:
        return json.dumps({"error": str(e)})


def handle_kernel_firewall_unblock(arguments: dict[str, Any]) -> str:
    ip = arguments.get("ip", "")
    try:
        with open("/proc/linux_ai_firewall", "w") as f:
            f.write(f"unblock {ip}")
        return json.dumps({"unblocked": ip})
    except Exception as e:
        return json.dumps({"error": str(e)})


def handle_kernel_usb_status(arguments: dict[str, Any]) -> str:
    try:
        with open("/proc/linux_ai_usb") as f:
            content = f.read()
        return json.dumps({"raw": content})
    except FileNotFoundError:
        return json.dumps({"error": "USB module not loaded"})


def handle_workspace_note_save(arguments: dict[str, Any]) -> str:
    workspace = "/data/claude/workspace"
    os.makedirs(workspace, exist_ok=True)
    path = os.path.join(workspace, arguments["name"])
    with open(path, "w") as f:
        f.write(arguments["content"])
    return json.dumps({"saved": arguments["name"], "size": len(arguments["content"])})


def handle_workspace_note_read(arguments: dict[str, Any]) -> str:
    path = f"/data/claude/workspace/{arguments['name']}"
    try:
        with open(path) as f:
            content = f.read()
        return json.dumps({"name": arguments["name"], "content": content})
    except FileNotFoundError:
        return json.dumps({"error": f"Note {arguments['name']} not found"})


def handle_workspace_note_list(arguments: dict[str, Any]) -> str:
    workspace = "/data/claude/workspace"
    notes = []
    if os.path.isdir(workspace):
        for f in sorted(os.listdir(workspace)):
            fp = os.path.join(workspace, f)
            if os.path.isfile(fp):
                notes.append({"name": f, "size": os.path.getsize(fp)})
    return json.dumps({"notes": notes})


def handle_memory_context(arguments: dict[str, Any]) -> str:
    import sqlite3

    db = "/opt/linux-ai-server/data/claude_memory.db"
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        result = {}

        # Active projects
        c.execute("SELECT name, description, content FROM memories WHERE type='project' AND active=1 ORDER BY updated_at DESC")
        result["active_projects"] = [
            {"name": r["name"], "description": r["description"], "content": r["content"][:500]} for r in c.fetchall()
        ]

        # Recent sessions (last 5)
        c.execute("SELECT date, device_name, platform, summary FROM sessions ORDER BY id DESC LIMIT 5")
        result["recent_sessions"] = [dict(r) for r in c.fetchall()]

        # Pending/recent tasks (last 10)
        c.execute("SELECT project, task, status, device_name, created_at FROM tasks_log ORDER BY id DESC LIMIT 10")
        result["recent_tasks"] = [dict(r) for r in c.fetchall()]

        # Unread discoveries
        c.execute("SELECT project, type, title, details FROM discoveries WHERE resolved=0 ORDER BY id DESC LIMIT 10")
        result["unresolved_discoveries"] = [dict(r) for r in c.fetchall()]

        # Devices
        c.execute("SELECT name, platform, last_seen FROM devices")
        result["devices"] = [dict(r) for r in c.fetchall()]

        # Active feedback/decisions
        c.execute("SELECT name, description, content FROM memories WHERE type IN ('feedback','decision') AND active=1")
        result["active_rules"] = [{"name": r["name"], "description": r["description"]} for r in c.fetchall()]

        conn.close()
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


def handle_memory_query(arguments: dict[str, Any]) -> str:
    import sqlite3

    sql = arguments.get("sql", "")
    if not sql.strip().upper().startswith("SELECT"):
        return json.dumps({"error": "Only SELECT queries allowed"})
    try:
        conn = sqlite3.connect("/opt/linux-ai-server/data/claude_memory.db")
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(sql)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return json.dumps({"rows": rows, "count": len(rows)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


def handle_memory_save(arguments: dict[str, Any]) -> str:
    import sqlite3
    from datetime import datetime

    db = "/opt/linux-ai-server/data/claude_memory.db"
    try:
        conn = sqlite3.connect(db)
        c = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Upsert by name
        c.execute("SELECT id FROM memories WHERE name=?", (arguments["name"],))
        existing = c.fetchone()
        if existing:
            c.execute(
                "UPDATE memories SET content=?, description=?, updated_at=?, active=1 WHERE id=?",
                (arguments["content"], arguments.get("description", ""), now, existing[0]),
            )
            action = "updated"
        else:
            c.execute(
                "INSERT INTO memories (type, name, description, content, created_at, updated_at, active, read_count) VALUES (?,?,?,?,?,?,1,0)",
                (arguments["type"], arguments["name"], arguments.get("description", ""), arguments["content"], now, now),
            )
            action = "created"
        conn.commit()
        conn.close()
        return json.dumps({"action": action, "name": arguments["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


def handle_memory_log_session(arguments: dict[str, Any]) -> str:
    import sqlite3
    from datetime import datetime

    db = "/opt/linux-ai-server/data/claude_memory.db"
    try:
        conn = sqlite3.connect(db)
        c = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d")
        c.execute("SELECT COALESCE(MAX(session_num),0)+1 FROM sessions")
        next_num = c.fetchone()[0]
        c.execute(
            "INSERT INTO sessions (session_num, date, summary, tasks_completed, files_changed, device_name, platform, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                next_num,
                now,
                arguments["summary"],
                arguments.get("tasks_completed", ""),
                arguments.get("files_changed", ""),
                arguments["device_name"],
                arguments["platform"],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        conn.close()
        return json.dumps({"logged": True, "session_num": next_num})
    except Exception as e:
        return json.dumps({"error": str(e)})


def handle_memory_log_task(arguments: dict[str, Any]) -> str:
    import sqlite3
    from datetime import datetime

    db = "/opt/linux-ai-server/data/claude_memory.db"
    try:
        conn = sqlite3.connect(db)
        c = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            "INSERT INTO tasks_log (project, task, status, files_changed, details, device_name, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                arguments["project"],
                arguments["task"],
                arguments["status"],
                arguments.get("files_changed", ""),
                arguments.get("details", ""),
                arguments["device_name"],
                now,
            ),
        )
        conn.commit()
        conn.close()
        return json.dumps({"logged": True, "task": arguments["task"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


def handle_vps_exec(arguments: dict[str, Any]) -> str:
    import shlex

    from app.core.config import get_settings
    from app.core.shell_executor import ShellExecutor

    settings = get_settings()
    executor = ShellExecutor(whitelist=settings.shell_whitelist)
    # Onceki kod normal-string'di (f-string DEGIL) -> literal "{arguments['command']}"
    # metni SSH'a gidiyordu, komut HIC calismiyordu (surer P1). shlex.quote ile
    # remote'a tek-arg + injection-guard (app/api/vps.py:35 sağlam deseni birebir).
    cmd = (
        "ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "
        + os.environ.get("VPS_HOST", "")
        + " "
        + shlex.quote(arguments["command"])
    )
    result = _run_async(executor.execute(cmd, timeout=arguments.get("timeout", 30)))
    return json.dumps(result)


def handle_vps_status(arguments: dict[str, Any]) -> str:
    from app.core.config import get_settings
    from app.core.shell_executor import ShellExecutor

    settings = get_settings()
    executor = ShellExecutor(whitelist=settings.shell_whitelist)
    cmd = (
        "ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "
        + os.environ.get("VPS_HOST", "")
        + " 'hostname && uptime -p && free -h | head -2 && df -h / | tail -1 && docker ps --format \"{{.Names}}: {{.Status}}\" | head -15'"
    )
    result = _run_async(executor.execute(cmd, timeout=15))
    return json.dumps(result)


def handle_vps_services(arguments: dict[str, Any]) -> str:
    from app.core.config import get_settings
    from app.core.shell_executor import ShellExecutor

    settings = get_settings()
    executor = ShellExecutor(whitelist=settings.shell_whitelist)
    # Onceki kod triple-quote'tu -> '" + os.environ.get(...) + "' concatenation
    # DEGIL, literal metin olarak komuta gomuluyordu (surer P1: komut bozuk).
    # Gercek concatenation'a cevrildi (vps_status:1010 sağlam deseni).
    cmd = (
        "ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "
        + os.environ.get("VPS_HOST", "")
        + " 'for u in https://coolify.panola.app https://uptime.panola.app https://n8n.panola.app https://analytics.panola.app; do echo \"$u $(curl -s -o /dev/null -w %{http_code} $u)\"; done'"
    )
    result = _run_async(executor.execute(cmd, timeout=20))
    return json.dumps(result)


_HANDLERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "kernel_status": handle_kernel_status,
    "kernel_set_governor": handle_kernel_set_governor,
    "system_info": handle_system_info,
    "process_list": handle_process_list,
    "monitor_metrics": handle_monitor_metrics,
    "file_read": handle_file_read,
    "file_write": handle_file_write,
    "file_list": handle_file_list,
    "file_search": handle_file_search,
    "shell_exec": handle_shell_exec,
    "http_request": handle_http_request,
    "log_search": handle_log_search,
    "log_tail": handle_log_tail,
    "git_status": handle_git_status,
    "git_log": handle_git_log,
    "ssh_exec": handle_ssh_exec,
    "agent_list": handle_agent_list,
    "agent_run": handle_agent_run,
    "ai_chat": handle_ai_chat,
    "rag_query": handle_rag_query,
    "rag_index_text": handle_rag_index_text,
    "rag_stats": handle_rag_stats,
    "deploy_self": handle_deploy_self,
    "project_list": handle_project_list,
    "project_info": handle_project_info,
    "devops_status": handle_devops_status,
    "devops_alerts": handle_devops_alerts,
    "devops_metrics": handle_devops_metrics,
    "devops_remediations": handle_devops_remediations,
    "kernel_proc_metrics": handle_kernel_proc_metrics,
    "kernel_firewall_status": handle_kernel_firewall_status,
    "kernel_firewall_block": handle_kernel_firewall_block,
    "kernel_firewall_unblock": handle_kernel_firewall_unblock,
    "kernel_usb_status": handle_kernel_usb_status,
    "workspace_note_save": handle_workspace_note_save,
    "workspace_note_read": handle_workspace_note_read,
    "workspace_note_list": handle_workspace_note_list,
    "memory_context": handle_memory_context,
    "memory_query": handle_memory_query,
    "memory_save": handle_memory_save,
    "memory_log_session": handle_memory_log_session,
    "memory_log_task": handle_memory_log_task,
    "vps_exec": handle_vps_exec,
    "vps_status": handle_vps_status,
    "vps_services": handle_vps_services,
}


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """Execute a tool and return JSON result. Registry-dispatch (eski 45-branch
    if/elif god-function yerine); bilinmeyen tool + her exception -> {"error": ...}."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        return handler(arguments)
    except Exception as e:
        return json.dumps({"error": str(e)})
