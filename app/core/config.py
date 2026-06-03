"""Server configuration with environment variable and YAML support."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated

import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode

DEFAULT_ENV_FILE = "/opt/linux-ai-server/.env"


def read_env_var(name: str, env_file: str = DEFAULT_ENV_FILE) -> str:
    """Read a single var from process env with KEY=VALUE file fallback.

    The systemd unit currently doesn't pass MEMORY_API_KEY through
    Environment= or EnvironmentFile=, so module-level callers in
    api/memory.py and api/csp.py used to inline this dotenv parsing
    twice. Centralised here so future endpoints don't repeat it.
    """
    val = os.environ.get(name, "")
    if val:
        return val
    if not os.path.exists(env_file):
        return ""
    try:
        with open(env_file) as f:
            for line in f:
                if line.startswith(f"{name}="):
                    return line.strip().split("=", 1)[1]
    except OSError:
        pass
    return ""


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
    internal_api_key: str = ""

    # Network
    vps_host: str = ""
    lan_ip: str = "127.0.0.1"
    tailscale_ip: str = "127.0.0.1"
    memory_api_key: str = ""
    memory_api_base: str = "http://100.84.251.49:8420/api/v1/memory"

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
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "grep",
        "find",
        "ps",
        "top",
        "df",
        "free",
        "uptime",
        "whoami",
        "id",
        "systemctl",
        "journalctl",
        "git",
        "pip",
        "npm",
        "python3",
        "node",
        "make",
        "cmake",
        "gcc",
        "g++",
        "claude",
        "bash",
        "curl",
        "wget",
        "dig",
        "nslookup",
        "ping",
        "docker",
        "docker-compose",
        "sudo",
        "dmesg",
        "lsmod",
        "modprobe",
        "insmod",
        "rmmod",
        "sysctl",
        "iptables",
        "ip",
        "ss",
        "lsblk",
        "mount",
        "umount",
        "apt",
        "dpkg",
        "chmod",
        "chown",
        "ln",
        "cp",
        "mv",
        "rm",
        "mkdir",
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

    # DevOps agent watchlists (CSV in env, e.g. MONITOR_CRITICAL_CONTAINERS=dozzle,uptime-kuma)
    # NoDecode disables pydantic-settings' default JSON parse so the validator below sees raw CSV.
    monitor_critical_services: Annotated[list[str], NoDecode] = ["linux-ai-server"]
    monitor_critical_containers: Annotated[list[str], NoDecode] = ["dozzle", "uptime-kuma"]
    monitor_vps_containers: Annotated[list[str], NoDecode] = [
        # 2026-05-14: n8n + grafana + prometheus + dokploy-traefik VPS'ten
        # klipper'a tasindi (infra/{n8n,monitoring}/). Bu container'lar VPS'te
        # artik yok, alert false positive uretiyordu — listeden cikarildi.
        "panola-postgres",
        "panola-caddy",
        "panola-postgrest",
        "panola-auth",
        "plausible-plausible-1",
    ]

    @field_validator(
        "monitor_critical_services",
        "monitor_critical_containers",
        "monitor_vps_containers",
        mode="before",
    )
    @classmethod
    def _split_csv_list(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    # WebOps tokens
    vercel_token: str = ""
    cloudflare_token: str = ""
    supabase_url: str = ""
    supabase_token: str = ""
    github_token: str = ""
    coolify_token: str = ""
    coolify_url: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

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
    # GUVENLIK: jwt_secret YAML'dan (cogu kez world-readable, ornek /etc/.../
    # server.yml 0644) ASLA gelmemeli — env-only. Prod'da server.yml'e dusen
    # "change-me-via-env" placeholder env'i eziyor ve public-default ile JWT
    # imzalamaya yol aciyordu (rotate edildi). Burada YAML'dan dislanir; degeri
    # env saglar, yoksa create_app guard'i placeholder/bos'u reddeder.
    # NOT (#5 config-drift takip): telegram/supabase/coolify token'lari da halen
    # server.yml'den geliyor; once env'e tasinmadan dislanmamali (kirilir).
    _yaml_excluded = {"jwt_secret"}
    filtered = {k: v for k, v in yaml_overrides.items() if k in valid_fields and k not in _yaml_excluded}

    return Settings(**filtered)
