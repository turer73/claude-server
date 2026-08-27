from __future__ import annotations

import sqlite3
import threading

import pytest

from app.core.autonomous_comms.audit import append_audit
from app.core.autonomous_comms.budget import BudgetLimits, finalize_budget, reserve_budget
from app.core.autonomous_comms.claims import ThreadClaim, acquire_claim, release_claim, renew_claim
from app.core.autonomous_comms.idempotency import ProcessingState, begin_processing, finish_processing
from app.core.autonomous_comms.schema import ensure_schema


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "autonomous-comms.db")
    conn = _connect(path)
    ensure_schema(conn)
    conn.close()
    return path


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def test_schema_is_idempotent_and_audit_is_append_only(db_path: str) -> None:
    conn = _connect(db_path)
    ensure_schema(conn)
    ensure_schema(conn)
    audit_id = append_audit(
        conn,
        decision="held",
        reason="shadow",
        correlation_id="corr-1",
        idempotency_key="7:9",
        thread_id=7,
        source_note_id=9,
        metadata={"hop_count": 1, "route": {"verdict": "held"}},
    )
    assert audit_id > 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE autonomous_comms_decision_audit SET reason = 'changed'")
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM autonomous_comms_decision_audit")
    conn.rollback()
    conn.close()


@pytest.mark.parametrize("key", ["prompt", "api_key", "nested"])
def test_audit_rejects_sensitive_metadata_recursively(db_path: str, key: str) -> None:
    metadata = {key: "raw"} if key != "nested" else {"nested": {"credential": "raw"}}
    conn = _connect(db_path)
    with pytest.raises(ValueError, match="sensitive"):
        append_audit(
            conn,
            decision="failed",
            reason="validation",
            correlation_id="corr-2",
            idempotency_key="1:2",
            metadata=metadata,
        )
    conn.close()


def test_thread_claim_token_and_stale_takeover(db_path: str) -> None:
    conn = _connect(db_path)
    first = acquire_claim(conn, thread_id=11, owner_id="worker-a", lease_seconds=30, now=100)
    assert first is not None
    assert acquire_claim(conn, thread_id=11, owner_id="worker-b", lease_seconds=30, now=110) is None
    forged = ThreadClaim(11, "worker-a", "x" * 43, 130)
    assert release_claim(conn, forged) is False
    assert renew_claim(conn, first, lease_seconds=30, now=110) is not None
    takeover = acquire_claim(conn, thread_id=11, owner_id="worker-b", lease_seconds=30, now=141)
    assert takeover is not None
    assert takeover.lease_token != first.lease_token
    assert release_claim(conn, first) is False
    assert release_claim(conn, takeover) is True
    conn.close()


def test_concurrent_claim_has_one_winner(db_path: str) -> None:
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def worker(owner: str) -> None:
        conn = _connect(db_path)
        barrier.wait()
        results.append(acquire_claim(conn, thread_id=12, owner_id=owner, lease_seconds=60, now=100) is not None)
        conn.close()

    threads = [threading.Thread(target=worker, args=(owner,)) for owner in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]


def test_idempotency_sent_is_immutable_and_stale_processing_reclaims(db_path: str) -> None:
    conn = _connect(db_path)
    first = begin_processing(conn, thread_id=20, source_note_id=30, stale_after_seconds=10, now=100)
    assert first is not None
    assert begin_processing(conn, thread_id=20, source_note_id=30, stale_after_seconds=10, now=105) is None
    reclaimed = begin_processing(conn, thread_id=20, source_note_id=30, stale_after_seconds=10, now=111)
    assert reclaimed is not None
    assert reclaimed.owner_token != first.owner_token
    assert finish_processing(conn, first, state=ProcessingState.FAILED) is False
    assert finish_processing(conn, reclaimed, state=ProcessingState.SENT, outgoing_note_id=99) is True
    assert begin_processing(conn, thread_id=20, source_note_id=30, stale_after_seconds=10, now=1000) is None
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            UPDATE autonomous_comms_processing SET state = 'failed'
            WHERE thread_id = 20 AND source_note_id = 30
            """
        )
    conn.rollback()
    conn.close()


def test_concurrent_idempotency_has_one_winner(db_path: str) -> None:
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def worker() -> None:
        conn = _connect(db_path)
        barrier.wait()
        results.append(
            begin_processing(conn, thread_id=21, source_note_id=31, stale_after_seconds=60, now=100) is not None
        )
        conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]


def test_budget_is_atomic_and_refunds_failures(db_path: str) -> None:
    limits = BudgetLimits(daily_replies=1, daily_tokens=100, daily_new_threads=1, concurrent_in_flight=1)
    conn = _connect(db_path)
    reservation = reserve_budget(
        conn,
        day_utc="2026-08-27",
        estimated_tokens=80,
        is_new_thread=True,
        limits=limits,
        now=100,
    )
    assert reservation is not None
    assert (
        reserve_budget(
            conn,
            day_utc="2026-08-27",
            estimated_tokens=10,
            is_new_thread=False,
            limits=limits,
            now=101,
        )
        is None
    )
    assert finalize_budget(conn, reservation, success=False, now=102) is True
    retry = reserve_budget(
        conn,
        day_utc="2026-08-27",
        estimated_tokens=70,
        is_new_thread=True,
        limits=limits,
        now=103,
    )
    assert retry is not None
    assert finalize_budget(conn, retry, success=True, actual_tokens=40, now=104) is True
    assert finalize_budget(conn, retry, success=False, now=105) is False
    counters = conn.execute(
        """
        SELECT replies_reserved, tokens_reserved, new_threads_reserved, in_flight
        FROM autonomous_comms_daily_budget WHERE day_utc = '2026-08-27'
        """
    ).fetchone()
    assert tuple(counters) == (1, 40, 1, 0)
    conn.close()


def test_budget_utc_day_rollover_is_independent(db_path: str) -> None:
    limits = BudgetLimits(daily_replies=1, daily_tokens=10, daily_new_threads=0, concurrent_in_flight=1)
    conn = _connect(db_path)
    first = reserve_budget(
        conn,
        day_utc="2026-08-27",
        estimated_tokens=10,
        is_new_thread=False,
        limits=limits,
        now=100,
    )
    assert first is not None
    assert finalize_budget(conn, first, success=True, actual_tokens=10, now=101)
    second = reserve_budget(
        conn,
        day_utc="2026-08-28",
        estimated_tokens=10,
        is_new_thread=False,
        limits=limits,
        now=102,
    )
    assert second is not None
    conn.close()


def test_stale_budget_reservation_is_recovered(db_path: str) -> None:
    limits = BudgetLimits(daily_replies=1, daily_tokens=100, daily_new_threads=1, concurrent_in_flight=1)
    conn = _connect(db_path)
    abandoned = reserve_budget(
        conn,
        day_utc="2026-08-27",
        estimated_tokens=50,
        is_new_thread=False,
        limits=limits,
        stale_after_seconds=10,
        now=100,
    )
    assert abandoned is not None
    recovered = reserve_budget(
        conn,
        day_utc="2026-08-27",
        estimated_tokens=40,
        is_new_thread=False,
        limits=limits,
        stale_after_seconds=10,
        now=111,
    )
    assert recovered is not None
    state = conn.execute(
        "SELECT state FROM autonomous_comms_budget_reservations WHERE reservation_id = ?",
        (abandoned.reservation_id,),
    ).fetchone()[0]
    assert state == "refunded"
    conn.close()


def test_concurrent_budget_has_one_winner(db_path: str) -> None:
    limits = BudgetLimits(daily_replies=1, daily_tokens=100, daily_new_threads=1, concurrent_in_flight=1)
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def worker() -> None:
        conn = _connect(db_path)
        barrier.wait()
        result = reserve_budget(
            conn,
            day_utc="2026-08-27",
            estimated_tokens=20,
            is_new_thread=False,
            limits=limits,
            now=100,
        )
        results.append(result is not None)
        conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]
