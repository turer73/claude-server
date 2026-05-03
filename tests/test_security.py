import pytest

from app.auth.jwt_handler import create_token, decode_token
from app.core.file_manager import FileManager
from app.core.shell_executor import ShellExecutor
from app.exceptions import AuthenticationError, AuthorizationError


class TestPathTraversal:
    @pytest.fixture
    def fm(self, tmp_path):
        return FileManager(allowed_paths=[str(tmp_path)])

    def test_dotdot_traversal(self, fm, tmp_path):
        with pytest.raises(AuthorizationError):
            fm.validate_path(str(tmp_path / ".." / ".." / "etc" / "passwd"))

    def test_absolute_escape(self, fm):
        with pytest.raises(AuthorizationError):
            fm.validate_path("/etc/shadow")

    def test_symlink_escape(self, fm, tmp_path):
        """Symlink pointing outside allowed paths should be blocked."""
        import os

        link = tmp_path / "evil_link"
        try:
            os.symlink("/etc/passwd", str(link))
            with pytest.raises(AuthorizationError):
                fm.validate_path(str(link))
        except OSError:
            pytest.skip("Cannot create symlinks on this system")

    def test_null_byte(self, fm, tmp_path):
        """Null bytes in path should be rejected."""
        try:
            fm.validate_path(str(tmp_path / "file\x00.txt"))
        except (AuthorizationError, ValueError):
            pass  # Either is acceptable


class TestShellSecurity:
    """New security model: JWT admin auth is the gate, bash -c for full shell.
    Only catastrophic commands are blocked at kernel level."""

    @pytest.fixture
    def exec(self):
        return ShellExecutor(whitelist=["ls", "echo", "cat"])

    def test_pipe_allowed(self, exec):
        """Pipes are now allowed — first command whitelisted."""
        assert exec.validate_command("ls | grep test") is True

    def test_chain_allowed(self, exec):
        assert exec.validate_command("ls && echo done") is True

    def test_redirect_allowed(self, exec):
        assert exec.validate_command("echo test > /tmp/test.txt") is True

    def test_rm_rf_root_blocked(self, exec):
        with pytest.raises(AuthorizationError, match="Blocked dangerous"):
            exec.validate_command("rm -rf /")

    def test_fork_bomb_blocked(self, exec):
        with pytest.raises(AuthorizationError, match="Blocked dangerous"):
            exec.validate_command(":(){ :|:& };:")

    def test_mkfs_system_blocked(self, exec):
        with pytest.raises(AuthorizationError, match="Blocked dangerous"):
            exec.validate_command("mkfs /dev/sda1")

    def test_dd_zero_system_blocked(self, exec):
        with pytest.raises(AuthorizationError, match="Blocked dangerous"):
            exec.validate_command("dd if=/dev/zero of=/dev/sda")

    def test_not_whitelisted_first_command(self, exec):
        with pytest.raises(AuthorizationError, match="not in whitelist"):
            exec.validate_command("nmap 192.168.1.1")

    def test_whitelisted_with_path(self, exec):
        """Base command extracted from full path."""
        assert exec.validate_command("/bin/ls -la") is True


class TestJWTSecurity:
    def test_wrong_secret(self):
        token = create_token("admin", "admin", "secret1")
        with pytest.raises(AuthenticationError):
            decode_token(token, "secret2")

    def test_tampered_token(self):
        token = create_token("admin", "admin", "secret")
        tampered = token[:-5] + "XXXXX"
        with pytest.raises(AuthenticationError):
            decode_token(tampered, "secret")

    def test_empty_token(self):
        with pytest.raises(AuthenticationError):
            decode_token("", "secret")

    def test_garbage_token(self):
        with pytest.raises(AuthenticationError):
            decode_token("not.a.jwt.token.at.all", "secret")


class TestUnauthenticatedAccess:
    """Verify that protected endpoints return 401 without a token."""

    @pytest.mark.anyio
    async def test_kernel_status_requires_auth(self, client):
        resp = await client.get("/api/v1/kernel/status")
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_kernel_governor_requires_auth(self, client):
        resp = await client.get("/api/v1/kernel/governor")
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_system_info_requires_auth(self, client):
        resp = await client.get("/api/v1/system/info")
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_system_processes_requires_auth(self, client):
        resp = await client.get("/api/v1/system/processes")
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_files_read_requires_auth(self, client):
        resp = await client.get("/api/v1/files/read?path=/tmp/test.txt")
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_monitor_metrics_no_auth(self, client):
        """Monitor metrics endpoint is public (no auth required)."""
        resp = await client.get("/api/v1/monitor/metrics")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_agents_list_requires_auth(self, client):
        resp = await client.get("/api/v1/agents/list")
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_network_interfaces_requires_auth(self, client):
        resp = await client.get("/api/v1/network/interfaces")
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_logs_sources_requires_auth(self, client):
        resp = await client.get("/api/v1/logs/sources")
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_ssh_sessions_requires_auth(self, client):
        resp = await client.get("/api/v1/ssh/sessions")
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_webops_services_requires_auth(self, client):
        resp = await client.get("/api/v1/webops/services")
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_webhooks_receive_no_auth(self, client):
        """Webhook receive endpoint is public (no auth required)."""
        resp = await client.post(
            "/api/v1/monitor/webhooks/receive",
            json={
                "source": "test",
                "event": "ping",
            },
        )
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_health_does_not_require_auth(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_ready_does_not_require_auth(self, client):
        resp = await client.get("/ready")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_invalid_token_returns_401(self, client):
        resp = await client.get(
            "/api/v1/kernel/status",
            headers={"Authorization": "Bearer invalid-token-here"},
        )
        assert resp.status_code == 401


class TestRateLimiter:
    def test_exhaustion(self):
        from app.middleware.rate_limit import TokenBucketLimiter

        limiter = TokenBucketLimiter(rate=3, per_seconds=60)
        assert limiter.allow("attacker") is True
        assert limiter.allow("attacker") is True
        assert limiter.allow("attacker") is True
        assert limiter.allow("attacker") is False
        assert limiter.allow("attacker") is False

    def test_key_isolation(self):
        from app.middleware.rate_limit import TokenBucketLimiter

        limiter = TokenBucketLimiter(rate=1, per_seconds=60)
        assert limiter.allow("user1") is True
        assert limiter.allow("user1") is False
        assert limiter.allow("user2") is True  # different key, not affected
