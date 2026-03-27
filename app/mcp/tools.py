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
        elif name == "agent_list":
            from app.core.agent_system import AgentRegistry

            registry = AgentRegistry()
            return json.dumps({"agents": registry.list_agents()})
        else:
            return json.dumps({"error": f"Tool '{name}' execution not implemented yet", "tool": name})
    except Exception as e:
        return json.dumps({"error": str(e)})
