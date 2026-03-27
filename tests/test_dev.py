import pytest
import subprocess
from app.core.dev_manager import DevManager
from app.exceptions import ShellExecutionError


@pytest.fixture
def dev(tmp_path):
    return DevManager(base_path=str(tmp_path))


@pytest.fixture
def git_repo(tmp_path):
    """Create a real git repo for testing."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True)
    # Create initial file and commit
    (tmp_path / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
    return tmp_path


def test_git_status(dev, git_repo):
    status = dev.git_status(str(git_repo))
    assert "branch" in status
    assert "clean" in status
    assert isinstance(status["staged"], list)
    assert isinstance(status["modified"], list)
    assert isinstance(status["untracked"], list)


def test_git_status_clean(dev, git_repo):
    status = dev.git_status(str(git_repo))
    assert status["clean"] is True


def test_git_status_with_changes(dev, git_repo):
    (git_repo / "new_file.txt").write_text("new")
    status = dev.git_status(str(git_repo))
    assert status["clean"] is False
    assert "new_file.txt" in status["untracked"]


def test_git_log(dev, git_repo):
    log = dev.git_log(str(git_repo), limit=5)
    assert isinstance(log, list)
    assert len(log) >= 1
    entry = log[0]
    assert "hash" in entry
    assert "message" in entry
    assert "author" in entry


def test_git_diff(dev, git_repo):
    (git_repo / "README.md").write_text("# Changed")
    diff = dev.git_diff(str(git_repo))
    assert isinstance(diff, str)
    assert "Changed" in diff or "diff" in diff.lower()


def test_git_commit(dev, git_repo):
    (git_repo / "newfile.txt").write_text("content")
    dev.git_add(str(git_repo), ["newfile.txt"])
    result = dev.git_commit(str(git_repo), "test commit")
    assert result is True
    log = dev.git_log(str(git_repo), limit=1)
    assert "test commit" in log[0]["message"]


def test_git_branch(dev, git_repo):
    branches = dev.git_branches(str(git_repo))
    assert isinstance(branches, list)
    assert len(branches) >= 1
