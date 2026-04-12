"""CI/CD API endpoints — run tests, fix failures, check status."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.core.ci_runner import PROJECT_REGISTRY, run_project_tests
from app.core.ci_fixer import attempt_fix
from app.middleware.dependencies import require_admin
from app.models.schemas import (
    CIFailure,
    CIFixRequest,
    CIFixResponse,
    CIProjectResult,
    CIStatusResponse,
    CITestRequest,
    CITestResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ci", tags=["ci"])

# In-memory store for last run status
_last_run: dict | None = None


@router.post(
    "/test",
    response_model=CITestResponse,
    dependencies=[Depends(require_admin)],
)
async def run_tests(body: CITestRequest):
    """Run tests for a single project."""
    result = await run_project_tests(body.project)
    failures = [
        CIFailure(
            test_file=f.get("test_file", "unknown"),
            test_name=f.get("test_name", "unknown"),
            error=f.get("error", "unknown"),
            source_file=f.get("source_file"),
            stack_trace=f.get("stack_trace"),
        )
        for f in result.get("failures", [])
    ]
    return CITestResponse(
        project=result.get("project", body.project),
        total=result.get("total", 0),
        passed=result.get("passed", 0),
        failed=result.get("failed", 0),
        duration_s=result.get("duration_s", 0.0),
        failures=failures,
    )


@router.post(
    "/fix",
    response_model=CIFixResponse,
    dependencies=[Depends(require_admin)],
)
async def fix_failure(body: CIFixRequest):
    """Attempt to fix a single test failure using Claude Code."""
    result = await attempt_fix(
        project=body.project,
        test_file=body.failure.test_file,
        test_name=body.failure.test_name,
        error=body.failure.error,
        source_file=body.failure.source_file,
        max_attempts=body.attempt,
    )
    return CIFixResponse(
        fixed=result.get("fixed", False),
        attempt=result.get("attempt", 0),
        diff=None,
        retry_result=None,
        error=result.get("error"),
    )


@router.get(
    "/status",
    response_model=CIStatusResponse,
    dependencies=[Depends(require_admin)],
)
async def get_status():
    """Return the last run status."""
    if _last_run is None:
        return CIStatusResponse(
            last_run="never",
            total_tests=0,
            passed=0,
            failed=0,
            projects=[],
        )
    return CIStatusResponse(**_last_run)


@router.post(
    "/run-all",
    dependencies=[Depends(require_admin)],
)
async def run_all():
    """Run tests for ALL projects, attempt fixes on failures, return report."""
    global _last_run

    project_results: list[dict] = []
    total_tests = 0
    total_passed = 0
    total_failed = 0

    for project_name in PROJECT_REGISTRY:
        logger.info("CI run-all: testing %s", project_name)
        test_result = await run_project_tests(project_name)

        proj_total = test_result.get("total", 0)
        proj_passed = test_result.get("passed", 0)
        proj_failed = test_result.get("failed", 0)

        fix_attempted = False
        fix_result_str = None

        # Attempt fix if there are failures
        if proj_failed > 0 and test_result.get("failures"):
            fix_attempted = True
            first_failure = test_result["failures"][0]
            fix_res = await attempt_fix(
                project=project_name,
                test_file=first_failure.get("test_file", "unknown"),
                test_name=first_failure.get("test_name", "unknown"),
                error=first_failure.get("error", "unknown"),
                source_file=first_failure.get("source_file"),
            )
            if fix_res.get("fixed"):
                fix_result_str = "fixed"
                # Re-run tests to get updated counts
                retest = await run_project_tests(project_name)
                proj_total = retest.get("total", proj_total)
                proj_passed = retest.get("passed", proj_passed)
                proj_failed = retest.get("failed", proj_failed)
            else:
                fix_result_str = fix_res.get("error", "fix_failed")

        total_tests += proj_total
        total_passed += proj_passed
        total_failed += proj_failed

        project_results.append({
            "project": project_name,
            "total": proj_total,
            "passed": proj_passed,
            "failed": proj_failed,
            "fix_attempted": fix_attempted,
            "fix_result": fix_result_str,
        })

    now = datetime.now(timezone.utc).isoformat()
    _last_run = {
        "last_run": now,
        "total_tests": total_tests,
        "passed": total_passed,
        "failed": total_failed,
        "projects": project_results,
    }

    return _last_run
