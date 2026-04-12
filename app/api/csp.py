"""
CSP Violation Reporting API
Merkezi CSP violation toplama, dedup, sorgulama.
VPS csp-collector bu endpoint'e batch gonderir.
"""
import sqlite3
import os
from datetime import datetime
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Optional

DB_PATH = "/opt/linux-ai-server/data/claude_memory.db"

MEMORY_API_KEY = os.environ.get("MEMORY_API_KEY", "")
if not MEMORY_API_KEY:
    _env_path = "/opt/linux-ai-server/.env"
    if os.path.exists(_env_path):
        with open(_env_path) as _f:
            for _line in _f:
                if _line.startswith("MEMORY_API_KEY="):
                    MEMORY_API_KEY = _line.strip().split("=", 1)[1]
                    break

router = APIRouter(prefix="/api/v1/csp", tags=["csp"])


def _check_key(key: str):
    if not MEMORY_API_KEY or key != MEMORY_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


class ViolationReport(BaseModel):
    site: str
    directive: str
    blocked_uri: str
    source_file: Optional[str] = None
    disposition: str = "enforce"
    user_agent: Optional[str] = None
    hit_count: int = 1


class BatchReport(BaseModel):
    violations: list[ViolationReport]


@router.post("/report")
def receive_violations(
    batch: BatchReport,
    x_memory_key: str = Header(..., alias="X-Memory-Key"),
):
    """VPS collector'dan batch violation al. Upsert (dedup) uygula."""
    _check_key(x_memory_key)
    now = datetime.utcnow().isoformat(timespec="seconds")
    db = _get_db()
    new_count = 0
    updated_count = 0

    try:
        for v in batch.violations:
            # Try insert — if UNIQUE conflict, update hit_count
            cur = db.execute(
                """INSERT INTO csp_violations
                   (site, directive, blocked_uri, source_file, disposition, user_agent, first_seen_at, last_seen_at, hit_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(site, directive, blocked_uri) DO UPDATE SET
                     hit_count = hit_count + excluded.hit_count,
                     last_seen_at = excluded.last_seen_at,
                     user_agent = COALESCE(excluded.user_agent, user_agent),
                     source_file = COALESCE(excluded.source_file, source_file),
                     resolved = 0""",
                (v.site, v.directive, v.blocked_uri, v.source_file,
                 v.disposition, v.user_agent, now, now, v.hit_count),
            )
            if cur.lastrowid and db.execute(
                "SELECT hit_count FROM csp_violations WHERE site=? AND directive=? AND blocked_uri=?",
                (v.site, v.directive, v.blocked_uri)
            ).fetchone()["hit_count"] == v.hit_count:
                new_count += 1
            else:
                updated_count += 1

        db.commit()
    finally:
        db.close()

    return {"new": new_count, "updated": updated_count, "total": len(batch.violations)}


@router.get("/violations")
def list_violations(
    site: Optional[str] = None,
    resolved: Optional[int] = None,
    limit: int = 50,
    x_memory_key: str = Header(..., alias="X-Memory-Key"),
):
    """Violation listesi — filtreleme destekli."""
    _check_key(x_memory_key)
    db = _get_db()
    conditions = []
    params = []
    if site:
        conditions.append("site = ?")
        params.append(site)
    if resolved is not None:
        conditions.append("resolved = ?")
        params.append(resolved)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = db.execute(
        f"SELECT * FROM csp_violations {where} ORDER BY last_seen_at DESC LIMIT ?",
        params + [limit]
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@router.get("/summary")
def violation_summary(
    x_memory_key: str = Header(..., alias="X-Memory-Key"),
):
    """Site bazli ozet — dashboard icin."""
    _check_key(x_memory_key)
    db = _get_db()
    rows = db.execute("""
        SELECT site,
               COUNT(*) as unique_violations,
               SUM(hit_count) as total_hits,
               SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) as open_count,
               MAX(last_seen_at) as last_violation
        FROM csp_violations
        GROUP BY site
        ORDER BY open_count DESC, total_hits DESC
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]


@router.post("/resolve/{violation_id}")
def resolve_violation(
    violation_id: int,
    x_memory_key: str = Header(..., alias="X-Memory-Key"),
):
    """Violation'i cozuldu olarak isaretle."""
    _check_key(x_memory_key)
    db = _get_db()
    cur = db.execute(
        "UPDATE csp_violations SET resolved=1 WHERE id=?", (violation_id,)
    )
    db.commit()
    db.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Violation not found")
    return {"resolved": violation_id}
