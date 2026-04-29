"""CI test results database — persistent storage for all test runs.

Stores run history, per-project results, and individual failures
so we can track trends, regressions, and fix success rates.
"""

from __future__ import annotations

import aiosqlite
import logging

logger = logging.getLogger(__name__)

DB_PATH = "/opt/linux-ai-server/data/ci_tests.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ci_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    total_tests INTEGER DEFAULT 0,
    passed INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    duration_s REAL DEFAULT 0,
    trigger TEXT DEFAULT 'manual',
    status TEXT DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS ci_project_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    project TEXT NOT NULL,
    total INTEGER DEFAULT 0,
    passed INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    duration_s REAL DEFAULT 0,
    error TEXT,
    FOREIGN KEY (run_id) REFERENCES ci_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ci_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    project TEXT NOT NULL,
    test_file TEXT,
    test_name TEXT NOT NULL,
    error TEXT,
    fix_attempted INTEGER DEFAULT 0,
    fix_success INTEGER DEFAULT 0,
    FOREIGN KEY (run_id) REFERENCES ci_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ci_runs_started ON ci_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ci_runs_status ON ci_runs(status);
CREATE INDEX IF NOT EXISTS idx_ci_results_run ON ci_project_results(run_id);
CREATE INDEX IF NOT EXISTS idx_ci_results_project ON ci_project_results(project);
CREATE INDEX IF NOT EXISTS idx_ci_failures_run ON ci_failures(run_id);
CREATE INDEX IF NOT EXISTS idx_ci_failures_project ON ci_failures(project);

CREATE TABLE IF NOT EXISTS ci_test_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    project TEXT NOT NULL,
    test_file TEXT,
    test_name TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER DEFAULT 0,
    error TEXT,
    FOREIGN KEY (run_id) REFERENCES ci_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ci_tests_run ON ci_test_results(run_id);
CREATE INDEX IF NOT EXISTS idx_ci_tests_project ON ci_test_results(project);
CREATE INDEX IF NOT EXISTS idx_ci_tests_status ON ci_test_results(status);
"""

_conn: aiosqlite.Connection | None = None


async def init_db() -> None:
    """Initialize the CI database and create tables."""
    global _conn
    _conn = await aiosqlite.connect(DB_PATH)
    _conn.row_factory = aiosqlite.Row
    await _conn.executescript(SCHEMA)
    await _conn.commit()
    logger.info("CI database initialized at %s", DB_PATH)


async def close_db() -> None:
    """Close the database connection."""
    global _conn
    if _conn:
        await _conn.close()
        _conn = None


def _db() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("CI database not initialized. Call init_db() first.")
    return _conn


# â”€â”€ Run lifecycle â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def create_run(trigger: str = "manual") -> int:
    """Start a new CI run, return its ID."""
    cursor = await _db().execute(
        "INSERT INTO ci_runs (trigger, status) VALUES (?, 'running')",
        (trigger,),
    )
    await _db().commit()
    return cursor.lastrowid


async def complete_run(
    run_id: int, total: int, passed: int, failed: int, duration_s: float
) -> None:
    """Mark a run as completed with aggregate stats."""
    await _db().execute(
        """UPDATE ci_runs
           SET completed_at = datetime('now'),
               total_tests = ?, passed = ?, failed = ?,
               duration_s = ?, status = ?
           WHERE id = ?""",
        (total, passed, failed, duration_s, "failed" if failed > 0 else "completed", run_id),
    )
    await _db().commit()


# â”€â”€ Project results â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def save_project_result(
    run_id: int,
    project: str,
    total: int,
    passed: int,
    failed: int,
    duration_s: float,
    error: str | None = None,
) -> int:
    """Save a single project's test result within a run."""
    cursor = await _db().execute(
        """INSERT INTO ci_project_results
           (run_id, project, total, passed, failed, duration_s, error)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (run_id, project, total, passed, failed, duration_s, error),
    )
    await _db().commit()
    return cursor.lastrowid


# â”€â”€ Failures â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def save_failure(
    run_id: int,
    project: str,
    test_file: str | None,
    test_name: str,
    error: str | None,
) -> int:
    """Record an individual test failure."""
    cursor = await _db().execute(
        """INSERT INTO ci_failures
           (run_id, project, test_file, test_name, error)
           VALUES (?, ?, ?, ?, ?)""",
        (run_id, project, test_file, test_name, error),
    )
    await _db().commit()
    return cursor.lastrowid


async def mark_fix_attempted(
    run_id: int, project: str, test_name: str, success: bool
) -> None:
    """Update a failure record after a fix attempt."""
    await _db().execute(
        """UPDATE ci_failures
           SET fix_attempted = 1, fix_success = ?
           WHERE run_id = ? AND project = ? AND test_name = ?""",
        (int(success), run_id, project, test_name),
    )
    await _db().commit()


# â”€â”€ Individual test results â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def save_tests(run_id: int, project: str, tests: list[dict]) -> int:
    """Bulk-save individual test results. Returns count saved."""
    if not tests:
        return 0
    db = _db()
    for t in tests:
        await db.execute(
            """INSERT INTO ci_test_results
               (run_id, project, test_file, test_name, status, duration_ms, error)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                project,
                t.get("test_file"),
                t.get("test_name", "unknown"),
                t.get("status", "unknown"),
                t.get("duration_ms", 0),
                t.get("error"),
            ),
        )
    await db.commit()
    return len(tests)


async def get_run_tests(
    run_id: int, project: str | None = None, status: str | None = None
) -> list[dict]:
    """Query individual test results for a run, optionally filtered."""
    sql = "SELECT * FROM ci_test_results WHERE run_id = ?"
    params: list = [run_id]
    if project:
        sql += " AND project = ?"
        params.append(project)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY project, test_file, test_name"
    cursor = await _db().execute(sql, tuple(params))
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_test_history(project: str, test_name: str, limit: int = 20) -> list[dict]:
    """Track a specific test across runs (flaky detection)."""
    cursor = await _db().execute(
        """SELECT t.run_id, r.started_at, t.status, t.duration_ms, t.error
           FROM ci_test_results t
           JOIN ci_runs r ON r.id = t.run_id
           WHERE t.project = ? AND t.test_name = ?
           ORDER BY r.id DESC LIMIT ?""",
        (project, test_name, limit),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# â”€â”€ Queries â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def get_runs(limit: int = 20, offset: int = 0) -> list[dict]:
    """Return recent CI runs, newest first."""
    cursor = await _db().execute(
        """SELECT id, started_at, completed_at, total_tests, passed, failed,
                  duration_s, trigger, status
           FROM ci_runs ORDER BY id DESC LIMIT ? OFFSET ?""",
        (limit, offset),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_run_detail(run_id: int) -> dict | None:
    """Return a full run with project results and failures."""
    cursor = await _db().execute(
        "SELECT * FROM ci_runs WHERE id = ?", (run_id,)
    )
    run = await cursor.fetchone()
    if not run:
        return None

    cursor = await _db().execute(
        "SELECT * FROM ci_project_results WHERE run_id = ? ORDER BY project",
        (run_id,),
    )
    projects = [dict(r) for r in await cursor.fetchall()]

    cursor = await _db().execute(
        "SELECT * FROM ci_failures WHERE run_id = ? ORDER BY project, test_name",
        (run_id,),
    )
    failures = [dict(r) for r in await cursor.fetchall()]

    return {**dict(run), "projects": projects, "failures": failures}


async def get_project_history(project: str, limit: int = 30) -> list[dict]:
    """Return test history for a single project across runs."""
    cursor = await _db().execute(
        """SELECT r.id AS run_id, r.started_at, r.trigger,
                  p.total, p.passed, p.failed, p.duration_s, p.error
           FROM ci_project_results p
           JOIN ci_runs r ON r.id = p.run_id
           WHERE p.project = ?
           ORDER BY r.id DESC LIMIT ?""",
        (project, limit),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_summary() -> dict:
    """Return overall CI summary stats."""
    # Last run
    cursor = await _db().execute(
        "SELECT * FROM ci_runs ORDER BY id DESC LIMIT 1"
    )
    last_run = await cursor.fetchone()

    # Total runs count
    cursor = await _db().execute("SELECT COUNT(*) as cnt FROM ci_runs")
    total_runs = (await cursor.fetchone())["cnt"]

    # Success rate (last 30 runs)
    cursor = await _db().execute(
        """SELECT COUNT(*) as total,
                  SUM(CASE WHEN failed = 0 THEN 1 ELSE 0 END) as green
           FROM ci_runs WHERE status != 'running'
           ORDER BY id DESC LIMIT 30"""
    )
    rate_row = await cursor.fetchone()
    success_rate = (
        round(rate_row["green"] / rate_row["total"] * 100, 1)
        if rate_row["total"] > 0
        else 0
    )

    # Most failing projects (last 30 runs)
    cursor = await _db().execute(
        """SELECT project, SUM(failed) as total_failures, COUNT(*) as runs
           FROM ci_project_results
           WHERE run_id IN (SELECT id FROM ci_runs ORDER BY id DESC LIMIT 30)
           GROUP BY project
           HAVING total_failures > 0
           ORDER BY total_failures DESC"""
    )
    flaky = [dict(r) for r in await cursor.fetchall()]

    return {
        "total_runs": total_runs,
        "last_run": dict(last_run) if last_run else None,
        "success_rate_pct": success_rate,
        "flaky_projects": flaky,
    }
