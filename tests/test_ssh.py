import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from app.core.ssh_client import SSHClient, SSHSessionManager


@pytest.fixture
def session_mgr():
    return SSHSessionManager(max_sessions=5)


def test_session_manager_empty(session_mgr):
    assert session_mgr.list_sessions() == []
    assert session_mgr.count() == 0


def test_session_manager_add(session_mgr):
    mock_client = MagicMock()
    sid = session_mgr.add("testhost", "root", mock_client)
    assert sid is not None
    assert session_mgr.count() == 1
    sessions = session_mgr.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["host"] == "testhost"


def test_session_manager_get(session_mgr):
    mock_client = MagicMock()
    sid = session_mgr.add("host1", "user1", mock_client)
    client = session_mgr.get(sid)
    assert client is mock_client


def test_session_manager_get_not_found(session_mgr):
    from app.exceptions import NotFoundError
    with pytest.raises(NotFoundError):
        session_mgr.get("nonexistent-id")


def test_session_manager_remove(session_mgr):
    mock_client = MagicMock()
    sid = session_mgr.add("host1", "user1", mock_client)
    session_mgr.remove(sid)
    assert session_mgr.count() == 0
    mock_client.close.assert_called_once()


def test_session_manager_max_sessions(session_mgr):
    for i in range(5):
        session_mgr.add(f"host{i}", f"user{i}", MagicMock())
    from app.exceptions import RateLimitError
    with pytest.raises(RateLimitError, match="Max SSH sessions"):
        session_mgr.add("host5", "user5", MagicMock())


def test_ssh_client_connect_mock():
    with patch("paramiko.SSHClient") as MockSSH:
        mock_instance = MagicMock()
        MockSSH.return_value = mock_instance

        client = SSHClient()
        paramiko_client = client.connect(
            host="192.168.1.1",
            username="root",
            password="pass123",
            port=22,
        )

        mock_instance.set_missing_host_key_policy.assert_called_once()
        mock_instance.connect.assert_called_once_with(
            hostname="192.168.1.1",
            username="root",
            password="pass123",
            port=22,
            key_filename=None,
            timeout=10,
        )


def test_ssh_client_exec_mock():
    mock_client = MagicMock()
    mock_stdin = MagicMock()
    mock_stdout = MagicMock()
    mock_stderr = MagicMock()
    mock_stdout.read.return_value = b"hello\n"
    mock_stderr.read.return_value = b""
    mock_stdout.channel.recv_exit_status.return_value = 0
    mock_client.exec_command.return_value = (mock_stdin, mock_stdout, mock_stderr)

    client = SSHClient()
    result = client.exec_command(mock_client, "echo hello")
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


def test_ssh_client_exec_timeout():
    mock_client = MagicMock()
    mock_stdout = MagicMock()
    mock_stdout.channel.recv_exit_status.side_effect = Exception("timeout")
    mock_client.exec_command.return_value = (MagicMock(), mock_stdout, MagicMock())

    client = SSHClient()
    from app.exceptions import ShellExecutionError
    with pytest.raises(ShellExecutionError):
        client.exec_command(mock_client, "sleep 100", timeout=1)


def test_ssh_client_connect_failure():
    with patch("paramiko.SSHClient") as MockSSH:
        mock_instance = MagicMock()
        mock_instance.connect.side_effect = Exception("Connection refused")
        MockSSH.return_value = mock_instance
        client = SSHClient()
        from app.exceptions import ShellExecutionError
        with pytest.raises(ShellExecutionError, match="SSH connection failed"):
            client.connect(host="bad-host", username="root")


def test_ssh_upload_file():
    mock_client = MagicMock()
    mock_sftp = MagicMock()
    mock_client.open_sftp.return_value = mock_sftp
    client = SSHClient()
    result = client.upload_file(mock_client, "/tmp/local", "/tmp/remote")
    assert result is True
    mock_sftp.put.assert_called_once_with("/tmp/local", "/tmp/remote")
    mock_sftp.close.assert_called_once()


def test_ssh_upload_file_failure():
    mock_client = MagicMock()
    mock_client.open_sftp.side_effect = Exception("SFTP error")
    client = SSHClient()
    from app.exceptions import ShellExecutionError
    with pytest.raises(ShellExecutionError, match="SFTP upload failed"):
        client.upload_file(mock_client, "/tmp/local", "/tmp/remote")


def test_ssh_download_file():
    mock_client = MagicMock()
    mock_sftp = MagicMock()
    mock_client.open_sftp.return_value = mock_sftp
    client = SSHClient()
    result = client.download_file(mock_client, "/tmp/remote", "/tmp/local")
    assert result is True
    mock_sftp.get.assert_called_once_with("/tmp/remote", "/tmp/local")
    mock_sftp.close.assert_called_once()


def test_ssh_download_file_failure():
    mock_client = MagicMock()
    mock_client.open_sftp.side_effect = Exception("SFTP error")
    client = SSHClient()
    from app.exceptions import ShellExecutionError
    with pytest.raises(ShellExecutionError, match="SFTP download failed"):
        client.download_file(mock_client, "/tmp/remote", "/tmp/local")


def test_session_manager_close_all(session_mgr):
    clients = [MagicMock() for _ in range(3)]
    for i, c in enumerate(clients):
        session_mgr.add(f"host{i}", f"user{i}", c)
    assert session_mgr.count() == 3
    session_mgr.close_all()
    assert session_mgr.count() == 0
    for c in clients:
        c.close.assert_called_once()


def test_session_manager_remove_nonexistent(session_mgr):
    """Olmayan session remove — hata vermemeli."""
    session_mgr.remove("ghost")
