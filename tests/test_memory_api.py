"""Tests for the central memory API (app/api/memory.py).

The memory router uses its own SQLite DB with a hardcoded path. Tests
patch DB_PATH to a per-test tmp file, build the schema, and exercise
every endpoint via the shared FastAPI test client.
"""

from __future__ import annotations

import sqlite3

import pytest

MEMORY_SCHEMA = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK(type IN ('user','feedback','project','reference')),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    active INTEGER DEFAULT 1,
    source_device TEXT DEFAULT 'klipper',
    last_read_at TEXT,
    read_count INTEGER DEFAULT 0,
    rationale TEXT
);

CREATE TABLE devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    platform TEXT NOT NULL,
    hostname TEXT,
    ip TEXT,
    tailscale_ip TEXT,
    os_version TEXT,
    claude_version TEXT,
    notes TEXT,
    last_seen TEXT DEFAULT (datetime('now')),
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_num INTEGER,
    date TEXT NOT NULL,
    summary TEXT NOT NULL,
    tasks_completed TEXT,
    files_changed TEXT,
    bugs_found TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    device_id INTEGER REFERENCES devices(id),
    platform TEXT DEFAULT 'linux',
    device_name TEXT DEFAULT 'klipper'
);

CREATE TABLE tasks_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(id),
    project TEXT,
    task TEXT NOT NULL,
    status TEXT DEFAULT 'completed',
    files_changed TEXT,
    details TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    device_name TEXT DEFAULT 'klipper',
    rationale TEXT
);

CREATE TABLE notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_device TEXT NOT NULL,
    to_device TEXT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    read INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE device_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_name TEXT NOT NULL,
    project TEXT NOT NULL,
    local_path TEXT,
    last_activity TEXT DEFAULT (datetime('now')),
    UNIQUE(device_name, project)
);

CREATE TABLE discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(id),
    project TEXT,
    type TEXT CHECK(type IN ('bug','fix','learning','config','workaround','architecture','plan')),
    title TEXT NOT NULL,
    details TEXT,
    resolved INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    device_name TEXT DEFAULT 'klipper',
    status TEXT DEFAULT 'active' CHECK(status IN ('active','completed','obsolete','superseded')),
    last_read_at TEXT,
    read_count INTEGER DEFAULT 0,
    rationale TEXT
);

CREATE UNIQUE INDEX idx_discoveries_unique_active
    ON discoveries(project, type, title)
    WHERE status='active';

CREATE VIRTUAL TABLE discoveries_fts USING fts5(
    title, details, content=discoveries, content_rowid=id
);

CREATE TABLE task_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_by TEXT NOT NULL,
    target_device TEXT,
    command TEXT NOT NULL,
    rationale TEXT,
    status TEXT DEFAULT 'pending',
    claimed_by TEXT,
    claimed_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    stdout TEXT,
    stderr TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


@pytest.fixture
def memory_db(tmp_path, monkeypatch):
    """Per-test memory SQLite with full schema. Patches DB_PATH and disables
    the X-Memory-Key check by clearing MEMORY_API_KEY."""
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(MEMORY_SCHEMA)
    conn.commit()
    conn.close()

    from app.api import memory as mem_module

    monkeypatch.setattr(mem_module, "DB_PATH", str(db_path))
    monkeypatch.setattr(mem_module, "MEMORY_API_KEY", "")
    return db_path


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


async def test_register_and_list_device(client, memory_db):
    resp = await client.post(
        "/api/v1/memory/devices",
        json={"name": "klipper", "platform": "linux", "hostname": "klipper", "tailscale_ip": "100.x.y.z"},
    )
    assert resp.status_code == 200
    assert resp.json()["device"] == "klipper"

    resp = await client.get("/api/v1/memory/devices")
    assert resp.status_code == 200
    devices = resp.json()
    assert len(devices) == 1
    assert devices[0]["name"] == "klipper"


async def test_register_device_upsert(client, memory_db):
    """Re-registering the same device updates fields, doesn't duplicate."""
    await client.post("/api/v1/memory/devices", json={"name": "k", "platform": "linux"})
    await client.post("/api/v1/memory/devices", json={"name": "k", "platform": "linux", "hostname": "newhost"})
    resp = await client.get("/api/v1/memory/devices")
    devices = resp.json()
    assert len(devices) == 1
    assert devices[0]["hostname"] == "newhost"


async def test_ping_device(client, memory_db):
    await client.post("/api/v1/memory/devices", json={"name": "k", "platform": "linux"})
    resp = await client.post("/api/v1/memory/devices/k/ping")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------


async def test_memory_crud(client, memory_db):
    # Create
    resp = await client.post(
        "/api/v1/memory/memories",
        json={"type": "user", "name": "test_user", "description": "desc", "content": "content"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "created"
    mid = body["id"]

    # Get (and read tracking — increment happens AFTER row is fetched,
    # so the second GET sees the bumped count)
    resp = await client.get(f"/api/v1/memory/memories/{mid}")
    assert resp.status_code == 200
    resp = await client.get(f"/api/v1/memory/memories/{mid}")
    assert resp.json()["read_count"] >= 1

    # List
    resp = await client.get("/api/v1/memory/memories")
    assert resp.status_code == 200
    items = resp.json()
    assert any(m["id"] == mid for m in items)

    # List filtered by type
    resp = await client.get("/api/v1/memory/memories?type=user")
    assert len(resp.json()) == 1

    # List with search
    resp = await client.get("/api/v1/memory/memories?search=content")
    assert len(resp.json()) == 1

    # Update
    resp = await client.put(f"/api/v1/memory/memories/{mid}", json={"description": "updated desc"})
    assert resp.status_code == 200

    # Update with no fields raises 400
    resp = await client.put(f"/api/v1/memory/memories/{mid}", json={})
    assert resp.status_code == 400

    # Delete (deactivate)
    resp = await client.delete(f"/api/v1/memory/memories/{mid}")
    assert resp.status_code == 200

    # No longer in active list
    resp = await client.get("/api/v1/memory/memories")
    assert all(m["id"] != mid for m in resp.json())


async def test_memory_create_duplicate_updates(client, memory_db):
    payload = {"type": "user", "name": "dup_test", "description": "v1", "content": "c1"}
    r1 = await client.post("/api/v1/memory/memories", json=payload)
    assert r1.json()["status"] == "created"

    payload["description"] = "v2"
    r2 = await client.post("/api/v1/memory/memories", json=payload)
    assert r2.json()["status"] == "updated_existing"
    assert r2.json()["id"] == r1.json()["id"]


async def test_memory_get_404(client, memory_db):
    resp = await client.get("/api/v1/memory/memories/9999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def test_session_crud(client, memory_db):
    # Register a device first so device_id is populated
    await client.post("/api/v1/memory/devices", json={"name": "klipper", "platform": "linux"})

    resp = await client.post(
        "/api/v1/memory/sessions",
        json={"device_name": "klipper", "summary": "test session", "tasks_completed": ["a", "b"]},
    )
    assert resp.status_code == 200
    sid = resp.json()["id"]
    assert resp.json()["session_num"] == 1

    # Auto-increment session_num on next session
    resp = await client.post(
        "/api/v1/memory/sessions",
        json={"device_name": "klipper", "summary": "next session"},
    )
    assert resp.json()["session_num"] == 2

    # Get
    resp = await client.get(f"/api/v1/memory/sessions/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == "test session"
    assert "tasks" in body
    assert "discoveries" in body

    # List
    resp = await client.get("/api/v1/memory/sessions")
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    # Filter by device
    resp = await client.get("/api/v1/memory/sessions?device=klipper")
    assert len(resp.json()) == 2

    # Filter by platform
    resp = await client.get("/api/v1/memory/sessions?platform=linux")
    assert len(resp.json()) == 2


async def test_session_unknown_device_uses_default(client, memory_db):
    """If device not in devices table, session is created with platform='unknown'."""
    resp = await client.post(
        "/api/v1/memory/sessions",
        json={"device_name": "ghost-device", "summary": "orphan"},
    )
    assert resp.status_code == 200


async def test_session_get_404(client, memory_db):
    resp = await client.get("/api/v1/memory/sessions/9999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tasks log
# ---------------------------------------------------------------------------


async def test_task_log_crud(client, memory_db):
    resp = await client.post(
        "/api/v1/memory/tasks",
        json={"project": "linux-ai-server", "task": "refactor x", "rationale": "because"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "created"

    # Duplicate returns existing
    resp = await client.post(
        "/api/v1/memory/tasks",
        json={"project": "linux-ai-server", "task": "refactor x"},
    )
    assert resp.json()["status"] == "already_exists"

    # List
    resp = await client.get("/api/v1/memory/tasks")
    assert len(resp.json()) == 1

    # Filter
    resp = await client.get("/api/v1/memory/tasks?project=linux-ai-server")
    assert len(resp.json()) == 1
    resp = await client.get("/api/v1/memory/tasks?device=klipper")
    assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# Discoveries
# ---------------------------------------------------------------------------


async def test_discovery_crud_and_lifecycle(client, memory_db):
    # Create
    resp = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "p1", "type": "bug", "title": "broken thing", "details": "stack trace"},
    )
    assert resp.status_code == 200
    did = resp.json()["id"]

    # Duplicate returns same id (active status only)
    resp = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "p1", "type": "bug", "title": "broken thing", "details": "more details"},
    )
    assert resp.json()["status"] == "already_exists"
    assert resp.json()["id"] == did

    # Get with read tracking — bump is post-fetch, so check on second call
    await client.get(f"/api/v1/memory/discoveries/{did}")
    resp = await client.get(f"/api/v1/memory/discoveries/{did}")
    assert resp.json()["read_count"] >= 1

    # List filters
    resp = await client.get("/api/v1/memory/discoveries?project=p1")
    assert len(resp.json()) == 1
    resp = await client.get("/api/v1/memory/discoveries?type=bug")
    assert len(resp.json()) == 1
    resp = await client.get("/api/v1/memory/discoveries?status=active")
    assert len(resp.json()) == 1

    # Update — change details
    resp = await client.put(f"/api/v1/memory/discoveries/{did}", json={"details": "new details"})
    assert resp.status_code == 200

    # Empty update -> 400
    resp = await client.put(f"/api/v1/memory/discoveries/{did}", json={})
    assert resp.status_code == 400

    # Resolve via PUT /resolve
    resp = await client.put(f"/api/v1/memory/discoveries/{did}/resolve")
    assert resp.status_code == 200
    resp = await client.get(f"/api/v1/memory/discoveries/{did}")
    assert resp.json()["status"] == "completed"

    # Update status to obsolete
    resp = await client.put(f"/api/v1/memory/discoveries/{did}", json={"status": "obsolete"})
    assert resp.status_code == 200


async def test_discovery_validation_rejects_short_title(client, memory_db):
    resp = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "p", "type": "bug", "title": "x"},
    )
    assert resp.status_code == 422


async def test_discovery_validation_rejects_trash_titles(client, memory_db):
    resp = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "p", "type": "bug", "title": "test"},
    )
    assert resp.status_code == 422


async def test_discovery_validation_rejects_invalid_type(client, memory_db):
    resp = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "p", "type": "totally-fake-type", "title": "valid title"},
    )
    assert resp.status_code == 422


async def test_discovery_invalid_status_update_rejected(client, memory_db):
    resp = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "p", "type": "bug", "title": "title here"},
    )
    did = resp.json()["id"]
    resp = await client.put(f"/api/v1/memory/discoveries/{did}", json={"status": "bogus"})
    assert resp.status_code == 422


async def test_discovery_get_404(client, memory_db):
    resp = await client.get("/api/v1/memory/discoveries/9999")
    assert resp.status_code == 404


async def test_discoveries_by_type(client, memory_db):
    await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "p1", "type": "architecture", "title": "use sqlite for X"},
    )
    await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "p2", "type": "architecture", "title": "use postgres for Y"},
    )
    resp = await client.get("/api/v1/memory/discoveries/by-type/architecture")
    assert len(resp.json()) == 2

    resp = await client.get("/api/v1/memory/discoveries/by-type/architecture?project=p1")
    assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


async def test_projects_summary_and_detail(client, memory_db):
    # Seed: bug + plan + task across two projects
    await client.post("/api/v1/memory/discoveries", json={"project": "alpha", "type": "bug", "title": "bug one"})
    await client.post("/api/v1/memory/discoveries", json={"project": "alpha", "type": "architecture", "title": "alpha arch"})
    await client.post("/api/v1/memory/discoveries", json={"project": "alpha", "type": "plan", "title": "alpha plan"})
    await client.post("/api/v1/memory/discoveries", json={"project": "beta", "type": "fix", "title": "beta fix"})
    await client.post("/api/v1/memory/tasks", json={"project": "alpha", "task": "alpha task"})
    await client.post("/api/v1/memory/tasks", json={"project": "gamma", "task": "gamma task"})

    resp = await client.get("/api/v1/memory/projects")
    assert resp.status_code == 200
    by_name = {p["name"]: p for p in resp.json()}
    assert "alpha" in by_name
    assert "beta" in by_name
    assert "gamma" in by_name
    assert by_name["alpha"]["open_bugs"] == 1
    assert by_name["alpha"]["architecture"] == 1
    assert by_name["alpha"]["active_plans"] == 1
    assert by_name["alpha"]["tasks"] == 1
    assert by_name["alpha"]["health"] > 0

    resp = await client.get("/api/v1/memory/projects/alpha")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["project"] == "alpha"
    assert detail["total_discoveries"] == 3
    assert detail["total_tasks"] == 1


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


async def test_notes_crud(client, memory_db):
    resp = await client.post(
        "/api/v1/memory/notes",
        json={"from_device": "klipper", "title": "hello", "content": "world"},
    )
    assert resp.status_code == 200
    nid = resp.json()["id"]

    resp = await client.get("/api/v1/memory/notes")
    assert len(resp.json()) == 1

    resp = await client.get("/api/v1/memory/notes?unread_only=true")
    assert len(resp.json()) == 1

    resp = await client.get("/api/v1/memory/notes?device=klipper")
    assert len(resp.json()) == 1

    resp = await client.put(f"/api/v1/memory/notes/{nid}/read")
    assert resp.status_code == 200

    resp = await client.get("/api/v1/memory/notes?unread_only=true")
    assert len(resp.json()) == 0


# ---------------------------------------------------------------------------
# Device-projects
# ---------------------------------------------------------------------------


async def test_device_projects_upsert(client, memory_db):
    resp = await client.post(
        "/api/v1/memory/device-projects",
        json={"device_name": "klipper", "project": "linux-ai-server", "local_path": "/opt/x"},
    )
    assert resp.status_code == 200

    # Upsert (same device+project) updates path
    resp = await client.post(
        "/api/v1/memory/device-projects",
        json={"device_name": "klipper", "project": "linux-ai-server", "local_path": "/opt/y"},
    )
    assert resp.status_code == 200

    resp = await client.get("/api/v1/memory/device-projects")
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["local_path"] == "/opt/y"

    resp = await client.get("/api/v1/memory/device-projects?device=klipper")
    assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def test_search_hits_all_tables(client, memory_db):
    # Seed a discovery (FTS-indexed via _sync_fts)
    await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "p", "type": "bug", "title": "needle in haystack", "details": "details with needle"},
    )
    # Seed memory + session + task with the same keyword
    await client.post(
        "/api/v1/memory/memories",
        json={"type": "user", "name": "needle_mem", "description": "desc needle", "content": "needle present"},
    )
    await client.post(
        "/api/v1/memory/sessions",
        json={"device_name": "klipper", "summary": "session contains needle"},
    )
    await client.post(
        "/api/v1/memory/tasks",
        json={"project": "p", "task": "find needle"},
    )

    resp = await client.get("/api/v1/memory/search?q=needle")
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "needle"
    assert body["total"] >= 4
    # at least one hit per group
    assert len(body["results"]["memories"]) >= 1
    assert len(body["results"]["sessions"]) >= 1
    assert len(body["results"]["tasks"]) >= 1


async def test_search_minimum_query_length(client, memory_db):
    resp = await client.get("/api/v1/memory/search?q=a")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Health & maintenance
# ---------------------------------------------------------------------------


async def test_memory_health(client, memory_db):
    resp = await client.get("/api/v1/memory/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "total_discoveries" in body
    assert "never_read" in body
    assert "recommendation" in body


async def test_archive_stale_obsoletes_old_unread(client, memory_db):
    # Seed an "old" non-bug active discovery via direct sqlite (so created_at is old)
    conn = sqlite3.connect(memory_db)
    conn.execute(
        "INSERT INTO discoveries (project, type, title, status, created_at, read_count) "
        "VALUES (?, ?, ?, 'active', datetime('now', '-200 days'), 0)",
        ("p", "learning", "stale learning"),
    )
    conn.commit()
    conn.close()

    resp = await client.post("/api/v1/memory/maintenance/archive-stale?days=90")
    assert resp.status_code == 200
    assert resp.json()["archived"] == 1


async def test_archive_stale_excludes_bugs(client, memory_db):
    """Bugs are never auto-archived, even if old + unread."""
    conn = sqlite3.connect(memory_db)
    conn.execute(
        "INSERT INTO discoveries (project, type, title, status, created_at, read_count) "
        "VALUES (?, ?, ?, 'active', datetime('now', '-365 days'), 0)",
        ("p", "bug", "very old bug"),
    )
    conn.commit()
    conn.close()

    resp = await client.post("/api/v1/memory/maintenance/archive-stale?days=30")
    assert resp.json()["archived"] == 0


# ---------------------------------------------------------------------------
# Task queue
# ---------------------------------------------------------------------------


async def test_task_queue_lifecycle(client, memory_db):
    resp = await client.post(
        "/api/v1/memory/queue",
        json={"requested_by": "klipper", "command": "echo hi", "rationale": "test"},
    )
    assert resp.status_code == 200
    tid = resp.json()["id"]

    # List
    resp = await client.get("/api/v1/memory/queue")
    assert len(resp.json()) == 1

    # Filtered list
    resp = await client.get("/api/v1/memory/queue?status=pending")
    assert len(resp.json()) == 1

    # Claim
    resp = await client.put(f"/api/v1/memory/queue/{tid}/claim", json={"claimed_by": "worker-1"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "claimed"

    # Re-claim same task -> 409
    resp = await client.put(f"/api/v1/memory/queue/{tid}/claim", json={"claimed_by": "worker-2"})
    assert resp.status_code == 409

    # Result
    resp = await client.put(
        f"/api/v1/memory/queue/{tid}/result",
        json={"exit_code": 0, "stdout": "hi", "stderr": "", "status": "completed"},
    )
    assert resp.status_code == 200

    # Re-write result on completed task -> 409
    resp = await client.put(
        f"/api/v1/memory/queue/{tid}/result",
        json={"exit_code": 0, "status": "completed"},
    )
    assert resp.status_code == 409


async def test_task_queue_target_filter(client, memory_db):
    await client.post(
        "/api/v1/memory/queue",
        json={"requested_by": "klipper", "command": "x", "target_device": "windows-laptop"},
    )
    resp = await client.get("/api/v1/memory/queue?target_device=windows-laptop")
    assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


async def test_dashboard(client, memory_db):
    # Seed a bug, plan, memory, note, session, task
    await client.post("/api/v1/memory/devices", json={"name": "klipper", "platform": "linux"})
    await client.post("/api/v1/memory/discoveries", json={"project": "p", "type": "bug", "title": "open issue"})
    await client.post("/api/v1/memory/discoveries", json={"project": "p", "type": "architecture", "title": "arch decision"})
    await client.post(
        "/api/v1/memory/memories",
        json={"type": "user", "name": "u", "description": "d", "content": "c"},
    )
    await client.post("/api/v1/memory/notes", json={"from_device": "klipper", "title": "t", "content": "c"})
    await client.post("/api/v1/memory/sessions", json={"device_name": "klipper", "summary": "s"})
    await client.post("/api/v1/memory/tasks", json={"project": "p", "task": "task1"})

    resp = await client.get("/api/v1/memory/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stats"]["open_bugs"] >= 1
    assert body["stats"]["memories"] >= 1
    assert body["stats"]["sessions"] >= 1
    assert body["stats"]["tasks"] >= 1
    assert body["stats"]["unread_notes"] >= 1
    assert any(d["name"] == "klipper" for d in body["devices"])
    assert any(b["title"] == "open issue" for b in body["open_bugs"])


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------


async def test_onboard_prompt(client, memory_db):
    await client.post("/api/v1/memory/devices", json={"name": "android", "platform": "android"})
    await client.post("/api/v1/memory/sessions", json={"device_name": "android", "summary": "s1"})
    await client.post("/api/v1/memory/discoveries", json={"project": "p", "type": "bug", "title": "issue one"})
    await client.post("/api/v1/memory/notes", json={"from_device": "klipper", "to_device": "android", "title": "t", "content": "msg"})
    await client.post(
        "/api/v1/memory/memories",
        json={"type": "user", "name": "u", "description": "d", "content": "c"},
    )

    resp = await client.get("/api/v1/memory/onboard/android")
    assert resp.status_code == 200
    assert resp.json()["device"] == "android"
    assert "Merkezi Hafıza" in resp.json()["prompt"]


async def test_onboard_unknown_device_404(client, memory_db):
    resp = await client.get("/api/v1/memory/onboard/no-such-device")
    assert resp.status_code == 404


async def test_onboard_raw_returns_plain_text(client, memory_db):
    await client.post("/api/v1/memory/devices", json={"name": "k", "platform": "linux"})
    resp = await client.get("/api/v1/memory/onboard/k/raw")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")


async def test_onboard_project_scan_prompt(client, memory_db):
    await client.post("/api/v1/memory/devices", json={"name": "k", "platform": "linux"})
    await client.post("/api/v1/memory/discoveries", json={"project": "alpha", "type": "bug", "title": "bug here"})
    resp = await client.get("/api/v1/memory/onboard/k/project-scan")
    assert resp.status_code == 200
    assert "alpha" in resp.text


async def test_onboard_project_scan_unknown_device_404(client, memory_db):
    resp = await client.get("/api/v1/memory/onboard/no-device/project-scan")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth (X-Memory-Key)
# ---------------------------------------------------------------------------


async def test_protected_router_requires_key_when_set(client, memory_db, monkeypatch):
    """When MEMORY_API_KEY is non-empty, missing/wrong header → 401."""
    from app.api import memory as mem_module

    monkeypatch.setattr(mem_module, "MEMORY_API_KEY", "secret-key")

    # No header → 401
    resp = await client.get("/api/v1/memory/devices")
    assert resp.status_code == 401

    # Wrong header → 401
    resp = await client.get("/api/v1/memory/devices", headers={"X-Memory-Key": "wrong"})
    assert resp.status_code == 401

    # Correct header → 200
    resp = await client.get("/api/v1/memory/devices", headers={"X-Memory-Key": "secret-key"})
    assert resp.status_code == 200
