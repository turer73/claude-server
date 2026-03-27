@echo off
"C:\Program Files\PuTTY\plink.exe" -ssh klipperos@REDACTED_LAN_IP -pw REDACTED_SSH_PASS -batch -T "/opt/linux-ai-server/venv/bin/python3 /opt/linux-ai-server/run_mcp.py"
