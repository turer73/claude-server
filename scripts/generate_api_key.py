#!/usr/bin/env python3
"""Generate an API key for linux-ai-server."""

import asyncio
import hashlib
import os
import secrets
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from app.db.database import Database

    db_path = os.environ.get("DB_PATH", "/var/lib/linux-ai-server/server.db")
    name = sys.argv[1] if len(sys.argv) > 1 else "admin"
    permissions = sys.argv[2] if len(sys.argv) > 2 else "admin"

    # Generate key
    api_key = secrets.token_hex(32)
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    # Store in database
    db = Database(db_path)
    await db.initialize()
    await db.execute(
        "INSERT INTO api_keys (key_hash, name, permissions) VALUES (?, ?, ?)",
        (key_hash, name, permissions),
    )
    await db.close()

    print(f"\n=== API Key Generated ===")
    print(f"Name:        {name}")
    print(f"Permissions: {permissions}")
    print(f"API Key:     {api_key}")
    print(f"\nSave this key! It cannot be recovered.")
    print(f"Use it in the X-API-Key header or POST /api/v1/auth/token")


if __name__ == "__main__":
    asyncio.run(main())
