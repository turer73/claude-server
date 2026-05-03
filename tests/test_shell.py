import shutil
import sys

import pytest

from app.core.shell_executor import ShellExecutor
from app.exceptions import AuthorizationError

_PYTHON = "python3" if shutil.which("python3") and sys.platform != "win32" else "python"


@pytest.fixture
def executor():
    return ShellExecutor(whitelist=["ls", "echo", "cat", "whoami", "python3", "python", "git", "pip"])


def test_validate_command_allowed(executor):
    assert executor.validate_command("ls -la /home") is True


def test_validate_command_not_whitelisted(executor):
    with pytest.raises(AuthorizationError, match="not in whitelist"):
        executor.validate_command("nmap 192.168.1.1")


def test_validate_command_pipe_allowed(executor):
    """Pipes are now allowed — first command must be whitelisted."""
    assert executor.validate_command("ls | grep test") is True


def test_validate_command_chain_allowed(executor):
    """Chaining with && is now allowed."""
    assert executor.validate_command("ls && echo done") is True


def test_validate_dangerous_rm_rf_root(executor):
    """rm -rf / is always blocked regardless of whitelist."""
    with pytest.raises(AuthorizationError, match="Blocked dangerous"):
        executor.validate_command("rm -rf /")


def test_validate_dangerous_fork_bomb(executor):
    with pytest.raises(AuthorizationError, match="Blocked dangerous"):
        executor.validate_command(":(){ :|:& };:")


def test_validate_dangerous_mkfs(executor):
    with pytest.raises(AuthorizationError, match="Blocked dangerous"):
        executor.validate_command("mkfs /dev/sda1")


def test_validate_command_empty(executor):
    with pytest.raises(AuthorizationError):
        executor.validate_command("")


def test_validate_command_with_args(executor):
    assert executor.validate_command("git status") is True
    assert executor.validate_command("pip install flask") is True


def test_validate_sudo_prefix(executor):
    """sudo prefix is handled — underlying command checked."""
    assert executor.validate_command("sudo ls -la") is True


@pytest.mark.anyio
async def test_execute_simple_command(executor):
    result = await executor.execute("echo hello")
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


@pytest.mark.anyio
async def test_execute_pipe(executor):
    """Pipes now work with subprocess_shell."""
    result = await executor.execute("echo hello world | cat")
    assert result["exit_code"] == 0
    assert "hello world" in result["stdout"]


@pytest.mark.anyio
async def test_execute_not_whitelisted(executor):
    with pytest.raises(AuthorizationError):
        await executor.execute("nmap localhost")


@pytest.mark.anyio
async def test_execute_returns_stderr(executor):
    result = await executor.execute(f"{_PYTHON} -c \"__import__('sys').stderr.write('err')\"")
    assert "err" in result["stderr"]


@pytest.mark.anyio
async def test_execute_nonzero_exit(executor):
    result = await executor.execute(f'{_PYTHON} -c "exit(1)"')
    assert result["exit_code"] == 1
