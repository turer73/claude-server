import pytest
import hashlib
from app.auth.jwt_handler import create_token, decode_token
from app.auth.api_key import hash_api_key, generate_api_key
from app.auth.permissions import Permission, check_permission
from app.exceptions import AuthenticationError


# --- JWT ---

def test_create_and_decode_jwt():
    token = create_token(subject="admin", permissions="admin", secret="test-secret")
    payload = decode_token(token, secret="test-secret")
    assert payload["sub"] == "admin"
    assert payload["permissions"] == "admin"


def test_decode_invalid_jwt():
    with pytest.raises(AuthenticationError):
        decode_token("invalid.token.here", secret="test-secret")


def test_decode_wrong_secret():
    token = create_token(subject="admin", permissions="admin", secret="secret-1")
    with pytest.raises(AuthenticationError):
        decode_token(token, secret="secret-2")


def test_jwt_contains_expiry():
    token = create_token(subject="admin", permissions="admin", secret="s")
    payload = decode_token(token, secret="s")
    assert "exp" in payload
    assert "iat" in payload


# --- API Key ---

def test_hash_api_key():
    key = "test-api-key-12345"
    hashed = hash_api_key(key)
    assert hashed == hashlib.sha256(key.encode()).hexdigest()
    assert hash_api_key(key) == hashed  # deterministic


def test_generate_api_key():
    key = generate_api_key()
    assert len(key) == 64  # 32 bytes hex
    key2 = generate_api_key()
    assert key != key2  # unique


# --- Permissions ---

def test_permission_read():
    assert check_permission(Permission.READ, "read")
    assert check_permission(Permission.READ, "write")
    assert check_permission(Permission.READ, "admin")


def test_permission_write():
    assert not check_permission(Permission.WRITE, "read")
    assert check_permission(Permission.WRITE, "write")
    assert check_permission(Permission.WRITE, "admin")


def test_permission_admin():
    assert not check_permission(Permission.ADMIN, "read")
    assert not check_permission(Permission.ADMIN, "write")
    assert check_permission(Permission.ADMIN, "admin")


def test_permission_unknown():
    assert not check_permission(Permission.ADMIN, "unknown")
