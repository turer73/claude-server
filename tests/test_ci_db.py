"""Tests for app/core/ci_db.py — CI test result persistence."""

from __future__ import annotations

import pytest

from app.core import ci_db


@pytest.fixture
async def fresh_db(tmp_path, monkeypatch):
    """Per-test ci_db pointed at a tmp file."""
    monkeypatch.setattr(ci_db, "DB_PATH", str(tmp_path / "ci.db"))
    # Ensure no module-level connection leaks across tests
    if ci_db._conn is not None:
        await ci_db.close_db()
    await ci_db.init_db()
    yield
    await ci_db.close_db()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_init_creates_tables(fresh_db):
    db = ci_db._db()
    cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    rows = await cursor.fetchall()
    names = {r["name"] for r in rows}
    assert {"ci_runs", "ci_project_results", "ci_failures", "ci_test_results"}.issubset(names)


async def test_db_raises_when_not_initialized(monkeypatch, tmp_path):
    """_db() before init_db raises RuntimeError."""
    monkeypatch.setattr(ci_db, "DB_PATH", str(tmp_path / "x.db"))
    if ci_db._conn is not None:
        await ci_db.close_db()
    with pytest.raises(RuntimeError, match="not initialized"):
        ci_db._db()


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


async def test_create_and_complete_run(fresh_db):
    run_id = await ci_db.create_run(trigger="manual")
    assert run_id > 0

    await ci_db.complete_run(run_id, total=10, passed=10, failed=0, duration_s=1.5)

    row = await (await ci_db._db().execute("SELECT * FROM ci_runs WHERE id=?", (run_id,))).fetchone()
    assert row["status"] == "completed"
    assert row["total_tests"] == 10
    assert row["passed"] == 10


async def test_complete_run_with_failures_marks_failed(fresh_db):
    run_id = await ci_db.create_run(trigger="cron")
    await ci_db.complete_run(run_id, total=10, passed=8, failed=2, duration_s=1.0)
    row = await (await ci_db._db().execute("SELECT status FROM ci_runs WHERE id=?", (run_id,))).fetchone()
    assert row["status"] == "failed"


# ---------------------------------------------------------------------------
# Project results & failures
# ---------------------------------------------------------------------------


async def test_save_project_result_and_failure(fresh_db):
    run_id = await ci_db.create_run()

    pid = await ci_db.save_project_result(run_id, "klipper", 100, 99, 1, 5.0, error=None)
    assert pid > 0

    fid = await ci_db.save_failure(run_id, "klipper", "tests/test_x.py", "test_y", "AssertionError")
    assert fid > 0

    # Mark fix attempted - success
    await ci_db.mark_fix_attempted(run_id, "klipper", "test_y", success=True)
    row = await (await ci_db._db().execute("SELECT * FROM ci_failures WHERE id=?", (fid,))).fetchone()
    assert row["fix_attempted"] == 1
    assert row["fix_success"] == 1


# ---------------------------------------------------------------------------
# Individual test results
# ---------------------------------------------------------------------------


async def test_save_tests_bulk_and_query(fresh_db):
    run_id = await ci_db.create_run()
    tests = [
        {"test_file": "a.py", "test_name": "test_a", "status": "passed", "duration_ms": 10},
        {"test_file": "a.py", "test_name": "test_b", "status": "failed", "duration_ms": 20, "error": "boom"},
        {"test_file": "b.py", "test_name": "test_c", "status": "passed", "duration_ms": 5},
    ]
    n = await ci_db.save_tests(run_id, "klipper", tests)
    assert n == 3

    all_tests = await ci_db.get_run_tests(run_id)
    assert len(all_tests) == 3

    failed = await ci_db.get_run_tests(run_id, status="failed")
    assert len(failed) == 1
    assert failed[0]["test_name"] == "test_b"

    by_project = await ci_db.get_run_tests(run_id, project="klipper")
    assert len(by_project) == 3


async def test_save_tests_empty_returns_zero(fresh_db):
    run_id = await ci_db.create_run()
    assert await ci_db.save_tests(run_id, "klipper", []) == 0


async def test_get_test_history(fresh_db):
    """Track a single test name across multiple runs."""
    for status in ["passed", "failed", "passed"]:
        run_id = await ci_db.create_run()
        await ci_db.save_tests(run_id, "klipper", [{"test_name": "test_flaky", "status": status, "duration_ms": 1}])
        await ci_db.complete_run(run_id, 1, 1 if status == "passed" else 0, 0 if status == "passed" else 1, 0.1)

    history = await ci_db.get_test_history("klipper", "test_flaky")
    assert len(history) == 3


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


async def test_get_runs_list(fresh_db):
    for trigger in ["manual", "cron", "manual"]:
        rid = await ci_db.create_run(trigger=trigger)
        await ci_db.complete_run(rid, 10, 10, 0, 1.0)

    runs = await ci_db.get_runs(limit=10)
    assert len(runs) == 3
    # Newest first
    assert runs[0]["id"] > runs[-1]["id"]


async def test_get_run_detail(fresh_db):
    run_id = await ci_db.create_run()
    await ci_db.save_project_result(run_id, "p1", 5, 4, 1, 0.5)
    await ci_db.save_failure(run_id, "p1", "f.py", "test_x", "err")
    await ci_db.complete_run(run_id, 5, 4, 1, 0.5)

    detail = await ci_db.get_run_detail(run_id)
    assert detail["id"] == run_id
    assert len(detail["projects"]) == 1
    assert len(detail["failures"]) == 1


async def test_get_run_detail_missing_returns_none(fresh_db):
    assert await ci_db.get_run_detail(99999) is None


async def test_get_project_history(fresh_db):
    for _ in range(3):
        rid = await ci_db.create_run()
        await ci_db.save_project_result(rid, "klipper", 10, 10, 0, 0.5)
        await ci_db.complete_run(rid, 10, 10, 0, 0.5)

    history = await ci_db.get_project_history("klipper")
    assert len(history) == 3


async def test_get_summary_with_data(fresh_db):
    # Two completed runs: one green, one with failure
    rid1 = await ci_db.create_run()
    await ci_db.save_project_result(rid1, "k", 10, 10, 0, 0.5)
    await ci_db.complete_run(rid1, 10, 10, 0, 0.5)

    rid2 = await ci_db.create_run()
    await ci_db.save_project_result(rid2, "k", 10, 8, 2, 0.5)
    await ci_db.complete_run(rid2, 10, 8, 2, 0.5)

    summary = await ci_db.get_summary()
    assert summary["total_runs"] == 2
    assert summary["last_run"] is not None
    assert summary["success_rate_pct"] == 50.0
    assert any(p["project"] == "k" for p in summary["flaky_projects"])


async def test_get_summary_empty_db(fresh_db):
    summary = await ci_db.get_summary()
    assert summary["total_runs"] == 0
    assert summary["last_run"] is None
    assert summary["success_rate_pct"] == 0
    assert summary["flaky_projects"] == []
