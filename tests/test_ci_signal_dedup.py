"""Tests for signal normalization and signature computation."""
import pytest

from app.core.ci_signal_dedup import compute_signature, normalize_error, record_lesson


def test_normalize_strips_iso_timestamp_z():
    raw = "Connection failed at 2026-04-18T01:23:45.123Z on port:5432"
    assert normalize_error(raw) == "Connection failed at <TS> on port:<PORT>"


def test_normalize_strips_iso_timestamp_space():
    raw = "logged at 2026-04-18 01:23:45 UTC"
    assert normalize_error(raw) == "logged at <TS> UTC"


def test_normalize_strips_uuid():
    raw = "job id deadbeef-1234-5678-9abc-def012345678 aborted"
    assert normalize_error(raw) == "job id <UUID> aborted"


def test_normalize_strips_hex_address():
    raw = "segfault at 0xdeadbeef"
    assert normalize_error(raw) == "segfault at <HEX>"


def test_normalize_strips_tmp_path():
    raw = "cannot write /tmp/pytest-abc/test.txt"
    assert normalize_error(raw) == "cannot write <TMPPATH>"


def test_normalize_strips_linux_home_path():
    raw = "open /home/klipperos/foo failed"
    assert normalize_error(raw) == "open <USERPATH> failed"


def test_normalize_strips_windows_user_path():
    raw = r"open C:\Users\sevdi\test.py failed"
    assert normalize_error(raw) == "open <USERPATH> failed"


def test_normalize_strips_bigint():
    raw = "epoch 1745000000000 exceeded"
    assert normalize_error(raw) == "epoch <BIGINT> exceeded"


def test_normalize_idempotent():
    raw = "timestamp 2026-04-18T01:23:45Z and id abc12345-6789-4abc-8def-123456789012"
    once = normalize_error(raw)
    twice = normalize_error(once)
    assert once == twice


def test_normalize_uppercase_uuid():
    # GUID from a .NET stack trace
    raw = "Request 3F2504E0-4F89-11D3-9A0C-0305E82C3301 failed"
    assert normalize_error(raw) == "Request <UUID> failed"


def test_normalize_uppercase_hex_address():
    raw = "Segfault at 0xDEADBEEF in libfoo"
    assert normalize_error(raw) == "Segfault at <HEX> in libfoo"


def test_normalize_non_c_windows_drive():
    raw = r"File not found: D:\Users\alice\proj\main.py"
    assert normalize_error(raw) == "File not found: <USERPATH>"


def test_normalize_lowercase_windows_drive():
    raw = r"File not found: c:\Users\bob\proj\main.py"
    assert normalize_error(raw) == "File not found: <USERPATH>"


def test_normalize_path_terminated_by_bracket():
    raw = "at /tmp/pytest-abc/test.py]:42"
    # Path stops at ], then :42 is not a port (under 4 digits so ignored),
    # so expect the trailing ]:42 preserved verbatim.
    assert normalize_error(raw) == "at <TMPPATH>]:42"


def test_normalize_path_terminated_by_comma():
    raw = "files: /tmp/a.log, /tmp/b.log"
    assert normalize_error(raw) == "files: <TMPPATH>, <TMPPATH>"


def test_normalize_timestamp_does_not_eat_bigint():
    raw = "2026-04-18T01:23:45.123Z1234567890 event"
    assert normalize_error(raw) == "<TS><BIGINT> event"


def test_normalize_full_idempotence():
    raw = (
        "2026-04-18T01:23:45.123Z "
        "uuid=3f2504e0-4f89-11d3-9a0c-0305e82c3301 "
        "hex=0xDEADBEEF "
        "tmp=/tmp/foo.log "
        "home=/home/user/proj "
        "port=:5432 "
        "big=1234567890 "
        "date=2026-04-18 12:00:00"
    )
    once = normalize_error(raw)
    twice = normalize_error(once)
    assert once == twice, f"not idempotent: {once!r} vs {twice!r}"


def test_signature_is_project_testname_hash_triple():
    h, sig = compute_signature("bilge-arena", "test_login", "AssertionError: 5 != 3")
    assert len(h) == 12
    assert sig == f"bilge-arena::test_login::{h}"


def test_signature_stable_across_timestamps():
    _, sig1 = compute_signature("p", "t", "failed at 2026-04-18T01:23:45Z")
    _, sig2 = compute_signature("p", "t", "failed at 2026-04-18T09:59:59Z")
    assert sig1 == sig2


def test_signature_differs_for_different_errors():
    _, sig1 = compute_signature("p", "t", "AssertionError: 5 != 3")
    _, sig2 = compute_signature("p", "t", "KeyError: missing")
    assert sig1 != sig2


def test_signature_hash_is_hex_12_chars():
    h, _ = compute_signature("p", "t", "anything")
    assert len(h) == 12
    assert all(c in "0123456789abcdef" for c in h)


@pytest.mark.asyncio
async def test_record_lesson_inserts_and_returns_id(ci_db):
    row_id = await record_lesson(
        ci_db,
        run_uuid="u1",
        project="p",
        test_name="t",
        error_hash="abc123abc123",
        signature="p::t::abc123abc123",
        raw_error="AssertionError",
        attempt_num=1,
        strategy="fix-direct",
        context_lessons=None,
        fix_diff="diff --git ...",
        outcome="passed",
        duration_ms=420,
    )
    assert row_id > 0
    row = await ci_db.fetch_one(
        "SELECT project, outcome, strategy FROM ci_lesson_learned WHERE id = ?",
        (row_id,),
    )
    assert row == {"project": "p", "outcome": "passed", "strategy": "fix-direct"}


@pytest.mark.asyncio
async def test_record_lesson_truncates_fix_diff(ci_db):
    big = "x" * 10000
    row_id = await record_lesson(
        ci_db,
        run_uuid="u2",
        project="p",
        test_name="t",
        error_hash="h",
        signature="p::t::h",
        raw_error="e",
        attempt_num=1,
        strategy="fix-direct",
        context_lessons=None,
        fix_diff=big,
        outcome="failed",
        duration_ms=0,
    )
    stored = await ci_db.fetch_one(
        "SELECT fix_diff FROM ci_lesson_learned WHERE id = ?", (row_id,)
    )
    assert len(stored["fix_diff"]) <= 4096


@pytest.mark.asyncio
async def test_record_lesson_accepts_none_optional_fields(ci_db):
    row_id = await record_lesson(
        ci_db,
        run_uuid="u3",
        project="p",
        test_name="t",
        error_hash="h",
        signature="p::t::h",
        raw_error=None,
        attempt_num=1,
        strategy="fix-direct",
        context_lessons=None,
        fix_diff=None,
        outcome="error",
        duration_ms=None,
    )
    assert row_id > 0
