"""Tests for CI auto-fixer — prompt builder, Claude Code caller, fix loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.ci_fixer import (
    MAX_ATTEMPTS,
    attempt_fix,
    build_fix_prompt,
    _call_claude_code,
)


class _NoCloseDB:
    """Proxy that forwards attribute access to a wrapped Database
    but makes ``close()`` a no-op.

    Used by attempt_fix tests: the fixture ``ci_db`` owns its lifecycle
    via pytest teardown, so when attempt_fix's ``finally`` clause closes
    the db it received, we don't want it to actually close the shared
    fixture (which would break post-call ``fetch_all`` assertions).
    """

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def close(self):
        return None


# ---------------------------------------------------------------------------
# build_fix_prompt
# ---------------------------------------------------------------------------


class TestBuildFixPrompt:
    def test_basic(self):
        """Prompt must contain project name, test file, test name, error."""
        prompt = build_fix_prompt(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError: 1 != 2",
        )
        assert "klipper" in prompt
        assert "tests/test_foo.py" in prompt
        assert "test_bar" in prompt
        assert "AssertionError: 1 != 2" in prompt

    def test_with_source_file(self):
        """When source_file is given it must appear in the prompt."""
        prompt = build_fix_prompt(
            project="panola",
            test_file="tests/test_x.py",
            test_name="test_y",
            error="KeyError",
            source_file="app/core/handler.py",
        )
        assert "app/core/handler.py" in prompt

    def test_with_prev_errors(self):
        """Previous attempt errors must be included with attempt numbers."""
        prev = ["timeout", "still broken"]
        prompt = build_fix_prompt(
            project="klipper",
            test_file="tests/test_a.py",
            test_name="test_b",
            error="new error",
            prev_errors=prev,
        )
        assert "Deneme 1" in prompt
        assert "timeout" in prompt
        assert "Deneme 2" in prompt
        assert "still broken" in prompt

    def test_no_source_no_prev(self):
        """Without optional args, prompt still contains required fields."""
        prompt = build_fix_prompt(
            project="petvet",
            test_file="tests/test_pet.py",
            test_name="test_create",
            error="ValueError",
        )
        assert "petvet" in prompt
        assert "Ilgili kaynak dosya" not in prompt
        assert "Onceki duzeltme" not in prompt


# ---------------------------------------------------------------------------
# MAX_ATTEMPTS constant
# ---------------------------------------------------------------------------


class TestMaxAttempts:
    def test_max_attempts_is_3(self):
        assert MAX_ATTEMPTS == 3


# ---------------------------------------------------------------------------
# attempt_fix
# ---------------------------------------------------------------------------


class TestAttemptFix:
    @pytest.mark.asyncio
    async def test_calls_claude_and_fixes_on_first_try(self):
        """Mock Claude Code + test runner so fix succeeds on attempt 1."""
        mock_claude = AsyncMock(return_value={
            "answer": "Fixed the assertion",
            "session_id": "sess-123",
            "error": None,
        })
        mock_tests = AsyncMock(return_value={
            "project": "klipper",
            "total": 10,
            "passed": 10,
            "failed": 0,
            "duration_s": 2.0,
            "failures": [],
        })

        with patch("app.core.ci_fixer._call_claude_code", mock_claude), \
             patch("app.core.ci_fixer.run_project_tests", mock_tests):
            result = await attempt_fix(
                project="klipper",
                test_file="tests/test_foo.py",
                test_name="test_bar",
                error="AssertionError",
            )

        assert result["fixed"] is True
        assert result["attempt"] == 1
        assert result["project"] == "klipper"
        mock_claude.assert_called_once()
        mock_tests.assert_called_once_with("klipper")

    @pytest.mark.asyncio
    async def test_retries_on_failure_then_succeeds(self):
        """First 2 test runs fail, 3rd passes."""
        mock_claude = AsyncMock(return_value={
            "answer": "Trying a fix",
            "session_id": "sess-456",
            "error": None,
        })

        # First two runs: still failing.  Third: passes.
        test_results = [
            {
                "project": "klipper",
                "total": 10, "passed": 9, "failed": 1,
                "duration_s": 2.0,
                "failures": [{"test_file": "tests/test_foo.py",
                              "test_name": "test_bar",
                              "error": "still broken"}],
            },
            {
                "project": "klipper",
                "total": 10, "passed": 9, "failed": 1,
                "duration_s": 2.0,
                "failures": [{"test_file": "tests/test_foo.py",
                              "test_name": "test_bar",
                              "error": "different error"}],
            },
            {
                "project": "klipper",
                "total": 10, "passed": 10, "failed": 0,
                "duration_s": 2.0,
                "failures": [],
            },
        ]
        mock_tests = AsyncMock(side_effect=test_results)

        with patch("app.core.ci_fixer._call_claude_code", mock_claude), \
             patch("app.core.ci_fixer.run_project_tests", mock_tests):
            result = await attempt_fix(
                project="klipper",
                test_file="tests/test_foo.py",
                test_name="test_bar",
                error="AssertionError",
            )

        assert result["fixed"] is True
        assert result["attempt"] == 3
        assert mock_claude.call_count == 3
        assert mock_tests.call_count == 3

    @pytest.mark.asyncio
    async def test_returns_false_after_max_attempts(self):
        """All attempts fail -> fixed=False."""
        mock_claude = AsyncMock(return_value={
            "answer": "Trying",
            "session_id": None,
            "error": None,
        })
        mock_tests = AsyncMock(return_value={
            "project": "klipper",
            "total": 10, "passed": 9, "failed": 1,
            "duration_s": 2.0,
            "failures": [{"test_file": "tests/test_foo.py",
                          "test_name": "test_bar",
                          "error": "persistent error"}],
        })

        with patch("app.core.ci_fixer._call_claude_code", mock_claude), \
             patch("app.core.ci_fixer.run_project_tests", mock_tests):
            result = await attempt_fix(
                project="klipper",
                test_file="tests/test_foo.py",
                test_name="test_bar",
                error="AssertionError",
            )

        assert result["fixed"] is False
        assert result["attempt"] == 3
        assert mock_claude.call_count == 3

    @pytest.mark.asyncio
    async def test_unknown_project(self):
        """Unknown project returns error without calling Claude."""
        result = await attempt_fix(
            project="nonexistent",
            test_file="tests/test.py",
            test_name="test_x",
            error="err",
        )
        assert result["fixed"] is False
        assert result["attempt"] == 0
        assert "Bilinmeyen proje" in result["error"]

    @pytest.mark.asyncio
    async def test_claude_error_counts_as_failed_attempt(self):
        """If Claude Code returns an error, it should still count as an attempt."""
        mock_claude = AsyncMock(return_value={
            "answer": "",
            "session_id": None,
            "error": "Claude Code CLI bulunamadi",
        })

        with patch("app.core.ci_fixer._call_claude_code", mock_claude):
            result = await attempt_fix(
                project="klipper",
                test_file="tests/test_foo.py",
                test_name="test_bar",
                error="AssertionError",
            )

        assert result["fixed"] is False
        assert result["attempt"] == 3
        assert mock_claude.call_count == 3


@pytest.mark.asyncio
async def test_attempt_fix_records_a_lesson_per_attempt(ci_db, monkeypatch):
    """Every iteration inside attempt_fix must insert a row into ci_lesson_learned."""
    from unittest.mock import AsyncMock, patch

    async def fake_open_ci_db():
        return _NoCloseDB(ci_db)

    monkeypatch.setattr("app.core.ci_fixer._open_ci_db", fake_open_ci_db)

    mock_claude = AsyncMock(return_value={
        "answer": "Fixed it",
        "session_id": "sess-1",
        "error": None,
    })
    mock_tests = AsyncMock(return_value={
        "project": "klipper",
        "total": 10, "passed": 10, "failed": 0,
        "duration_s": 2.0, "failures": [],
    })

    with patch("app.core.ci_fixer._call_claude_code", mock_claude), \
         patch("app.core.ci_fixer.run_project_tests", mock_tests):
        result = await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError: 1 != 2",
        )

    assert result["fixed"] is True
    rows = await ci_db.fetch_all(
        "SELECT project, test_name, outcome, strategy, attempt_num, run_uuid "
        "FROM ci_lesson_learned ORDER BY id"
    )
    assert len(rows) == 1
    assert rows[0]["project"] == "klipper"
    assert rows[0]["test_name"] == "test_bar"
    assert rows[0]["outcome"] == "passed"
    assert rows[0]["strategy"] == "fix-direct"
    assert rows[0]["attempt_num"] == 1


@pytest.mark.asyncio
async def test_attempt_fix_all_attempts_share_one_run_uuid(ci_db, monkeypatch):
    """When attempt_fix retries 3 times, all rows must share the same run_uuid."""
    from unittest.mock import AsyncMock, patch

    async def fake_open_ci_db():
        return _NoCloseDB(ci_db)

    monkeypatch.setattr("app.core.ci_fixer._open_ci_db", fake_open_ci_db)

    mock_claude = AsyncMock(return_value={"answer": "", "session_id": None, "error": None})
    mock_tests = AsyncMock(return_value={
        "project": "klipper",
        "total": 10, "passed": 9, "failed": 1,
        "duration_s": 2.0,
        "failures": [{"test_file": "tests/test_foo.py",
                      "test_name": "test_bar",
                      "error": "still broken"}],
    })

    with patch("app.core.ci_fixer._call_claude_code", mock_claude), \
         patch("app.core.ci_fixer.run_project_tests", mock_tests):
        await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError",
        )

    rows = await ci_db.fetch_all(
        "SELECT run_uuid, attempt_num, outcome FROM ci_lesson_learned ORDER BY id"
    )
    assert len(rows) == 3
    assert {r["run_uuid"] for r in rows} == {rows[0]["run_uuid"]}  # all identical
    assert [r["attempt_num"] for r in rows] == [1, 2, 3]
    assert all(r["outcome"] == "failed" for r in rows)
