"""Tests for CI auto-fixer — prompt builder, Claude Code caller, fix loop."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.core.ci_fixer import (
    MAX_ATTEMPTS,
    attempt_fix,
    build_fix_prompt,
    post_lesson_summary_to_memory_api,
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
        mock_claude = AsyncMock(
            return_value={
                "answer": "Fixed the assertion",
                "session_id": "sess-123",
                "error": None,
            }
        )
        mock_tests = AsyncMock(
            return_value={
                "project": "klipper",
                "total": 10,
                "passed": 10,
                "failed": 0,
                "duration_s": 2.0,
                "failures": [],
            }
        )

        with patch("app.core.ci_fixer._call_claude_code", mock_claude), patch("app.core.ci_fixer.run_project_tests", mock_tests):
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
        mock_claude = AsyncMock(
            return_value={
                "answer": "Trying a fix",
                "session_id": "sess-456",
                "error": None,
            }
        )

        # First two runs: still failing.  Third: passes.
        test_results = [
            {
                "project": "klipper",
                "total": 10,
                "passed": 9,
                "failed": 1,
                "duration_s": 2.0,
                "failures": [{"test_file": "tests/test_foo.py", "test_name": "test_bar", "error": "still broken"}],
            },
            {
                "project": "klipper",
                "total": 10,
                "passed": 9,
                "failed": 1,
                "duration_s": 2.0,
                "failures": [{"test_file": "tests/test_foo.py", "test_name": "test_bar", "error": "different error"}],
            },
            {
                "project": "klipper",
                "total": 10,
                "passed": 10,
                "failed": 0,
                "duration_s": 2.0,
                "failures": [],
            },
        ]
        mock_tests = AsyncMock(side_effect=test_results)

        with patch("app.core.ci_fixer._call_claude_code", mock_claude), patch("app.core.ci_fixer.run_project_tests", mock_tests):
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
        mock_claude = AsyncMock(
            return_value={
                "answer": "Trying",
                "session_id": None,
                "error": None,
            }
        )
        mock_tests = AsyncMock(
            return_value={
                "project": "klipper",
                "total": 10,
                "passed": 9,
                "failed": 1,
                "duration_s": 2.0,
                "failures": [{"test_file": "tests/test_foo.py", "test_name": "test_bar", "error": "persistent error"}],
            }
        )

        with patch("app.core.ci_fixer._call_claude_code", mock_claude), patch("app.core.ci_fixer.run_project_tests", mock_tests):
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
        mock_claude = AsyncMock(
            return_value={
                "answer": "",
                "session_id": None,
                "error": "Claude Code CLI bulunamadi",
            }
        )

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

    mock_claude = AsyncMock(
        return_value={
            "answer": "Fixed it",
            "session_id": "sess-1",
            "error": None,
        }
    )
    mock_tests = AsyncMock(
        return_value={
            "project": "klipper",
            "total": 10,
            "passed": 10,
            "failed": 0,
            "duration_s": 2.0,
            "failures": [],
        }
    )

    with patch("app.core.ci_fixer._call_claude_code", mock_claude), patch("app.core.ci_fixer.run_project_tests", mock_tests):
        result = await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError: 1 != 2",
        )

    assert result["fixed"] is True
    rows = await ci_db.fetch_all("SELECT project, test_name, outcome, strategy, attempt_num, run_uuid FROM ci_lesson_learned ORDER BY id")
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
    mock_tests = AsyncMock(
        return_value={
            "project": "klipper",
            "total": 10,
            "passed": 9,
            "failed": 1,
            "duration_s": 2.0,
            "failures": [{"test_file": "tests/test_foo.py", "test_name": "test_bar", "error": "still broken"}],
        }
    )

    with patch("app.core.ci_fixer._call_claude_code", mock_claude), patch("app.core.ci_fixer.run_project_tests", mock_tests):
        await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError",
        )

    rows = await ci_db.fetch_all("SELECT run_uuid, attempt_num, outcome FROM ci_lesson_learned ORDER BY id")
    assert len(rows) == 3
    assert {r["run_uuid"] for r in rows} == {rows[0]["run_uuid"]}  # all identical
    assert [r["attempt_num"] for r in rows] == [1, 2, 3]
    assert all(r["outcome"] == "failed" for r in rows)


@pytest.mark.asyncio
async def test_attempt_fix_posts_memory_summary_on_success(ci_db, monkeypatch):
    from unittest.mock import AsyncMock, patch

    async def fake_open_ci_db():
        return _NoCloseDB(ci_db)

    monkeypatch.setattr("app.core.ci_fixer._open_ci_db", fake_open_ci_db)

    posted = []

    async def fake_post(**kw):
        posted.append(kw)

    mock_claude = AsyncMock(return_value={"answer": "done", "session_id": None, "error": None})
    mock_tests = AsyncMock(
        return_value={
            "project": "klipper",
            "total": 1,
            "passed": 1,
            "failed": 0,
            "duration_s": 0.1,
            "failures": [],
        }
    )

    with (
        patch("app.core.ci_fixer._call_claude_code", mock_claude),
        patch("app.core.ci_fixer.run_project_tests", mock_tests),
        patch("app.core.ci_fixer.post_lesson_summary_to_memory_api", fake_post),
    ):
        result = await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError",
        )

    assert result["fixed"] is True
    assert len(posted) == 1
    assert posted[0]["lesson_type"] == "lesson_learned"
    assert "klipper" in posted[0]["name"]


@pytest.mark.asyncio
async def test_attempt_fix_skips_memory_post_on_failure(ci_db, monkeypatch):
    from unittest.mock import AsyncMock, patch

    async def fake_open_ci_db():
        return _NoCloseDB(ci_db)

    monkeypatch.setattr("app.core.ci_fixer._open_ci_db", fake_open_ci_db)

    posted = []

    async def fake_post(**kw):
        posted.append(kw)

    mock_claude = AsyncMock(return_value={"answer": "", "session_id": None, "error": None})
    mock_tests = AsyncMock(
        return_value={
            "project": "klipper",
            "total": 1,
            "passed": 0,
            "failed": 1,
            "duration_s": 0.1,
            "failures": [{"test_file": "tests/test_foo.py", "test_name": "test_bar", "error": "boom"}],
        }
    )

    with (
        patch("app.core.ci_fixer._call_claude_code", mock_claude),
        patch("app.core.ci_fixer.run_project_tests", mock_tests),
        patch("app.core.ci_fixer.post_lesson_summary_to_memory_api", fake_post),
    ):
        await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError",
        )

    assert posted == []


@pytest.mark.asyncio
async def test_strategy_switches_to_context_enriched_after_2_failures(ci_db, monkeypatch):
    """If 2 past runs failed with the same signature, 3rd run uses context-enriched."""
    from unittest.mock import AsyncMock, patch

    async def fake_open_ci_db():
        return _NoCloseDB(ci_db)

    monkeypatch.setattr("app.core.ci_fixer._open_ci_db", fake_open_ci_db)

    # Seed 2 past failures with a known signature
    sig = "klipper::test_bar::h"
    for u in ("u-old-1", "u-old-2"):
        await ci_db.execute(
            """INSERT INTO ci_lesson_learned
               (run_uuid, project, test_name, error_hash, signature, raw_error,
                attempt_num, strategy, context_lessons, fix_diff, outcome, duration_ms)
               VALUES (?, 'klipper', 'test_bar', 'h', ?, 'e', 1,
                       'fix-direct', NULL, NULL, 'failed', 0)""",
            (u, sig),
        )

    mock_claude = AsyncMock(return_value={"answer": "fix", "session_id": None, "error": None})
    mock_tests = AsyncMock(
        return_value={
            "project": "klipper",
            "total": 1,
            "passed": 1,
            "failed": 0,
            "duration_s": 0.1,
            "failures": [],
        }
    )

    with (
        patch("app.core.ci_fixer._call_claude_code", mock_claude),
        patch("app.core.ci_fixer.run_project_tests", mock_tests),
        patch("app.core.ci_fixer.compute_signature", return_value=("h", sig)),
        patch("app.core.ci_fixer.post_lesson_summary_to_memory_api", AsyncMock()),
    ):
        await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError",
        )

    latest = await ci_db.fetch_one("SELECT strategy FROM ci_lesson_learned ORDER BY id DESC LIMIT 1")
    assert latest["strategy"] == "context-enriched"


@pytest.mark.asyncio
async def test_dedup_disabled_by_env_flag(ci_db, monkeypatch):
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("CI_SIGNAL_DEDUP_ENABLED", "0")

    async def fake_open_ci_db():
        return _NoCloseDB(ci_db)

    monkeypatch.setattr("app.core.ci_fixer._open_ci_db", fake_open_ci_db)

    sig = "klipper::test_bar::h"
    for u in ("u-old-1", "u-old-2"):
        await ci_db.execute(
            """INSERT INTO ci_lesson_learned
               (run_uuid, project, test_name, error_hash, signature, raw_error,
                attempt_num, strategy, context_lessons, fix_diff, outcome, duration_ms)
               VALUES (?, 'klipper', 'test_bar', 'h', ?, 'e', 1,
                       'fix-direct', NULL, NULL, 'failed', 0)""",
            (u, sig),
        )

    mock_claude = AsyncMock(return_value={"answer": "fix", "session_id": None, "error": None})
    mock_tests = AsyncMock(
        return_value={
            "project": "klipper",
            "total": 1,
            "passed": 1,
            "failed": 0,
            "duration_s": 0.1,
            "failures": [],
        }
    )

    with (
        patch("app.core.ci_fixer._call_claude_code", mock_claude),
        patch("app.core.ci_fixer.run_project_tests", mock_tests),
        patch("app.core.ci_fixer.compute_signature", return_value=("h", sig)),
        patch("app.core.ci_fixer.post_lesson_summary_to_memory_api", AsyncMock()),
    ):
        await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError",
        )

    latest = await ci_db.fetch_one("SELECT strategy FROM ci_lesson_learned ORDER BY id DESC LIMIT 1")
    assert latest["strategy"] == "fix-direct"


# ---------------------------------------------------------------------------
# Task 10: build_fix_prompt past-lessons block
# ---------------------------------------------------------------------------


def test_prompt_contains_past_lessons_when_provided():
    lessons = [
        {
            "attempt_num": 1,
            "strategy": "fix-direct",
            "outcome": "failed",
            "fix_diff": "diff 1",
            "raw_error": "err",
            "created_at": "2026-04-17 10:00:00",
        },
        {
            "attempt_num": 2,
            "strategy": "fix-direct",
            "outcome": "failed",
            "fix_diff": "diff 2",
            "raw_error": "err",
            "created_at": "2026-04-18 09:00:00",
        },
    ]
    prompt = build_fix_prompt(
        project="klipper",
        test_file="tests/test_foo.py",
        test_name="test_bar",
        error="AssertionError",
        context_lessons=lessons,
    )
    assert "Onceki denemelerdeki dersler" in prompt
    assert "diff 1" in prompt
    assert "diff 2" in prompt
    # Shape asserts: pin the per-lesson header format so reordering fields
    # or dropping the timestamp would be a visible regression.
    assert "deneme 1 (fix-direct) => failed (2026-04-17 10:00:00)" in prompt
    assert "deneme 2 (fix-direct) => failed (2026-04-18 09:00:00)" in prompt


def test_prompt_truncates_long_fix_diff_to_500_chars():
    """The prompt preview of fix_diff is capped at FIX_DIFF_PROMPT_PREVIEW (500)
    chars so context-enriched prompts stay lean. Storage (FIX_DIFF_CAP=4096) is
    a separate, larger cap -- this test pins the prompt-side invariant only.
    """
    long_diff = "x" * 600
    lessons = [
        {
            "attempt_num": 1,
            "strategy": "fix-direct",
            "outcome": "failed",
            "fix_diff": long_diff,
            "raw_error": "err",
            "created_at": "2026-04-17 10:00:00",
        },
    ]
    prompt = build_fix_prompt(
        project="klipper",
        test_file="tests/test_foo.py",
        test_name="test_bar",
        error="AssertionError",
        context_lessons=lessons,
    )
    assert "x" * 500 in prompt
    assert "x" * 501 not in prompt


def test_prompt_has_no_lessons_section_when_none():
    prompt = build_fix_prompt(
        project="klipper",
        test_file="tests/test_foo.py",
        test_name="test_bar",
        error="AssertionError",
        context_lessons=None,
    )
    assert "Onceki denemelerdeki dersler" not in prompt


def test_prompt_has_no_lessons_section_when_empty_list():
    prompt = build_fix_prompt(
        project="klipper",
        test_file="tests/test_foo.py",
        test_name="test_bar",
        error="AssertionError",
        context_lessons=[],
    )
    assert "Onceki denemelerdeki dersler" not in prompt


# ---------------------------------------------------------------------------
# post_lesson_summary_to_memory_api -- real helper body, httpx.MockTransport
# ---------------------------------------------------------------------------


def _patch_async_client_with_transport(monkeypatch, handler):
    """Route the helper's internal httpx.AsyncClient through MockTransport."""
    real_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("app.core.ci_fixer.httpx.AsyncClient", make_client)


def _patch_settings(monkeypatch, *, base: str, key: str):
    """Override app.core.ci_fixer.get_settings without polluting the real cache."""
    fake = SimpleNamespace(memory_api_base=base, memory_api_key=key)
    monkeypatch.setattr("app.core.ci_fixer.get_settings", lambda: fake)


@pytest.mark.asyncio
async def test_post_lesson_summary_sends_expected_request(monkeypatch, caplog):
    """Happy path: URL, headers, payload shape; no warning on 200."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={})

    _patch_async_client_with_transport(monkeypatch, handler)
    _patch_settings(monkeypatch, base="http://test/api", key="test-key")

    with caplog.at_level(logging.WARNING, logger="app.core.ci_fixer"):
        await post_lesson_summary_to_memory_api(
            lesson_type="lesson_learned",
            name="x",
            description="y",
            content="z",
        )

    assert captured["url"].endswith("/memories")
    # httpx lowercases header names
    assert captured["headers"]["x-memory-key"] == "test-key"
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["json"] == {
        "type": "lesson_learned",
        "name": "x",
        "description": "y",
        "content": "z",
    }
    # No warnings on 200
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base", "key"),
    [
        ("", "some-key"),
        ("http://test/api", ""),
    ],
    ids=["base_missing", "key_missing"],
)
async def test_post_lesson_summary_skips_when_config_missing(
    monkeypatch,
    base,
    key,
):
    """When base or key is empty, no HTTP request must be made."""
    called: list = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        called.append(request)
        return httpx.Response(200, json={})

    _patch_async_client_with_transport(monkeypatch, handler)
    _patch_settings(monkeypatch, base=base, key=key)

    await post_lesson_summary_to_memory_api(
        lesson_type="lesson_learned",
        name="x",
        description="y",
        content="z",
    )

    assert called == []


@pytest.mark.asyncio
async def test_post_lesson_summary_logs_warning_on_500(monkeypatch, caplog):
    """On 5xx, helper must log a warning and not raise into the fix loop."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream exploded")

    _patch_async_client_with_transport(monkeypatch, handler)
    _patch_settings(monkeypatch, base="http://test/api", key="test-key")

    with caplog.at_level(logging.WARNING, logger="app.core.ci_fixer"):
        # Must not raise
        await post_lesson_summary_to_memory_api(
            lesson_type="lesson_learned",
            name="x",
            description="y",
            content="z",
        )

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "expected a warning log for 5xx response"
    joined = " ".join(r.getMessage() for r in warnings)
    assert "500" in joined
    assert "upstream exploded" in joined


@pytest.mark.asyncio
async def test_post_lesson_summary_logs_warning_on_401(monkeypatch, caplog):
    """On 4xx (e.g. bad key), helper logs warning, does not raise."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    _patch_async_client_with_transport(monkeypatch, handler)
    _patch_settings(monkeypatch, base="http://test/api", key="wrong-key")

    with caplog.at_level(logging.WARNING, logger="app.core.ci_fixer"):
        await post_lesson_summary_to_memory_api(
            lesson_type="lesson_learned",
            name="x",
            description="y",
            content="z",
        )

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "expected a warning log for 4xx response"
    joined = " ".join(r.getMessage() for r in warnings)
    assert "401" in joined


@pytest.mark.asyncio
async def test_post_lesson_summary_sanitizes_newlines_in_string_fields(monkeypatch):
    """Defense-in-depth: newlines in name/description/content are flattened
    to spaces before the JSON body goes out, so the strict memory API parser
    never sees a rejectable payload even if a caller interpolates unsanitized
    values (e.g. generative test names, project slugs with stray \\n)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={})

    _patch_async_client_with_transport(monkeypatch, handler)
    _patch_settings(monkeypatch, base="http://test/api", key="test-key")

    await post_lesson_summary_to_memory_api(
        lesson_type="lesson_learned",
        name="n1\nn2",
        description="d1\r\nd2",
        content="a\nb\r\nc",
    )

    # content: \n -> space, then \r -> space; \r\n sequence becomes two spaces
    assert captured["json"]["content"] == "a b  c"
    assert captured["json"]["name"] == "n1 n2"
    assert captured["json"]["description"] == "d1  d2"

    # No raw newlines or carriage returns anywhere in the serialized body
    assert b"\n" not in captured["body"]
    assert b"\r" not in captured["body"]
