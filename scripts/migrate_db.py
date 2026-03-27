#!/usr/bin/env python3
"""Run database migrations for linux-ai-server."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from app.db.database import Database

    db_path = os.environ.get("DB_PATH", "/var/lib/linux-ai-server/server.db")
    print(f"Migrating database: {db_path}")

    db = Database(db_path)
    await db.initialize()
    await db.close()

    print("Migration complete!")


if __name__ == "__main__":
    asyncio.run(main())
