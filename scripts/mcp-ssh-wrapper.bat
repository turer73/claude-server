@echo off
REM Required env vars: MCP_SSH_USER, MCP_SSH_HOST, MCP_SSH_PASS
"C:\Program Files\PuTTY\plink.exe" -ssh %MCP_SSH_USER%@%MCP_SSH_HOST% -pw %MCP_SSH_PASS% -batch -T "/opt/linux-ai-server/venv/bin/python3 /opt/linux-ai-server/run_mcp.py"
