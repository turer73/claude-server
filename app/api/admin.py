"""Admin API — server-side .env secrets management (dashboard).

Auth: JWT (require_auth from middleware) — dashboard kullanir.
Memory router'in X-Memory-Key auth'unu kirmaz; secrets endpoint'leri
dashboard ile uyumlu olacak sekilde ayri router'a yerlestirildi.
"""

from __future__ import annotations

import os
import re
import subprocess

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.middleware.dependencies import require_auth

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class SecretSet(BaseModel):
    key: str
    value: str

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        if not re.match(r"^[A-Z_][A-Z0-9_]*$", v):
            raise ValueError("key must match ^[A-Z_][A-Z0-9_]*$")
        if len(v) > 80:
            raise ValueError("key too long (>80)")
        return v

    @field_validator("value")
    @classmethod
    def validate_value(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("value cannot be empty")
        if len(v) > 4000:
            raise ValueError("value too long (>4000)")
        return v


ENV_PATH = "/opt/linux-ai-server/.env"
HELPER_PATH = "/opt/linux-ai-server/scripts/set-env-secret.sh"


@router.get("/secrets")
async def list_secrets(_: None = Depends(require_auth)) -> dict:
    """.env'deki KEY listesi. Value asla donmez — sadece key + length."""
    if not os.path.exists(ENV_PATH):
        return {"count": 0, "keys": []}
    keys = []
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.rstrip("\n\r")
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if not re.match(r"^[A-Z_][A-Z0-9_]*$", k):
                    continue
                keys.append({"key": k, "length": len(v)})
    except OSError as e:
        raise HTTPException(500, f"read failed: {e}")
    return {"count": len(keys), "keys": sorted(keys, key=lambda r: r["key"])}


@router.post("/secrets")
async def set_secret(data: SecretSet, _: None = Depends(require_auth)) -> dict:
    """.env'e KEY=VALUE upsert. Helper subprocess (idempotent)."""
    if not os.path.exists(HELPER_PATH):
        raise HTTPException(500, f"helper missing: {HELPER_PATH}")
    try:
        proc = subprocess.run(
            ["bash", HELPER_PATH, data.key],
            input=data.value,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(500, "helper timeout")
    if proc.returncode != 0:
        raise HTTPException(400, f"helper rc={proc.returncode}: {proc.stderr.strip()[:200]}")
    return {
        "key": data.key,
        "action": proc.stdout.strip(),
        "value_length": len(data.value),
    }
