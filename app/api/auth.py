"""Auth API endpoints — token generation and user info."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from app.auth.jwt_handler import create_token, decode_token
from app.core.config import Settings, get_settings
from app.exceptions import AuthenticationError

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class TokenRequest(BaseModel):
    api_key: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserInfo(BaseModel):
    name: str
    permissions: str


@router.post("/token", response_model=TokenResponse)
async def get_token(body: TokenRequest, settings: Settings = Depends(get_settings)):
    token = create_token(
        subject="api-user",
        permissions="admin",
        secret=settings.jwt_secret,
        ttl_hours=settings.jwt_ttl_hours,
    )
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_ttl_hours * 3600,
    )


@router.get("/me", response_model=UserInfo)
async def get_me(
    authorization: str = Header(...),
    settings: Settings = Depends(get_settings),
):
    if not authorization.startswith("Bearer "):
        raise AuthenticationError("Invalid authorization header")
    token = authorization.removeprefix("Bearer ")
    payload = decode_token(token, settings.jwt_secret)
    return UserInfo(name=payload["sub"], permissions=payload["permissions"])
