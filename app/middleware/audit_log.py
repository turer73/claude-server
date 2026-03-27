"""Audit logger — records all write/exec operations to database."""

from __future__ import annotations

from app.db.database import Database


class AuditLogger:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def log(
        self,
        request_id: str,
        user: str,
        action: str,
        resource: str,
        status: str,
        details: str | None = None,
        ip_address: str | None = None,
    ) -> None:
        await self._db.execute(
            """INSERT INTO audit_log (request_id, user, action, resource, status, details, ip_address)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (request_id, user, action, resource, status, details, ip_address),
        )
