"""Server configuration with environment variable support."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server
    server_host: str = "0.0.0.0"
    server_port: int = 8420
    server_workers: int = 2
    server_debug: bool = False

    # Auth
    jwt_secret: str = "change-me-in-production"
    jwt_ttl_hours: int = 1
    api_key_header: str = "X-API-Key"

    # Database
    db_path: str = "/var/lib/linux-ai-server/server.db"
    metrics_retention_days: int = 30

    # Rate Limiting
    rate_limit_read: int = 100
    rate_limit_write: int = 10
    rate_limit_exec: int = 5

    # Security
    allowed_paths: list[str] = [
        "/var/AI-stump/",
        "/proc/",
        "/sys/",
        "/home/",
        "/tmp/linux-ai-server/",
    ]
    shell_whitelist: list[str] = [
        "ls", "cat", "head", "tail", "wc", "grep", "find",
        "ps", "top", "df", "free", "uptime", "whoami", "id",
        "systemctl", "journalctl",
        "git", "pip", "npm", "python3", "node",
        "make", "cmake", "gcc", "g++",
        "curl", "wget", "dig", "nslookup", "ping",
        "docker", "docker-compose",
    ]
    max_file_size_mb: int = 10
    max_terminal_sessions: int = 5
    terminal_idle_timeout_min: int = 30

    # Monitoring
    monitor_poll_interval_sec: int = 5
    metrics_history_size: int = 1000
    alert_cpu_percent: int = 85
    alert_memory_percent: int = 85
    alert_disk_percent: int = 90
    alert_temperature_c: int = 80

    # WebOps tokens
    vercel_token: str = ""
    cloudflare_token: str = ""
    supabase_url: str = ""
    supabase_token: str = ""
    github_token: str = ""
    coolify_token: str = ""
    coolify_url: str = ""

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"
    log_file: str = "/var/log/linux-ai-server/server.log"

    model_config = {"env_prefix": "", "env_nested_delimiter": "__", "case_sensitive": False}


@lru_cache
def get_settings() -> Settings:
    return Settings()
