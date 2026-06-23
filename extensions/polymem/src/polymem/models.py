"""Pydantic v2 models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MemoryType = Literal["user", "feedback", "project", "reference"]


class MemoryCreate(BaseModel):
    type: MemoryType
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)
    source_device: str | None = None
    rationale: str | None = None


class MemoryUpdate(BaseModel):
    description: str | None = None
    content: str | None = None
    rationale: str | None = None
    active: int | None = None


class MemoryRead(BaseModel):
    id: int
    type: MemoryType
    name: str
    description: str
    content: str
    source_device: str | None
    rationale: str | None
    active: int
    read_count: int
    last_read_at: str | None
    created_at: str
    updated_at: str


# ----- devices -----


class DeviceRegister(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=50)
    hostname: str | None = None
    ip: str | None = None
    mesh_ip: str | None = None
    os_version: str | None = None
    client_version: str | None = None
    notes: str | None = None


class DeviceRead(BaseModel):
    id: int
    name: str
    platform: str
    hostname: str | None
    ip: str | None
    mesh_ip: str | None
    os_version: str | None
    client_version: str | None
    notes: str | None
    last_seen: str
    created_at: str


class DeviceProjectCreate(BaseModel):
    project: str = Field(min_length=1, max_length=200)
    local_path: str | None = None


class DeviceProjectRead(BaseModel):
    id: int
    device_name: str
    project: str
    local_path: str | None
    last_activity: str


# ----- sessions -----


class SessionCreate(BaseModel):
    summary: str = Field(min_length=1)
    device_name: str | None = None
    project: str | None = None
    date: str | None = None  # ISO YYYY-MM-DD; defaults to today at DB level
    metadata: dict[str, Any] | None = None


class SessionRead(BaseModel):
    id: int
    device_name: str | None
    project: str | None
    date: str
    summary: str
    metadata: dict[str, Any] | None
    created_at: str


# ----- search -----


class SearchMemoryHit(BaseModel):
    id: int
    type: MemoryType
    name: str
    description: str
    snippet: str


class SearchSessionHit(BaseModel):
    id: int
    date: str
    device_name: str | None
    project: str | None
    snippet: str


class SearchResults(BaseModel):
    memories: list[SearchMemoryHit]
    sessions: list[SearchSessionHit]


class SearchResponse(BaseModel):
    query: str
    total: int
    results: SearchResults
