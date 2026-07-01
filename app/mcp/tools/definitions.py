"""MCP tool şema tanımları — get_tool_definitions (statik JSON-schema)."""

from __future__ import annotations

from typing import Any


def get_tool_definitions() -> list[dict[str, Any]]:
    """Return all available MCP tools in MCP protocol format."""
    return [
        {
            "name": "kernel_status",
            "description": "Get Linux-AI kernel module status (state, governor, CPU count, services)",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "kernel_set_governor",
            "description": "Set the CPU cpufreq governor (availability is hardware-dependent; validated at runtime). Common: performance, powersave, ondemand, conservative, schedutil",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["performance", "powersave", "ondemand", "conservative", "schedutil"],
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
        # ── Memory DB Tools ──
        {
            "name": "memory_context",
            "description": "Get full session context from memory DB: active projects, recent sessions, pending tasks, unread discoveries. Call this at the START of every conversation to load context.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "memory_query",
            "description": "Query memory DB with flexible SQL. Tables: memories, sessions, tasks_log, devices, device_projects, discoveries, notes. Use for specific lookups.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SELECT query (read-only)"},
                },
                "required": ["sql"],
            },
        },
        {
            "name": "memory_save",
            "description": "Save or update a memory entry (project info, decision, feedback, reference). Use type: project|decision|feedback|reference",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["project", "decision", "feedback", "reference"]},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["type", "name", "content"],
            },
        },
        {
            "name": "memory_log_session",
            "description": "Log current session summary when conversation ends or major milestone reached",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "tasks_completed": {"type": "string"},
                    "files_changed": {"type": "string"},
                    "device_name": {"type": "string"},
                    "platform": {"type": "string"},
                },
                "required": ["summary", "device_name", "platform"],
            },
        },
        {
            "name": "memory_log_task",
            "description": "Log a completed task with project, description, and changed files",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "task": {"type": "string"},
                    "status": {"type": "string", "enum": ["done", "failed", "blocked", "in_progress"]},
                    "files_changed": {"type": "string"},
                    "details": {"type": "string"},
                    "device_name": {"type": "string"},
                },
                "required": ["project", "task", "status", "device_name"],
            },
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
