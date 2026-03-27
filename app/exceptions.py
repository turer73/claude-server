"""Custom exception hierarchy for Linux-AI Server."""

from __future__ import annotations


class ServerError(Exception):
    """Base exception for all server errors."""

    def __init__(self, message: str, status_code: int = 500, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


class AuthenticationError(ServerError):
    def __init__(self, message: str = "Authentication required"):
        super().__init__(message, status_code=401)


class AuthorizationError(ServerError):
    def __init__(self, message: str = "Insufficient permissions"):
        super().__init__(message, status_code=403)


class NotFoundError(ServerError):
    def __init__(self, message: str = "Resource not found"):
        super().__init__(message, status_code=404)


class ValidationError(ServerError):
    def __init__(self, message: str = "Validation error"):
        super().__init__(message, status_code=422)


class RateLimitError(ServerError):
    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(message, status_code=429)


class KernelError(ServerError):
    def __init__(self, message: str = "Kernel operation failed"):
        super().__init__(message, status_code=502)


class ShellExecutionError(ServerError):
    def __init__(self, message: str = "Command execution failed"):
        super().__init__(message, status_code=500)
