"""Unified FTS5 search across memories + sessions."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from polymem.db import connect

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _to_fts_query(q: str) -> str:
    """Convert free-form input into a safe FTS5 query.

    Each word token becomes a prefix match (`tok*`), wrapped in quotes so
    punctuation in the original input never reaches the FTS5 parser.
    Returns an empty string when nothing tokenizable remains.
    """
    tokens = _TOKEN_RE.findall(q)
    return " ".join(f'"{t}"*' for t in tokens)


def build_router(db_path: str | Path, auth_dep) -> APIRouter:
    router = APIRouter(prefix="/search", tags=["search"], dependencies=[Depends(auth_dep)])

    @router.get("")
    async def search(
        q: str = Query(..., min_length=2),
        limit: int = Query(default=10, ge=1, le=100),
    ):
        fts_q = _to_fts_query(q)
        empty = {
            "query": q,
            "total": 0,
            "results": {"memories": [], "sessions": []},
        }
        if not fts_q:
            return empty

        with connect(db_path) as db:
            memory_rows = db.execute(
                """
                SELECT m.id, m.type, m.name, m.description,
                       snippet(memories_fts, 2, '<b>', '</b>', '…', 12) AS snippet
                FROM memories_fts
                JOIN memories m ON m.id = memories_fts.rowid
                WHERE memories_fts MATCH ? AND m.active = 1
                ORDER BY rank
                LIMIT ?
                """,
                (fts_q, limit),
            ).fetchall()

            session_rows = db.execute(
                """
                SELECT s.id, s.date, s.device_name, s.project,
                       snippet(sessions_fts, 0, '<b>', '</b>', '…', 12) AS snippet
                FROM sessions_fts
                JOIN sessions s ON s.id = sessions_fts.rowid
                WHERE sessions_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_q, limit),
            ).fetchall()

        memories = [dict(r) for r in memory_rows]
        sessions = [dict(r) for r in session_rows]
        return {
            "query": q,
            "total": len(memories) + len(sessions),
            "results": {"memories": memories, "sessions": sessions},
        }

    return router
