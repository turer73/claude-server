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
    ]


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

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})
