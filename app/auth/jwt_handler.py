"""JWT token creation and verification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt

from app.exceptions import AuthenticationError

ALGORITHM = "HS256"


def create_token(
    subject: str,
    permissions: str,
    secret: str,
    ttl_hours: int = 1,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "permissions": permissions,
        "iat": now,
        "exp": now + timedelta(hours=ttl_hours),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_token(token: str, secret: str) -> dict:
    try:
        return jwt.decode(token, secret, algorithms=[ALGORITHM])
    except JWTError as e:
        raise AuthenticationError(f"Invalid token: {e}")
