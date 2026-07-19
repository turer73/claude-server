"""Lifecycle tests for the single-worker continuous-agent cohort."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import main


class FakeAgent:
    def __init__(self, name: str, calls: list[str], *, start_error: bool = False, stop_error: bool = False) -> None:
        self.name = name
        self.calls = calls
        self.start_error = start_error
        self.stop_error = stop_error

    def start(self) -> None:
        self.calls.append(f"start:{self.name}")
        if self.start_error:
            raise RuntimeError(f"start failed: {self.name}")

    async def stop(self) -> None:
        self.calls.append(f"stop:{self.name}")
        if self.stop_error:
            raise RuntimeError(f"stop failed: {self.name}")


class FakeLeaderLock:
    def __init__(self, *roles: str) -> None:
        self.roles = list(roles)
        self.role = "standby"
        self.fd = None
        self.error = None
        self.path = "cohort.lock"
        self.release_calls = 0

    def try_acquire(self) -> str:
        if self.roles:
            self.role = self.roles.pop(0)
        self.fd = 42 if self.role == "leader" else None
        self.error = "lock failed" if self.role == "lock_error" else None
        return self.role

    def release(self) -> None:
        self.release_calls += 1
        self.role = "standby"
        self.fd = None
        self.error = None


class FakeBus:
    def __init__(self) -> None:
        self.unsubscribed: list[tuple[str, object]] = []
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True

    def unsubscribe(self, event_type: str, handler: object) -> None:
        self.unsubscribed.append((event_type, handler))


def _app(calls: list[str], **overrides: FakeAgent) -> SimpleNamespace:
    agents = {
        "memory_consolidator": FakeAgent("memory", calls),
        "learning_loop": FakeAgent("learning", calls),
        "critic_agent": FakeAgent("critic", calls),
        "code_review_agent": FakeAgent("code-review", calls),
        "consciousness_stream": FakeAgent("consciousness", calls),
    }
    agents.update(overrides)
    return SimpleNamespace(state=SimpleNamespace(**agents, continuous_agents_started=False))


async def test_cohort_starts_consumers_before_publisher_once() -> None:
    calls: list[str] = []
    app = _app(calls)

    assert await main._start_continuous_agent_cohort(app) is True
    assert await main._start_continuous_agent_cohort(app) is True

    assert calls == [
        "start:memory",
        "start:learning",
        "start:critic",
        "start:code-review",
        "start:consciousness",
    ]
    assert app.state.continuous_agents_role == "leader"


async def test_partial_start_rolls_back_and_stays_fail_closed() -> None:
    calls: list[str] = []
    app = _app(calls, critic_agent=FakeAgent("critic", calls, start_error=True))

    assert await main._start_continuous_agent_cohort(app) is False

    assert calls == [
        "start:memory",
        "start:learning",
        "start:critic",
        "stop:critic",
        "stop:learning",
        "stop:memory",
    ]
    assert app.state.continuous_agents_started is False
    assert app.state.continuous_agents_role == "lock_error"


async def test_clean_start_failure_retains_lock_until_process_exit() -> None:
    calls: list[str] = []
    app = _app(calls, critic_agent=FakeAgent("critic", calls, start_error=True))
    lock = FakeLeaderLock("leader")

    with pytest.raises(RuntimeError, match="retaining leadership until worker exit"):
        await main._initialize_continuous_agent_cohort(app, lock)

    assert lock.release_calls == 0
    assert lock.fd == 42
    assert app.state.continuous_agents_role == "lock_error"
    assert app.state.continuous_agents_rollback_clean is True


async def test_dirty_start_rollback_retains_lock_and_fails_startup() -> None:
    calls: list[str] = []
    app = _app(
        calls,
        learning_loop=FakeAgent("learning", calls, stop_error=True),
        critic_agent=FakeAgent("critic", calls, start_error=True),
    )
    lock = FakeLeaderLock("leader")

    with pytest.raises(RuntimeError, match="retaining leadership until worker exit"):
        await main._initialize_continuous_agent_cohort(app, lock)

    assert lock.release_calls == 0
    assert lock.fd == 42
    assert app.state.continuous_agents_role == "lock_error"
    assert app.state.continuous_agents_rollback_clean is False


async def test_stop_continues_after_member_failure() -> None:
    calls: list[str] = []
    app = _app(calls, code_review_agent=FakeAgent("code-review", calls, stop_error=True))
    app.state.continuous_agents_started = True

    await main._stop_continuous_agent_cohort(app)

    assert calls == [
        "stop:consciousness",
        "stop:code-review",
        "stop:critic",
        "stop:learning",
        "stop:memory",
    ]
    assert app.state.continuous_agents_started is False


async def test_standby_retries_and_takes_over(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    app = _app(calls)
    lock = FakeLeaderLock("standby", "leader")
    monkeypatch.setattr(main, "_CONTINUOUS_AGENT_RETRY_SECONDS", 0)

    await main._initialize_continuous_agent_cohort(app, lock)
    retry_task = app.state.continuous_agents_retry_task
    await asyncio.wait_for(retry_task, timeout=1)

    assert app.state.continuous_agents_role == "leader"
    assert app.state.continuous_agents_lock_fd == 42
    assert calls[-1] == "start:consciousness"


async def test_dirty_runtime_takeover_fail_stops_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    app = _app(
        calls,
        learning_loop=FakeAgent("learning", calls, stop_error=True),
        critic_agent=FakeAgent("critic", calls, start_error=True),
    )
    lock = FakeLeaderLock("standby", "leader")
    terminated = asyncio.Event()
    monkeypatch.setattr(main, "_CONTINUOUS_AGENT_RETRY_SECONDS", 0)
    monkeypatch.setattr(main, "_terminate_worker_for_cohort_failure", terminated.set)

    await main._initialize_continuous_agent_cohort(app, lock)
    retry_task = app.state.continuous_agents_retry_task
    await asyncio.wait_for(retry_task, timeout=1)

    assert terminated.is_set()
    assert lock.release_calls == 0
    assert lock.fd == 42
    assert app.state.continuous_agents_role == "lock_error"
    assert app.state.continuous_agents_rollback_clean is False


async def test_clean_runtime_takeover_also_fail_stops_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    app = _app(calls, critic_agent=FakeAgent("critic", calls, start_error=True))
    lock = FakeLeaderLock("standby", "leader")
    terminated = asyncio.Event()
    monkeypatch.setattr(main, "_CONTINUOUS_AGENT_RETRY_SECONDS", 0)
    monkeypatch.setattr(main, "_terminate_worker_for_cohort_failure", terminated.set)

    await main._initialize_continuous_agent_cohort(app, lock)
    retry_task = app.state.continuous_agents_retry_task
    await asyncio.wait_for(retry_task, timeout=1)

    assert terminated.is_set()
    assert lock.release_calls == 0
    assert lock.fd == 42
    assert app.state.continuous_agents_role == "lock_error"
    assert app.state.continuous_agents_rollback_clean is True


async def test_shutdown_cancels_retry_stops_all_and_unsubscribes_bridge() -> None:
    calls: list[str] = []
    app = _app(calls)
    lock = FakeLeaderLock("standby")
    bus = FakeBus()

    await main._initialize_continuous_agent_cohort(app, lock)
    retry_task = app.state.continuous_agents_retry_task
    bridge = object()
    await main._shutdown_continuous_agent_cohort(app, bus, bridge)

    assert retry_task.cancelled()
    assert app.state.continuous_agents_retry_task is None
    assert bus.stopped is True
    assert bus.unsubscribed == [("*", bridge)]
    assert calls == [
        "stop:consciousness",
        "stop:code-review",
        "stop:critic",
        "stop:learning",
        "stop:memory",
    ]
