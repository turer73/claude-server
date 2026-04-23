"""Server configuration with environment variable and YAML support."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings


def load_yaml_config(path: str) -> dict:
    """Load configuration from YAML file. Returns empty dict on failure."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, PermissionError, yaml.YAMLError):
        return {}


class Settings(BaseSettings):
    # Server
    server_host: str = "0.0.0.0"
    server_port: int = 8420
    server_workers: int = 2
    server_debug: bool = False

    # Auth
    jwt_secret: str = "change-me-via-env"
    jwt_ttl_hours: int = 1
    api_key_header: str = "X-API-Key"

    # Network
    vps_host: str = ""
    lan_ip: str = "127.0.0.1"
    tailscale_ip: str = "127.0.0.1"
    memory_api_key: str = ""
    memory_api_base: str = "http://100.113.153.62:8420/api/v1/memory"

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
        "make", "cmake", "gcc", "g++", "claude", "bash",
        "curl", "wget", "dig", "nslookup", "ping",
        "docker", "docker-compose",
        "sudo", "dmesg", "lsmod", "modprobe", "insmod", "rmmod",
        "sysctl", "iptables", "ip", "ss", "lsblk", "mount", "umount",
        "apt", "dpkg", "chmod", "chown", "ln", "cp", "mv", "rm", "mkdir",
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
    yaml_paths = [
        os.environ.get("CONFIG_FILE", ""),
        "config/server.yml",
        "/etc/linux-ai-server/server.yml",
    ]
    yaml_overrides = {}
    for path in yaml_paths:
        if path:
            yaml_overrides = load_yaml_config(path)
            if yaml_overrides:
                break

    # Only pass keys that match Settings fields (flat keys only)
    valid_fields = Settings.model_fields
    filtered = {k: v for k, v in yaml_overrides.items() if k in valid_fields}

    return Settings(**filtered)
