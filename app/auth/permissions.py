"""RBAC permission checking."""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


_LEVEL = {"read": 1, "write": 2, "admin": 3}


def check_permission(required: Permission, user_permission: str) -> bool:
    return _LEVEL.get(user_permission, 0) >= _LEVEL.get(required, 99)
