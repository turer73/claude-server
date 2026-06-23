"""Devices + device_projects router."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from polymem.db import connect
from polymem.models import DeviceProjectCreate, DeviceProjectRead, DeviceRead, DeviceRegister


def build_router(db_path: str | Path, auth_dep) -> APIRouter:
    router = APIRouter(prefix="/devices", tags=["devices"], dependencies=[Depends(auth_dep)])

    @router.get("", response_model=list[DeviceRead])
    async def list_devices():
        with connect(db_path) as db:
            rows = db.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
        return [dict(r) for r in rows]

    @router.get("/{name}", response_model=DeviceRead)
    async def get_device(name: str):
        with connect(db_path) as db:
            row = db.execute("SELECT * FROM devices WHERE name = ?", (name,)).fetchone()
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="device not found")
        return dict(row)

    @router.post("", response_model=DeviceRead, status_code=status.HTTP_200_OK)
    async def register_device(data: DeviceRegister):
        """Upsert by name. Refreshes last_seen on conflict."""
        with connect(db_path) as db:
            db.execute(
                """
                INSERT INTO devices
                    (name, platform, hostname, ip, mesh_ip, os_version, client_version, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    platform       = excluded.platform,
                    hostname       = excluded.hostname,
                    ip             = excluded.ip,
                    mesh_ip        = excluded.mesh_ip,
                    os_version     = excluded.os_version,
                    client_version = excluded.client_version,
                    notes          = excluded.notes,
                    last_seen      = datetime('now')
                """,
                (
                    data.name,
                    data.platform,
                    data.hostname,
                    data.ip,
                    data.mesh_ip,
                    data.os_version,
                    data.client_version,
                    data.notes,
                ),
            )
            db.commit()
            row = db.execute("SELECT * FROM devices WHERE name = ?", (data.name,)).fetchone()
        return dict(row)

    @router.post("/{name}/ping", response_model=DeviceRead)
    async def ping_device(name: str):
        with connect(db_path) as db:
            cursor = db.execute(
                "UPDATE devices SET last_seen = datetime('now') WHERE name = ?",
                (name,),
            )
            db.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="device not found")
            row = db.execute("SELECT * FROM devices WHERE name = ?", (name,)).fetchone()
        return dict(row)

    @router.delete("/{name}", status_code=status.HTTP_200_OK)
    async def delete_device(name: str):
        with connect(db_path) as db:
            cursor = db.execute("DELETE FROM devices WHERE name = ?", (name,))
            db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="device not found")
        return {"name": name, "deleted": True}

    # ----- device_projects -----

    @router.get("/{name}/projects", response_model=list[DeviceProjectRead])
    async def list_device_projects(name: str):
        with connect(db_path) as db:
            if not db.execute("SELECT 1 FROM devices WHERE name = ?", (name,)).fetchone():
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="device not found")
            rows = db.execute(
                "SELECT * FROM device_projects WHERE device_name = ? ORDER BY last_activity DESC",
                (name,),
            ).fetchall()
        return [dict(r) for r in rows]

    @router.post(
        "/{name}/projects",
        response_model=DeviceProjectRead,
        status_code=status.HTTP_200_OK,
    )
    async def upsert_device_project(name: str, data: DeviceProjectCreate):
        """Upsert by (device_name, project). Refreshes last_activity on conflict."""
        with connect(db_path) as db:
            if not db.execute("SELECT 1 FROM devices WHERE name = ?", (name,)).fetchone():
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="device not found")
            db.execute(
                """
                INSERT INTO device_projects (device_name, project, local_path)
                VALUES (?, ?, ?)
                ON CONFLICT(device_name, project) DO UPDATE SET
                    local_path    = excluded.local_path,
                    last_activity = datetime('now')
                """,
                (name, data.project, data.local_path),
            )
            db.commit()
            row = db.execute(
                "SELECT * FROM device_projects WHERE device_name = ? AND project = ?",
                (name, data.project),
            ).fetchone()
        return dict(row)

    @router.delete("/{name}/projects/{project}", status_code=status.HTTP_200_OK)
    async def delete_device_project(name: str, project: str):
        with connect(db_path) as db:
            cursor = db.execute(
                "DELETE FROM device_projects WHERE device_name = ? AND project = ?",
                (name, project),
            )
            db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="device_project not found")
        return {"device": name, "project": project, "deleted": True}

    return router
