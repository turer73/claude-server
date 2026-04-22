"""Tests for signal normalization and signature computation."""
from app.core.ci_signal_dedup import normalize_error


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
