import pytest
from pydantic import ValidationError
from app.models.schemas import (
    KernelStatusResponse,
    CpuMetricsResponse,
    GovernorRequest,
    SystemInfoResponse,
    FileReadRequest,
    FileWriteRequest,
    FileEditRequest,
    ShellExecRequest,
    HttpProxyRequest,
    HttpProxyResponse,
    ErrorResponse,
    MetricsSnapshot,
    AlertConfig,
    AlertEntry,
    LogSearchRequest,
    GitStatusResponse,
    GitCommitRequest,
    PackageInstallRequest,
    ProcessInfo,
    ServiceAction,
    AIChatRequest,
    SshConnectRequest,
    SshExecRequest,
    AgentDefinition,
)


def test_governor_request_valid():
    req = GovernorRequest(mode="performance")
    assert req.mode == "performance"


def test_governor_request_invalid():
    with pytest.raises(ValidationError):
        GovernorRequest(mode="turbo")


def test_file_read_defaults():
    req = FileReadRequest(path="/home/user/file.txt")
    assert req.path == "/home/user/file.txt"
    assert req.offset == 0
    assert req.limit == 1000


def test_shell_exec_defaults():
    req = ShellExecRequest(command="ls -la /home")
    assert req.command == "ls -la /home"
    assert req.timeout == 30


def test_shell_exec_timeout_bounds():
    with pytest.raises(ValidationError):
        ShellExecRequest(command="ls", timeout=999)  # max 300


def test_http_proxy_request():
    req = HttpProxyRequest(method="GET", url="https://example.com")
    assert req.method == "GET"


def test_http_proxy_invalid_method():
    with pytest.raises(ValidationError):
        HttpProxyRequest(method="INVALID", url="https://example.com")


def test_error_response():
    resp = ErrorResponse(error="NotFound", message="File not found")
    assert resp.error == "NotFound"


def test_metrics_snapshot():
    snap = MetricsSnapshot(
        timestamp="2026-03-27T12:00:00",
        cpu_percent=45.2,
        memory_percent=62.1,
        disk_percent=38.5,
        temperature=55.0,
        load_avg=[1.2, 0.8, 0.5],
        network_sent_mb=100.0,
        network_recv_mb=200.0,
    )
    assert snap.cpu_percent == 45.2


def test_git_commit_request():
    req = GitCommitRequest(message="fix: bug")
    assert req.message == "fix: bug"
    assert req.files is None


def test_package_install_request():
    req = PackageInstallRequest(manager="pip", packages=["flask", "requests"])
    assert req.manager == "pip"
    assert len(req.packages) == 2


def test_package_install_invalid_manager():
    with pytest.raises(ValidationError):
        PackageInstallRequest(manager="brew", packages=["x"])


def test_service_action_valid():
    sa = ServiceAction(action="restart")
    assert sa.action == "restart"


def test_service_action_invalid():
    with pytest.raises(ValidationError):
        ServiceAction(action="destroy")


def test_ssh_connect_request():
    req = SshConnectRequest(host="192.168.1.1", username="root")
    assert req.port == 22  # default


def test_agent_definition():
    agent = AgentDefinition(
        name="backup-agent",
        description="Runs backups",
        trigger="manual",
        tools=["shell_exec", "file_read"],
    )
    assert agent.name == "backup-agent"
    assert len(agent.tools) == 2


def test_ai_chat_request():
    req = AIChatRequest(message="hello")
    assert req.model == "linux-ai-agent"


def test_alert_config_defaults():
    cfg = AlertConfig()
    assert cfg.cpu_percent == 85
    assert cfg.memory_percent == 85


def test_log_search_request():
    req = LogSearchRequest(pattern="error")
    assert req.limit == 100
    assert req.source is None


def test_cpu_metrics_response():
    m = CpuMetricsResponse(
        cpu_id=0, usage_percent=45.2, frequency_mhz=2400,
        temperature_c=55.0, io_read_bytes=1024, io_write_bytes=512,
    )
    assert m.cpu_id == 0


def test_file_write_request():
    req = FileWriteRequest(path="/tmp/test.txt", content="hello")
    assert req.mode == "write"


def test_file_edit_request():
    req = FileEditRequest(path="/tmp/test.txt", old_string="hello", new_string="world")
    assert req.old_string == "hello"
