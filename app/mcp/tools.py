"""MCP tool definitions -- maps core services to MCP tool format."""

from __future__ import annotations

import json

from app.core.kernel_bridge import KernelBridge
from app.core.system_manager import SystemManager
from app.core.monitor_agent import MonitorAgent
from app.core.log_manager import LogManager


def get_tool_definitions() -> list[dict]:
    """Return all available MCP tools in MCP protocol format."""
    return [
        {
            "name": "kernel_status",
            "description": "Get Linux-AI kernel module status (state, governor, CPU count, services)",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "kernel_set_governor",
            "description": "Set CPU governor mode (performance, powersave, ondemand, conservative, ai_adaptive)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["performance", "powersave", "ondemand", "conservative", "ai_adaptive"],
                    }
                },
                "required": ["mode"],
            },
        },
        {
            "name": "system_info",
            "description": "Get system information (CPU, RAM, disk, uptime, hostname)",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "process_list",
            "description": "List running processes sorted by CPU or memory usage",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20},
                    "sort_by": {"type": "string", "enum": ["cpu", "memory"], "default": "cpu"},
                },
            },
        },
        {
            "name": "file_read",
            "description": "Read file contents from the server",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "file_write",
            "description": "Write content to a file on the server",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "mode": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "file_list",
            "description": "List files in a directory",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "file_search",
            "description": "Search for files by pattern",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "pattern": {"type": "string"},
                },
                "required": ["path", "pattern"],
            },
        },
        {
            "name": "shell_exec",
            "description": "Execute a whitelisted shell command",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
        },
        {
            "name": "http_request",
            "description": "Make an HTTP request (proxy for internet access)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "method": {"type": "string"},
                    "url": {"type": "string"},
                    "headers": {"type": "object"},
                    "body": {"type": "string"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "monitor_metrics",
            "description": "Get current system metrics (CPU, RAM, disk, temp, network)",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "log_search",
            "description": "Search log files by pattern",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "source": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "log_tail",
            "description": "Get last N lines from logs",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "n": {"type": "integer"},
                },
            },
        },
        {
            "name": "git_status",
            "description": "Get git status of a repository",
            "inputSchema": {
                "type": "object",
                "properties": {"cwd": {"type": "string"}},
                "required": ["cwd"],
            },
        },
        {
            "name": "git_log",
            "description": "Get git log of a repository",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["cwd"],
            },
        },
        {
            "name": "ssh_exec",
            "description": "Execute command on a remote server via SSH",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "command": {"type": "string"},
                },
                "required": ["session_id", "command"],
            },
        },
        {
            "name": "agent_list",
            "description": "List all registered agents",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "agent_run",
            "description": "Run a named agent",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string"},
                    "params": {"type": "object"},
                },
                "required": ["agent_name"],
            },
        },
        {
            "name": "ai_chat",
            "description": "Chat with local Ollama AI model",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "model": {"type": "string"},
                },
                "required": ["message"],
            },
        },
        # ── RAG Tools ──
        {
            "name": "rag_query",
            "description": "Search indexed documents with semantic search (no LLM generation)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "n_results": {"type": "integer", "default": 5},
                },
                "required": ["question"],
            },
        },
        {
            "name": "rag_index_text",
            "description": "Index text into the RAG document store",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "source": {"type": "string", "default": "manual"},
                },
                "required": ["text"],
            },
        },
        {
            "name": "rag_stats",
            "description": "Get RAG collection statistics (document count)",
            "inputSchema": {"type": "object", "properties": {}},
        },
        # ── Deploy Tools ──
        {
            "name": "deploy_self",
            "description": "Run tests and restart linux-ai-server (one-command deploy)",
            "inputSchema": {
                "type": "object",
                "properties": {"restart": {"type": "boolean", "default": True}},
            },
        },
        {
            "name": "project_list",
            "description": "List all tracked projects with metadata",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "project_info",
            "description": "Get project details including git status",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        # ── DevOps Agent Tools ──
        {
            "name": "devops_status",
            "description": "Get DevOps agent status (running, check count, active alerts)",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "devops_alerts",
            "description": "Get currently active (unresolved) alerts",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "devops_metrics",
            "description": "Get in-memory metrics buffer (last ~1 hour of 30s samples)",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "devops_remediations",
            "description": "Get remediation action history",
            "inputSchema": {"type": "object", "properties": {}},
        },
        # ── Kernel Module Tools ──
        {
            "name": "kernel_proc_metrics",
            "description": "Read /proc/linux_ai kernel metrics (memory, load, uptime from kernel space)",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "kernel_firewall_status",
            "description": "Read /proc/linux_ai_firewall (packet counts, blocked IPs)",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "kernel_firewall_block",
            "description": "Block an IP address at kernel level via netfilter",
            "inputSchema": {
                "type": "object",
                "properties": {"ip": {"type": "string"}},
                "required": ["ip"],
            },
        },
        {
            "name": "kernel_firewall_unblock",
            "description": "Unblock an IP address at kernel level",
            "inputSchema": {
                "type": "object",
                "properties": {"ip": {"type": "string"}},
                "required": ["ip"],
            },
        },
        {
            "name": "kernel_usb_status",
            "description": "Read /proc/linux_ai_usb (USB whitelist, connection log)",
            "inputSchema": {"type": "object", "properties": {}},
        },
        # ── Workspace Tools ──
        {
            "name": "workspace_note_save",
            "description": "Save a note to Claude's persistent workspace on the server",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["name", "content"],
            },
        },
        {
            "name": "workspace_note_read",
            "description": "Read a note from Claude's workspace",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        {
            "name": "workspace_note_list",
            "description": "List all notes in Claude's workspace",
            "inputSchema": {"type": "object", "properties": {}},
        },
        # ── VPS Bridge Tools ──
        {
            "name": "vps_exec",
            "description": "Execute a command on production VPS (Contabo) via SSH bridge",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30},
                },
                "required": ["command"],
            },
        },
        {
            "name": "vps_status",
            "description": "Get production VPS status (hostname, uptime, RAM, disk, containers)",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "vps_services",
            "description": "Check production VPS web services health (Coolify, Uptime Kuma, n8n, Plausible)",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _run_async(coro):
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


def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool and return JSON result."""
    try:
        if name == "kernel_status":
            bridge = KernelBridge()
            return json.dumps(bridge.get_status())

        elif name == "kernel_set_governor":
            bridge = KernelBridge()
            bridge.set_governor(arguments["mode"])
            return json.dumps({"governor": arguments["mode"], "success": True})

        elif name == "system_info":
            mgr = SystemManager()
            return json.dumps(mgr.get_system_info())

        elif name == "process_list":
            mgr = SystemManager()
            procs = mgr.get_processes(
                limit=arguments.get("limit", 20),
                sort_by=arguments.get("sort_by", "cpu"),
            )
            return json.dumps({"processes": procs})

        elif name == "monitor_metrics":
            monitor = MonitorAgent()
            return json.dumps(monitor.collect_metrics())

        elif name == "file_read":
            from app.core.file_manager import FileManager
            fm = FileManager(allowed_paths=["/"], max_file_size_mb=10)
            result = fm.read_file(
                arguments["path"],
                offset=arguments.get("offset", 0),
                limit=arguments.get("limit", 1000),
            )
            return json.dumps(result)

        elif name == "file_write":
            from app.core.file_manager import FileManager
            fm = FileManager(allowed_paths=["/"], max_file_size_mb=10)
            result = fm.write_file(
                arguments["path"],
                arguments["content"],
                mode=arguments.get("mode", "write"),
            )
            return json.dumps(result)

        elif name == "file_list":
            from app.core.file_manager import FileManager
            fm = FileManager(allowed_paths=["/"], max_file_size_mb=10)
            entries = fm.list_directory(arguments["path"])
            return json.dumps({"path": arguments["path"], "entries": entries})

        elif name == "file_search":
            from app.core.file_manager import FileManager
            fm = FileManager(allowed_paths=["/"], max_file_size_mb=10)
            results = fm.search_files(
                arguments["path"],
                arguments["pattern"],
                max_results=arguments.get("max_results", 50),
            )
            return json.dumps({"results": results})

        elif name == "shell_exec":
            from app.core.shell_executor import ShellExecutor
            from app.core.config import get_settings
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
                result = asyncio.run(executor.execute(
                    arguments["command"], timeout=arguments.get("timeout", 30),
                ))
            return json.dumps(result)

        elif name == "http_request":
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
                result = asyncio.run(proxy.http_request(
                    method=arguments.get("method", "GET"),
                    url=arguments["url"],
                    headers=arguments.get("headers"),
                    body=arguments.get("body"),
                    timeout=arguments.get("timeout", 30),
                ))
            return json.dumps(result)

        elif name == "log_search":
            lm = LogManager()
            results = lm.search(
                arguments["pattern"],
                source=arguments.get("source"),
                limit=arguments.get("limit", 100),
            )
            return json.dumps({"results": results})

        elif name == "log_tail":
            lm = LogManager()
            lines = lm.tail(
                source=arguments.get("source"),
                n=arguments.get("n", 50),
            )
            return json.dumps({"lines": lines})

        elif name == "git_status":
            from app.core.dev_manager import DevManager
            dm = DevManager()
            return json.dumps(dm.git_status(arguments["cwd"]))

        elif name == "git_log":
            from app.core.dev_manager import DevManager
            dm = DevManager()
            entries = dm.git_log(arguments["cwd"], limit=arguments.get("limit", 10))
            return json.dumps({"entries": entries})

        elif name == "ssh_exec":
            from app.core.ssh_client import SSHClient
            ssh = SSHClient()
            session_id = arguments.get("session_id", "")
            command = arguments.get("command", "")
            if not session_id or not command:
                return json.dumps({"error": "session_id and command required"})
            # MCP ssh_exec uses a fresh connection — for persistent sessions use REST API
            return json.dumps({
                "note": "MCP ssh_exec requires active session via REST API",
                "hint": "POST /api/v1/ssh/connect first, then POST /api/v1/ssh/exec",
                "session_id": session_id,
                "command": command,
            })

        elif name == "agent_list":
            from app.core.agent_system import AgentRegistry
            registry = AgentRegistry()
            return json.dumps({"agents": registry.list_agents()})

        elif name == "agent_run":
            from app.core.agent_system import AgentRegistry, AgentRunner
            registry = AgentRegistry()
            agent_name = arguments.get("agent_name", "")
            if not agent_name:
                return json.dumps({"error": "agent_name required"})
            try:
                agent = registry.get(agent_name)
            except Exception as e:
                return json.dumps({"error": f"Agent not found: {agent_name}"})
            runner = AgentRunner(registry)
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            params = arguments.get("params", {})
            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run, runner.run(agent_name, params)
                    ).result()
            else:
                result = asyncio.run(runner.run(agent_name, params))
            return json.dumps(result)

        elif name == "ai_chat":
            from app.core.ai_inference import AIInference
            ai = AIInference()
            message = arguments.get("message", "")
            model = arguments.get("model")
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
                    result = pool.submit(
                        asyncio.run, ai.chat(message=message, model=model)
                    ).result()
            else:
                result = asyncio.run(ai.chat(message=message, model=model))
            return json.dumps(result)

        # ── RAG Tools ──
        elif name == "rag_query":
            from app.core.rag_engine import RAGEngine
            engine = RAGEngine()
            import asyncio
            result = _run_async(engine.query(
                arguments["question"], n_results=arguments.get("n_results", 5), generate=False,
            ))
            return json.dumps(result)

        elif name == "rag_index_text":
            from app.core.rag_engine import RAGEngine
            engine = RAGEngine()
            result = _run_async(engine.index_text(
                arguments["text"], source=arguments.get("source", "mcp"),
            ))
            return json.dumps(result)

        elif name == "rag_stats":
            from app.core.rag_engine import RAGEngine
            engine = RAGEngine()
            return json.dumps(_run_async(engine.stats()))

        # ── Deploy Tools ──
        elif name == "deploy_self":
            from app.core.shell_executor import ShellExecutor
            from app.core.config import get_settings
            settings = get_settings()
            executor = ShellExecutor(whitelist=settings.shell_whitelist)
            test_result = _run_async(executor.execute(
                "bash -c 'cd /opt/linux-ai-server && source venv/bin/activate && python -m pytest tests/ -q --ignore=tests/test_mcp.py 2>&1 | tail -5'",
                timeout=120,
            ))
            results = [{"step": "test", "exit_code": test_result["exit_code"], "output": test_result["stdout"]}]
            if arguments.get("restart", True) and test_result["exit_code"] == 0:
                restart = _run_async(executor.execute("systemctl restart linux-ai-server", timeout=15))
                results.append({"step": "restart", "exit_code": restart["exit_code"]})
            return json.dumps({"success": test_result["exit_code"] == 0, "results": results})

        elif name == "project_list":
            from app.api.deploy import _load_registry
            return json.dumps(_load_registry())

        elif name == "project_info":
            from app.api.deploy import _load_registry
            registry = _load_registry()
            project = registry["projects"].get(arguments.get("name", ""))
            return json.dumps(project or {"error": "Project not found"})

        # ── DevOps Tools ──
        elif name == "devops_status":
            return json.dumps({"note": "Use REST API GET /api/v1/devops/status — agent state is in app memory"})

        elif name == "devops_alerts":
            return json.dumps({"note": "Use REST API GET /api/v1/devops/alerts"})

        elif name == "devops_metrics":
            return json.dumps({"note": "Use REST API GET /api/v1/devops/metrics/buffer"})

        elif name == "devops_remediations":
            return json.dumps({"note": "Use REST API GET /api/v1/devops/remediation/log"})

        # ── Kernel Module Tools ──
        elif name == "kernel_proc_metrics":
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

        elif name == "kernel_firewall_status":
            try:
                with open("/proc/linux_ai_firewall") as f:
                    content = f.read()
                return json.dumps({"raw": content})
            except FileNotFoundError:
                return json.dumps({"error": "Firewall module not loaded"})

        elif name == "kernel_firewall_block":
            ip = arguments.get("ip", "")
            try:
                with open("/proc/linux_ai_firewall", "w") as f:
                    f.write(f"block {ip}")
                return json.dumps({"blocked": ip})
            except Exception as e:
                return json.dumps({"error": str(e)})

        elif name == "kernel_firewall_unblock":
            ip = arguments.get("ip", "")
            try:
                with open("/proc/linux_ai_firewall", "w") as f:
                    f.write(f"unblock {ip}")
                return json.dumps({"unblocked": ip})
            except Exception as e:
                return json.dumps({"error": str(e)})

        elif name == "kernel_usb_status":
            try:
                with open("/proc/linux_ai_usb") as f:
                    content = f.read()
                return json.dumps({"raw": content})
            except FileNotFoundError:
                return json.dumps({"error": "USB module not loaded"})

        # ── Workspace Tools ──
        elif name == "workspace_note_save":
            import os
            workspace = "/data/claude/workspace"
            os.makedirs(workspace, exist_ok=True)
            path = os.path.join(workspace, arguments["name"])
            with open(path, "w") as f:
                f.write(arguments["content"])
            return json.dumps({"saved": arguments["name"], "size": len(arguments["content"])})

        elif name == "workspace_note_read":
            path = f"/data/claude/workspace/{arguments['name']}"
            try:
                with open(path) as f:
                    content = f.read()
                return json.dumps({"name": arguments["name"], "content": content})
            except FileNotFoundError:
                return json.dumps({"error": f"Note {arguments['name']} not found"})

        elif name == "workspace_note_list":
            import os
            workspace = "/data/claude/workspace"
            notes = []
            if os.path.isdir(workspace):
                for f in sorted(os.listdir(workspace)):
                    fp = os.path.join(workspace, f)
                    if os.path.isfile(fp):
                        notes.append({"name": f, "size": os.path.getsize(fp)})
            return json.dumps({"notes": notes})

        # ── VPS Bridge Tools ──
        elif name == "vps_exec":
            from app.core.shell_executor import ShellExecutor
            from app.core.config import get_settings
            settings = get_settings()
            executor = ShellExecutor(whitelist=settings.shell_whitelist)
            cmd = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@REDACTED_VPS_IP '{arguments['command']}'"
            result = _run_async(executor.execute(cmd, timeout=arguments.get("timeout", 30)))
            return json.dumps(result)

        elif name == "vps_status":
            from app.core.shell_executor import ShellExecutor
            from app.core.config import get_settings
            settings = get_settings()
            executor = ShellExecutor(whitelist=settings.shell_whitelist)
            cmd = "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@REDACTED_VPS_IP 'hostname && uptime -p && free -h | head -2 && df -h / | tail -1 && docker ps --format \"{{.Names}}: {{.Status}}\" | head -15'"
            result = _run_async(executor.execute(cmd, timeout=15))
            return json.dumps(result)

        elif name == "vps_services":
            from app.core.shell_executor import ShellExecutor
            from app.core.config import get_settings
            settings = get_settings()
            executor = ShellExecutor(whitelist=settings.shell_whitelist)
            cmd = """ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@REDACTED_VPS_IP 'for u in https://coolify.panola.app https://uptime.panola.app https://n8n.panola.app https://analytics.panola.app; do echo "$u $(curl -s -o /dev/null -w %{http_code} $u)"; done'"""
            result = _run_async(executor.execute(cmd, timeout=20))
            return json.dumps(result)

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})
