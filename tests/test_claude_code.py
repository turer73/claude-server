"""Tests for Claude Code API — status, run, sessions."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.anyio
async def test_claude_status_available(client, auth_headers):
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"1.0.0\n", b"")

    with (
        patch("app.api.claude_code._find_claude", return_value="/usr/bin/claude"),
        patch("app.api.claude_code._load_claude_token", return_value="test-token"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        resp = await client.get("/api/v1/claude/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert "1.0.0" in data["version"]


@pytest.mark.anyio
async def test_claude_status_not_available(client, auth_headers):
    with patch("app.api.claude_code._find_claude", return_value=None):
        resp = await client.get("/api/v1/claude/status", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["available"] is False


@pytest.mark.anyio
async def test_claude_run_no_binary(client, auth_headers):
    with patch("app.api.claude_code._find_claude", return_value=None):
        resp = await client.post("/api/v1/claude/run", json={"prompt": "hello"}, headers=auth_headers)
        assert resp.status_code == 200
        assert "error" in resp.json()


@pytest.mark.anyio
async def test_claude_run_success(client, auth_headers):
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (
        b'[{"type":"result","session_id":"abc123","result":"Hello!","total_cost_usd":0.01,"is_error":false}]',
        b"",
    )
    mock_proc.kill = MagicMock()

    with (
        patch("app.api.claude_code._find_claude", return_value="/usr/bin/claude"),
        patch("app.api.claude_code._build_env", return_value={}),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        resp = await client.post("/api/v1/claude/run", json={"prompt": "hello"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["session_id"] == "abc123"
        assert data["cost"] == 0.01


@pytest.mark.anyio
async def test_claude_run_read_only_uses_allowlist(client, auth_headers):
    """read_only=True -> `--allowedTools <read-only set>` (skip-permissions DEĞİL).
    git log gibi read-only kabuk ÇALIŞIR, mutasyon araçları (Edit/Write) listede yok."""
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b'{"result":"ok","session_id":"s1"}', b"")
    mock_proc.kill = MagicMock()
    captured = {}

    def _spawn(*args, **kwargs):
        captured["argv"] = args
        return mock_proc

    with (
        patch("app.api.claude_code._find_claude", return_value="/usr/bin/claude"),
        patch("app.api.claude_code._build_env", return_value={}),
        patch("asyncio.create_subprocess_exec", side_effect=_spawn),
    ):
        resp = await client.post("/api/v1/claude/run", json={"prompt": "durum?", "read_only": True}, headers=auth_headers)
        assert resp.status_code == 200
    argv = captured["argv"]
    assert "--allowedTools" in argv
    tools = argv[argv.index("--allowedTools") + 1]
    assert "Bash(git log:*)" in tools
    assert "Read" in tools
    assert "git branch" not in tools  # P2: git branch mutasyon yapabilir, hariç
    assert "--dangerously-skip-permissions" not in argv
    # P1: disallowedTools mutasyonu KESİN engeller (settings'i ezer)
    assert "--disallowedTools" in argv
    dis = argv[argv.index("--disallowedTools") + 1]
    assert "Edit" in dis
    assert "Write" in dis
    assert "Bash(rm:*)" in dis


@pytest.mark.anyio
async def test_claude_run_default_uses_skip_permissions(client, auth_headers):
    """read_only verilmezse mevcut web-UI davranışı korunur (skip-permissions)."""
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b'{"result":"ok"}', b"")
    mock_proc.kill = MagicMock()
    captured = {}

    def _spawn(*args, **kwargs):
        captured["argv"] = args
        return mock_proc

    with (
        patch("app.api.claude_code._find_claude", return_value="/usr/bin/claude"),
        patch("app.api.claude_code._build_env", return_value={}),
        patch("asyncio.create_subprocess_exec", side_effect=_spawn),
    ):
        resp = await client.post("/api/v1/claude/run", json={"prompt": "x"}, headers=auth_headers)
        assert resp.status_code == 200
    assert "--dangerously-skip-permissions" in captured["argv"]


@pytest.mark.anyio
async def test_claude_run_vps_read_only_uses_allowlist(client, auth_headers, monkeypatch):
    """Codex P2: host=vps + read_only=True -> VPS remote komutu `--allowedTools` içerir."""
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b'{"result":"ok"}', b"")
    mock_proc.kill = MagicMock()
    captured = {}

    def _spawn(*args, **kwargs):
        captured["argv"] = args
        return mock_proc

    class _S:
        vps_host = "root@vps"

    monkeypatch.setattr("app.api.claude_code.get_settings", lambda: _S())
    with patch("asyncio.create_subprocess_exec", side_effect=_spawn):
        resp = await client.post(
            "/api/v1/claude/run",
            json={"prompt": "durum", "host": "vps", "read_only": True},
            headers=auth_headers,
        )
        assert resp.status_code == 200
    remote = captured["argv"][-1]  # ssh_cmd son arg = remote komut string'i
    assert "--allowedTools" in remote


@pytest.mark.anyio
async def test_claude_run_requires_admin(client, read_headers):
    resp = await client.post("/api/v1/claude/run", json={"prompt": "hello"}, headers=read_headers)
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_claude_sessions_empty(client, auth_headers):
    with patch("os.path.isdir", return_value=False):
        resp = await client.get("/api/v1/claude/sessions", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["sessions"] == []


@pytest.mark.anyio
async def test_claude_run_with_session(client, auth_headers):
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b'{"result":"continued","session_id":"abc123"}', b"")
    mock_proc.kill = MagicMock()

    with (
        patch("app.api.claude_code._find_claude", return_value="/usr/bin/claude"),
        patch("app.api.claude_code._build_env", return_value={}),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        resp = await client.post(
            "/api/v1/claude/run",
            json={
                "prompt": "continue",
                "session_id": "abc123",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "abc123"


@pytest.mark.anyio
async def test_claude_ui(client, auth_headers):
    resp = await client.get("/api/v1/claude/ui", headers=auth_headers)
    # Should return HTML or 404
    assert resp.status_code in (200, 404)


# ── Max-plan zorunlu (kullanıcı: "API istemiyorum") ──────────────


def test_build_env_strips_api_key(monkeypatch):
    """_build_env ANTHROPIC_API_KEY/AUTH_TOKEN'i SİLER -> claude abonelik kimliğine düşer
    (pay-per-token API'ye asla)."""
    from app.api import claude_code

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-strip")
    env = claude_code._build_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_is_authenticated_via_credentials_file(monkeypatch):
    """OAuth token yoksa bile ~/.claude/.credentials.json varsa authenticated (Max login)."""
    from app.api import claude_code

    monkeypatch.setattr(claude_code, "_load_claude_token", lambda: None)
    monkeypatch.setattr(claude_code.os.path, "exists", lambda p: str(p).endswith(".credentials.json"))
    assert claude_code._is_authenticated() is True


def test_is_authenticated_none(monkeypatch):
    """Ne token ne credentials -> authenticated False."""
    from app.api import claude_code

    monkeypatch.setattr(claude_code, "_load_claude_token", lambda: None)
    monkeypatch.setattr(claude_code.os.path, "exists", lambda p: False)
    assert claude_code._is_authenticated() is False
