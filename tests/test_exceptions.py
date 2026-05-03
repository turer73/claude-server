from app.exceptions import (
    AuthenticationError,
    AuthorizationError,
    KernelError,
    NotFoundError,
    RateLimitError,
    ServerError,
    ShellExecutionError,
)


def test_server_error_defaults():
    err = ServerError("test")
    assert err.message == "test"
    assert err.status_code == 500
    assert err.detail is None


def test_server_error_custom():
    err = ServerError("test", status_code=418, detail="teapot")
    assert err.status_code == 418
    assert err.detail == "teapot"


def test_auth_error():
    err = AuthenticationError()
    assert err.status_code == 401
    assert "Authentication" in err.message


def test_authz_error():
    err = AuthorizationError()
    assert err.status_code == 403


def test_not_found_error():
    err = NotFoundError()
    assert err.status_code == 404


def test_kernel_error():
    err = KernelError("module not loaded")
    assert err.status_code == 502
    assert err.message == "module not loaded"


def test_rate_limit_error():
    err = RateLimitError()
    assert err.status_code == 429


def test_shell_execution_error():
    err = ShellExecutionError()
    assert err.status_code == 500
