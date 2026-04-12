"""Tests for CI/CD Pydantic models."""

import pytest
from pydantic import ValidationError

from app.models.schemas import (
    VALID_CI_PROJECTS,
    CIFailure,
    CIFixRequest,
    CIFixResponse,
    CIProjectResult,
    CIStatusResponse,
    CITestRequest,
    CITestResponse,
)


# --- CITestRequest ---


def test_ci_test_request_valid():
    req = CITestRequest(project="panola", test_type="unit")
    assert req.project == "panola"
    assert req.test_type == "unit"


def test_ci_test_request_default_test_type():
    req = CITestRequest(project="klipper")
    assert req.test_type == "all"


def test_ci_test_request_all_valid_projects():
    for proj in VALID_CI_PROJECTS:
        req = CITestRequest(project=proj)
        assert req.project == proj


def test_ci_test_request_rejects_unknown_project():
    with pytest.raises(ValidationError, match="Unknown project"):
        CITestRequest(project="nonexistent-project")


def test_ci_test_request_rejects_empty_project():
    with pytest.raises(ValidationError, match="Unknown project"):
        CITestRequest(project="")


# --- CIFailure ---


def test_ci_failure_valid():
    f = CIFailure(
        test_file="tests/test_foo.py",
        test_name="test_bar",
        error="AssertionError: expected 1 got 2",
    )
    assert f.test_file == "tests/test_foo.py"
    assert f.test_name == "test_bar"
    assert "AssertionError" in f.error


def test_ci_failure_optional_fields_default_none():
    f = CIFailure(
        test_file="tests/test_foo.py",
        test_name="test_bar",
        error="fail",
    )
    assert f.source_file is None
    assert f.stack_trace is None


def test_ci_failure_with_optional_fields():
    f = CIFailure(
        test_file="tests/test_foo.py",
        test_name="test_bar",
        error="fail",
        source_file="app/core/engine.py",
        stack_trace="Traceback ...",
    )
    assert f.source_file == "app/core/engine.py"
    assert f.stack_trace == "Traceback ..."


# --- CITestResponse ---


def test_ci_test_response_valid():
    resp = CITestResponse(
        project="panola",
        total=100,
        passed=95,
        failed=5,
        duration_s=12.5,
    )
    assert resp.project == "panola"
    assert resp.total == 100
    assert resp.passed == 95
    assert resp.failed == 5
    assert resp.duration_s == 12.5
    assert resp.failures == []


def test_ci_test_response_total_equals_passed_plus_failed():
    resp = CITestResponse(
        project="kuafor-panel",
        total=50,
        passed=48,
        failed=2,
        duration_s=3.2,
    )
    assert resp.total == resp.passed + resp.failed


def test_ci_test_response_with_failures():
    failure = CIFailure(
        test_file="tests/test_a.py",
        test_name="test_x",
        error="boom",
    )
    resp = CITestResponse(
        project="petvet",
        total=10,
        passed=9,
        failed=1,
        duration_s=1.0,
        failures=[failure],
    )
    assert len(resp.failures) == 1
    assert resp.failures[0].test_name == "test_x"


# --- CIFixRequest ---


def test_ci_fix_request_valid():
    failure = CIFailure(
        test_file="tests/test_a.py",
        test_name="test_x",
        error="boom",
    )
    req = CIFixRequest(project="panola", failure=failure)
    assert req.project == "panola"
    assert req.attempt == 1
    assert req.prev_errors == []


def test_ci_fix_request_with_attempt_and_prev_errors():
    failure = CIFailure(
        test_file="tests/test_a.py",
        test_name="test_x",
        error="boom",
    )
    req = CIFixRequest(
        project="panola",
        failure=failure,
        attempt=3,
        prev_errors=["err1", "err2"],
    )
    assert req.attempt == 3
    assert len(req.prev_errors) == 2


# --- CIFixResponse ---


def test_ci_fix_response_success():
    resp = CIFixResponse(
        fixed=True,
        attempt=1,
        diff="--- a/foo.py\n+++ b/foo.py",
    )
    assert resp.fixed is True
    assert resp.attempt == 1
    assert resp.diff is not None
    assert resp.retry_result is None
    assert resp.error is None


def test_ci_fix_response_failure():
    resp = CIFixResponse(
        fixed=False,
        attempt=2,
        error="Could not resolve import",
    )
    assert resp.fixed is False
    assert resp.error == "Could not resolve import"


def test_ci_fix_response_optional_fields_default_none():
    resp = CIFixResponse(fixed=False, attempt=1)
    assert resp.diff is None
    assert resp.retry_result is None
    assert resp.error is None


# --- CIProjectResult ---


def test_ci_project_result_valid():
    pr = CIProjectResult(
        project="renderhane",
        total=20,
        passed=18,
        failed=2,
        fix_attempted=True,
        fix_result="partial",
    )
    assert pr.project == "renderhane"
    assert pr.fix_attempted is True
    assert pr.fix_result == "partial"


def test_ci_project_result_fix_result_default_none():
    pr = CIProjectResult(
        project="klipper",
        total=430,
        passed=430,
        failed=0,
        fix_attempted=False,
    )
    assert pr.fix_result is None


# --- CIStatusResponse ---


def test_ci_status_response_valid():
    proj = CIProjectResult(
        project="panola",
        total=100,
        passed=98,
        failed=2,
        fix_attempted=True,
        fix_result="ok",
    )
    status = CIStatusResponse(
        last_run="2026-04-12T10:00:00Z",
        total_tests=100,
        passed=98,
        failed=2,
        projects=[proj],
    )
    assert status.last_run == "2026-04-12T10:00:00Z"
    assert status.total_tests == 100
    assert len(status.projects) == 1
    assert status.projects[0].project == "panola"


def test_ci_status_response_multiple_projects():
    projects = [
        CIProjectResult(
            project=p,
            total=10,
            passed=10,
            failed=0,
            fix_attempted=False,
        )
        for p in ["panola", "kuafor-panel", "petvet"]
    ]
    status = CIStatusResponse(
        last_run="2026-04-12T12:00:00Z",
        total_tests=30,
        passed=30,
        failed=0,
        projects=projects,
    )
    assert len(status.projects) == 3
    assert status.total_tests == sum(p.total for p in status.projects)
