"""Devices + device_projects tests."""
from __future__ import annotations


def _register(client, auth_headers, **overrides):
    payload = {"name": "klipper", "platform": "linux"}
    payload.update(overrides)
    r = client.post("/devices", headers=auth_headers, json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# ----- auth -----

def test_devices_auth_required(client):
    r = client.get("/devices")
    assert r.status_code == 401


def test_devices_auth_disabled_mode_ok(client_noauth):
    r = client_noauth.get("/devices")
    assert r.status_code == 200
    assert r.json() == []


# ----- register / upsert -----

def test_register_creates_device(client, auth_headers):
    d = _register(client, auth_headers, hostname="klipper.local", mesh_ip="100.64.0.1")
    assert d["name"] == "klipper"
    assert d["platform"] == "linux"
    assert d["hostname"] == "klipper.local"
    assert d["mesh_ip"] == "100.64.0.1"
    assert d["last_seen"] and d["created_at"]


def test_register_rejects_missing_fields(client, auth_headers):
    r = client.post("/devices", headers=auth_headers, json={"name": "x"})
    assert r.status_code == 422


def test_register_upserts_by_name(client, auth_headers):
    first = _register(client, auth_headers, hostname="old")
    second = _register(client, auth_headers, hostname="new", notes="updated")
    assert first["name"] == second["name"]
    assert second["hostname"] == "new"
    assert second["notes"] == "updated"
    # Single row only after upsert.
    r = client.get("/devices", headers=auth_headers)
    assert len(r.json()) == 1


# ----- list / get -----

def test_list_orders_by_last_seen_desc(client, auth_headers):
    _register(client, auth_headers, name="dev-a", platform="linux")
    _register(client, auth_headers, name="dev-b", platform="linux")
    # Touch dev-a so it floats to top.
    client.post("/devices/dev-a/ping", headers=auth_headers)
    rows = client.get("/devices", headers=auth_headers).json()
    assert [r["name"] for r in rows] == ["dev-a", "dev-b"]


def test_get_by_name(client, auth_headers):
    _register(client, auth_headers)
    r = client.get("/devices/klipper", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["name"] == "klipper"


def test_get_missing_returns_404(client, auth_headers):
    r = client.get("/devices/nope", headers=auth_headers)
    assert r.status_code == 404


# ----- ping / delete -----

def test_ping_bumps_last_seen(client, auth_headers):
    d = _register(client, auth_headers)
    r = client.post("/devices/klipper/ping", headers=auth_headers)
    assert r.status_code == 200
    # last_seen is at least monotonic at second resolution
    assert r.json()["last_seen"] >= d["last_seen"]


def test_ping_missing_returns_404(client, auth_headers):
    r = client.post("/devices/nope/ping", headers=auth_headers)
    assert r.status_code == 404


def test_delete_removes_device(client, auth_headers):
    _register(client, auth_headers)
    r = client.delete("/devices/klipper", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert client.get("/devices/klipper", headers=auth_headers).status_code == 404


def test_delete_missing_returns_404(client, auth_headers):
    r = client.delete("/devices/nope", headers=auth_headers)
    assert r.status_code == 404


# ----- device_projects -----

def test_projects_require_device(client, auth_headers):
    r = client.get("/devices/nope/projects", headers=auth_headers)
    assert r.status_code == 404
    r2 = client.post(
        "/devices/nope/projects",
        headers=auth_headers,
        json={"project": "x"},
    )
    assert r2.status_code == 404


def test_upsert_project_creates_then_updates(client, auth_headers):
    _register(client, auth_headers)
    r1 = client.post(
        "/devices/klipper/projects",
        headers=auth_headers,
        json={"project": "polymem", "local_path": "/opt/polymem"},
    )
    assert r1.status_code == 200
    assert r1.json()["local_path"] == "/opt/polymem"

    r2 = client.post(
        "/devices/klipper/projects",
        headers=auth_headers,
        json={"project": "polymem", "local_path": "/srv/polymem"},
    )
    assert r2.status_code == 200
    assert r2.json()["local_path"] == "/srv/polymem"

    rows = client.get("/devices/klipper/projects", headers=auth_headers).json()
    assert len(rows) == 1


def test_list_projects_for_device(client, auth_headers):
    _register(client, auth_headers)
    client.post(
        "/devices/klipper/projects", headers=auth_headers, json={"project": "p1"}
    )
    client.post(
        "/devices/klipper/projects", headers=auth_headers, json={"project": "p2"}
    )
    rows = client.get("/devices/klipper/projects", headers=auth_headers).json()
    assert {r["project"] for r in rows} == {"p1", "p2"}


def test_delete_project(client, auth_headers):
    _register(client, auth_headers)
    client.post(
        "/devices/klipper/projects", headers=auth_headers, json={"project": "p1"}
    )
    r = client.delete("/devices/klipper/projects/p1", headers=auth_headers)
    assert r.status_code == 200
    assert client.get("/devices/klipper/projects", headers=auth_headers).json() == []


def test_delete_missing_project_returns_404(client, auth_headers):
    _register(client, auth_headers)
    r = client.delete("/devices/klipper/projects/never", headers=auth_headers)
    assert r.status_code == 404
