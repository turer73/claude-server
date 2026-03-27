import pytest
from app.core.config import Settings


def test_default_settings():
    s = Settings()
    assert s.server_host == "0.0.0.0"
    assert s.server_port == 8420
    assert s.server_workers == 2
    assert s.jwt_ttl_hours == 1
    assert s.rate_limit_read == 100
    assert s.rate_limit_write == 10
    assert s.rate_limit_exec == 5


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-key-123")
    monkeypatch.setenv("SERVER_PORT", "9999")
    s = Settings()
    assert s.jwt_secret == "test-secret-key-123"
    assert s.server_port == 9999


def test_shell_whitelist_default():
    s = Settings()
    assert "ls" in s.shell_whitelist
    assert "git" in s.shell_whitelist
    assert "rm" not in s.shell_whitelist


def test_allowed_paths_default():
    s = Settings()
    assert "/home/" in s.allowed_paths


def test_monitoring_defaults():
    s = Settings()
    assert s.monitor_poll_interval_sec == 5
    assert s.alert_cpu_percent == 85
    assert s.alert_memory_percent == 85


def test_webops_tokens_empty_by_default():
    s = Settings()
    assert s.vercel_token == ""
    assert s.cloudflare_token == ""
    assert s.github_token == ""


def test_db_path_default():
    s = Settings()
    assert "server.db" in s.db_path
