#!/bin/bash
# MCP SSH wrapper — pipes stdin/stdout through SSH to remote MCP server
# Usage: Called by Claude Code as MCP command
exec "C:/Program Files/PuTTY/plink.exe" -ssh klipperos@REDACTED_LAN_IP -pw REDACTED_SSH_PASS -batch -T "/opt/linux-ai-server/venv/bin/python3 /opt/linux-ai-server/run_mcp.py"
