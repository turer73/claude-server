"""End-to-end: 3 consecutive attempt_fix calls, same signature, enriched on 3rd."""

from unittest.mock import AsyncMock, patch

import pytest

from app.core.ci_fixer import attempt_fix


class _NoCloseDB:
    """Proxy that suppresses close() so the shared ci_db fixture isn't torn
    down when attempt_fix's finally block runs. Pattern also used in
    tests/test_ci_fixer.py for Task 7/8/9 tests."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def close(self):
        return None


async def _one_call(ci_db, monkeypatch, *, claude_result, test_result):
    async def fake_open_ci_db():
        return _NoCloseDB(ci_db)

    monkeypatch.setattr("app.core.ci_fixer._open_ci_db", fake_open_ci_db)

    mock_claude = AsyncMock(return_value=claude_result)
    mock_tests = AsyncMock(return_value=test_result)

    with (
        patch("app.core.ci_fixer._call_claude_code", mock_claude),
        patch("app.core.ci_fixer.run_project_tests", mock_tests),
        patch("app.core.ci_fixer.compute_signature", return_value=("h", "klipper::test_bar::h")),
        patch("app.core.ci_fixer.post_lesson_summary_to_memory_api", AsyncMock()),
    ):
        return await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError",
        )


@pytest.mark.asyncio
async def test_enrichment_kicks_in_after_two_failing_calls(ci_db, monkeypatch):
    """End-to-end: strategy flips from fix-direct to context-enriched once the
    dedup check sees >=2 failed rows for the signature in the recent window.

    Spec caveat: the plan's Task 11 prose assumed "recent == 2" would only be
    reached after two separate failing calls. In practice, Task 3 defines
    ``get_recent_occurrences`` to count failed *rows* (not distinct failing
    run_uuids) within the ``window`` most recent run_uuids — see
    ``tests/test_ci_signal_dedup.py::test_get_recent_occurrences_counts_per_run_uuid``
    which explicitly encodes that semantics. Consequence: within a single call
    whose run_uuid accumulates 2 failed attempts, the 3rd attempt already sees
    ``recent == 2`` and flips to context-enriched. The test below asserts the
    actual end-to-end behavior produced by T1-T10's committed code: the flip
    happens mid-call-1 at attempt 3, and every attempt thereafter stays
    context-enriched."""
    failed_result = {
        "project": "klipper",
        "total": 1,
        "passed": 0,
        "failed": 1,
        "duration_s": 0.1,
        "failures": [{"test_file": "tests/test_foo.py", "test_name": "test_bar", "error": "still broken"}],
    }
    passed_result = {
        "project": "klipper",
        "total": 1,
        "passed": 1,
        "failed": 0,
        "duration_s": 0.1,
        "failures": [],
    }
    claude = {"answer": "try", "session_id": None, "error": None}

    # Call 1: MAX_ATTEMPTS=3, all fail -> 3 rows with the same run_uuid.
    await _one_call(ci_db, monkeypatch, claude_result=claude, test_result=failed_result)
    # Call 2: new run_uuid, 3 more failing attempts.
    await _one_call(ci_db, monkeypatch, claude_result=claude, test_result=failed_result)
    # Call 3: new run_uuid, passes first try -> 1 row.
    await _one_call(ci_db, monkeypatch, claude_result=claude, test_result=passed_result)

    rows = await ci_db.fetch_all("SELECT strategy, outcome, attempt_num FROM ci_lesson_learned ORDER BY id")
    strategies = [r["strategy"] for r in rows]
    outcomes = [r["outcome"] for r in rows]

    # MAX_ATTEMPTS=3 x 2 failing calls + 1 passing attempt = 7 rows total.
    assert len(rows) == 7

    # Call 1's attempts 1-2 see recent < 2 (0 then 1 prior failed row in U1)
    # => fix-direct. Call 1's attempt 3 sees recent == 2 => context-enriched.
    assert strategies[:2] == ["fix-direct", "fix-direct"]
    assert outcomes[:2] == ["failed", "failed"]

    # Every subsequent attempt (the rest of call 1, all of call 2, and call 3)
    # sees >=2 prior failed rows in the window, so strategy is context-enriched.
    assert all(s == "context-enriched" for s in strategies[2:])

    # Calls 1 and 2 fail throughout (6 failed rows); call 3's single row passes.
    assert outcomes[:6] == ["failed"] * 6
    assert outcomes[-1] == "passed"

    # Lock down the run_uuid grouping shape: call 1 (3 attempts) + call 2
    # (3 attempts) + call 3 (1 attempt) must land in 3 distinct run_uuids
    # with row counts [3, 3, 1] in insertion order. Guards against a
    # refactor that collapses uuid.uuid4() into a module-level constant.
    run_uuid_groups = await ci_db.fetch_all("SELECT run_uuid, COUNT(*) AS n FROM ci_lesson_learned GROUP BY run_uuid ORDER BY MIN(id)")
    assert [r["n"] for r in run_uuid_groups] == [3, 3, 1]
    assert len({r["run_uuid"] for r in run_uuid_groups}) == 3
