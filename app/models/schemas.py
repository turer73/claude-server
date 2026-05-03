"""Pydantic v2 request/response schemas for all API endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# --- Common ---


class ErrorResponse(BaseModel):
    error: str
    message: str
    detail: str | None = None
    request_id: str | None = None


# --- Kernel ---


class KernelStatusResponse(BaseModel):
    state: str
    governor: str
    cpu_count: int
    services: int
    version: str | None = None


class GovernorRequest(BaseModel):
    mode: Literal["performance", "powersave", "ondemand", "conservative", "ai_adaptive"]
    cpu_mask: int | None = None


class GovernorResponse(BaseModel):
    governor: str
    cpu_mask: int | None = None


class CpuMetricsResponse(BaseModel):
    cpu_id: int
    usage_percent: float
    frequency_mhz: int
    temperature_c: float
    io_read_bytes: int
    io_write_bytes: int


class FrequencyRequest(BaseModel):
    cpu_id: int = 0
    min_freq_mhz: int
    max_freq_mhz: int


# --- System ---


class SystemInfoResponse(BaseModel):
    hostname: str
    os: str
    kernel: str
    uptime_seconds: float
    cpu_count: int
    cpu_percent: float
    memory_total_mb: int
    memory_used_mb: int
    memory_percent: float
    disk_total_gb: float
    disk_used_gb: float
    disk_percent: float
    load_avg: list[float]


class ProcessInfo(BaseModel):
    pid: int
    name: str
    cpu_percent: float
    memory_mb: float
    status: str
    user: str


class ProcessListResponse(BaseModel):
    processes: list[ProcessInfo]
    total: int


class ServiceAction(BaseModel):
    action: Literal["start", "stop", "restart", "enable", "disable"]


# --- Files ---


class FileReadRequest(BaseModel):
    path: str
    offset: int = 0
    limit: int = 1000


class FileReadResponse(BaseModel):
    path: str
    content: str
    size: int
    lines: int


class FileWriteRequest(BaseModel):
    path: str
    content: str
    mode: Literal["write", "append"] = "write"


class FileEditRequest(BaseModel):
    path: str
    old_string: str
    new_string: str


class FileInfoResponse(BaseModel):
    path: str
    size: int
    is_dir: bool
    permissions: str
    modified: str
    owner: str


class FileSearchRequest(BaseModel):
    path: str = "."
    pattern: str
    content_search: bool = False
    max_results: int = 50


class FileListResponse(BaseModel):
    path: str
    entries: list[FileInfoResponse]


# --- Dev ---


class GitStatusResponse(BaseModel):
    branch: str
    clean: bool
    staged: list[str]
    modified: list[str]
    untracked: list[str]


class GitCommitRequest(BaseModel):
    message: str
    files: list[str] | None = None


class GitLogEntry(BaseModel):
    hash: str
    author: str
    date: str
    message: str


class PackageInstallRequest(BaseModel):
    manager: Literal["pip", "npm", "apt"]
    packages: list[str]


# --- Network ---


class HttpProxyRequest(BaseModel):
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"] = "GET"
    url: str
    headers: dict[str, str] | None = None
    body: str | None = None
    timeout: int = 30


class HttpProxyResponse(BaseModel):
    status_code: int
    headers: dict[str, str]
    body: str
    elapsed_ms: float


# --- Shell ---


class ShellExecRequest(BaseModel):
    command: str
    timeout: int = Field(default=30, ge=1, le=300)
    cwd: str | None = None


class ShellExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    elapsed_ms: float


# --- SSH ---


class SshConnectRequest(BaseModel):
    host: str
    username: str
    port: int = 22
    key_path: str | None = None
    password: str | None = None


class SshExecRequest(BaseModel):
    session_id: str
    command: str
    timeout: int = 30


class SshExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


class SshTransferRequest(BaseModel):
    session_id: str
    local_path: str
    remote_path: str
    direction: Literal["upload", "download"]


# --- Agent ---


class AgentDefinition(BaseModel):
    name: str
    description: str
    trigger: Literal["manual", "cron", "event"] = "manual"
    schedule: str | None = None
    tools: list[str] = []
    system_prompt: str | None = None
    steps: list[dict] | None = None


class AgentRunRequest(BaseModel):
    agent_name: str
    params: dict | None = None


class AgentStatusResponse(BaseModel):
    name: str
    status: Literal["idle", "running", "error"]
    last_run: str | None = None
    last_result: str | None = None


# --- Monitor ---


class MetricsSnapshot(BaseModel):
    timestamp: str
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    temperature: float | None
    load_avg: list[float]
    network_sent_mb: float
    network_recv_mb: float


class AlertConfig(BaseModel):
    cpu_percent: int = 85
    memory_percent: int = 85
    disk_percent: int = 90
    temperature_c: int = 80


class AlertEntry(BaseModel):
    id: int
    timestamp: str
    severity: str
    source: str
    message: str
    resolved: bool


# --- Logs ---


class LogSearchRequest(BaseModel):
    pattern: str
    source: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    limit: int = 100


class LogEntry(BaseModel):
    timestamp: str
    level: str
    source: str
    message: str


# --- AI ---


class AIChatRequest(BaseModel):
    message: str
    model: str = "linux-ai-agent"
    context: list[dict[str, str]] | None = None


class AIChatResponse(BaseModel):
    response: str
    model: str
    elapsed_ms: float


# --- CI/CD ---

VALID_CI_PROJECTS = [
    "panola",
    "kuafor-panel",
    "kuafor-worker",
    "petvet",
    "renderhane",
    "bilge-arena",
    "koken-akademi",
    "klipper",
    "panola-rag",
]


class CITestRequest(BaseModel):
    project: str
    test_type: str = "all"

    @field_validator("project")
    @classmethod
    def project_must_be_known(cls, v: str) -> str:
        if v not in VALID_CI_PROJECTS:
            raise ValueError(f"Unknown project {v!r}. Valid: {VALID_CI_PROJECTS}")
        return v


class CIFailure(BaseModel):
    test_file: str
    test_name: str
    error: str
    source_file: str | None = None
    stack_trace: str | None = None


class CITestResponse(BaseModel):
    project: str
    total: int
    passed: int
    failed: int
    duration_s: float
    failures: list[CIFailure] = []


class CIFixRequest(BaseModel):
    project: str
    failure: CIFailure
    attempt: int = 1
    prev_errors: list[str] = []


class CIFixResponse(BaseModel):
    fixed: bool
    attempt: int
    diff: str | None = None
    retry_result: str | None = None
    error: str | None = None


class CIProjectResult(BaseModel):
    project: str
    total: int
    passed: int
    failed: int
    fix_attempted: bool
    fix_result: str | None = None


class CIStatusResponse(BaseModel):
    last_run: str
    total_tests: int
    passed: int
    failed: int
    projects: list[CIProjectResult]
