#!/usr/bin/env python3
"""MCP server entry point for stdio transport."""
import sys
import os

# Add project to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('CONFIG_FILE', '/etc/linux-ai-server/server.yml')

from app.mcp.server import MCPServer

if __name__ == '__main__':
    server = MCPServer()
    server.run_stdio()
