@echo off
"C:\Program Files\PuTTY\plink.exe" -ssh klipperos@192.168.1.129 -pw tur0452 -batch -T "/opt/linux-ai-server/venv/bin/python3 /opt/linux-ai-server/run_mcp.py"
