"""API key generation and hashing."""

from __future__ import annotations

import hashlib
import secrets


def generate_api_key() -> str:
    return secrets.token_hex(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()
