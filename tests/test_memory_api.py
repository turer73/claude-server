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
    read_by TEXT DEFAULT '',
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

"""


@pytest.fixture
def memory_db(tmp_path, monkeypatch):
    """Per-test memory SQLite with full schema. Patches DB_PATH ve MEMORY_API_KEY'i
    gerçek test-key'e set eder (fail-closed güvenlik fix; client X-Memory-Key gönderir).
    Eski 'MEMORY_API_KEY=\"\"' fail-open'ı test ediyordu — kaldırıldı."""
    from tests.conftest import TEST_MEMORY_KEY

    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(MEMORY_SCHEMA)
    conn.commit()
    conn.close()

    from app.api import memory as mem_module

    monkeypatch.setattr(mem_module, "DB_PATH", str(db_path))
    monkeypatch.setattr(mem_module, "MEMORY_API_KEY", TEST_MEMORY_KEY)
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


async def test_task_log_patch_status(client, memory_db):
    resp = await client.post(
        "/api/v1/memory/tasks",
        json={"project": "linux-ai-server", "task": "patch me"},
    )
    task_id = resp.json()["id"]

    resp = await client.patch(f"/api/v1/memory/tasks/{task_id}", json={"status": "completed"})
    assert resp.status_code == 200
    assert resp.json()["new_status"] == "completed"

    # Sadece rationale — status korunur
    resp = await client.patch(f"/api/v1/memory/tasks/{task_id}", json={"rationale": "artık gereksiz"})
    assert resp.status_code == 200
    assert resp.json()["new_status"] == "completed"

    rows = await client.get("/api/v1/memory/tasks")
    assert any(r["id"] == task_id and r["status"] == "completed" and r["rationale"] == "artık gereksiz" for r in rows.json())


async def test_task_log_patch_validation(client, memory_db):
    # Boş gövde → 400
    resp = await client.patch("/api/v1/memory/tasks/1", json={})
    assert resp.status_code == 400

    # Olmayan task → 404
    resp = await client.patch("/api/v1/memory/tasks/99999", json={"status": "completed"})
    assert resp.status_code == 404

    # Geçersiz status → 422 (Pydantic Literal)
    resp = await client.patch("/api/v1/memory/tasks/1", json={"status": "bogus"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Discoveries
# ---------------------------------------------------------------------------


async def test_discovery_skip_dedup_bypasses_semantic(client, memory_db, monkeypatch):
    # Codex#176: skip_dedup=True → semantic-dedup ATLANIR (recurring-log; ardışık benzer raporlar
    # cosine≥0.90 ile yanlış-merge olmasın). semantic_dedup çağrılmamalı.
    import app.api.memory.signal_quality as sq

    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        return {"operation": "ADD"}

    monkeypatch.setenv("SIGNAL_SEMANTIC_DEDUP", "1")  # dedup açık olsa BİLE
    monkeypatch.setattr(sq, "semantic_dedup", _boom)
    resp = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "p1", "type": "learning", "title": "Haftalık Rapor — W25", "skip_dedup": True},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "created"
    assert called["n"] == 0  # skip_dedup → semantic_dedup HİÇ çağrılmadı


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

    # Update — rationale persist edilmeli (eski silent-fail bug: model'de alan yoktu, 200 dönüp yazmazdı)
    resp = await client.put(f"/api/v1/memory/discoveries/{did}", json={"rationale": "triage: FP, obsolete"})
    assert resp.status_code == 200
    assert (await client.get(f"/api/v1/memory/discoveries/{did}")).json()["rationale"] == "triage: FP, obsolete"

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


async def test_notes_per_device_read(client, memory_db):
    """#647: device-param ile okundu PER-DEVICE — bir device okuyunca diğerleri için
    okunmamış KALIR (eski global read-flag hatası düzeldi)."""
    resp = await client.post(
        "/api/v1/memory/notes",
        json={"from_device": "surer", "title": "broadcast", "content": "herkese"},
    )
    nid = resp.json()["id"]

    # klipper okudu (device-param) → klipper için okundu, opencode için OKUNMAMIŞ kalmalı
    resp = await client.put(f"/api/v1/memory/notes/{nid}/read?device=klipper")
    assert resp.status_code == 200
    assert resp.json()["read_by"] == ["klipper"]

    resp = await client.get("/api/v1/memory/notes?device=klipper&unread_only=true")
    assert len(resp.json()) == 0  # klipper için okundu
    resp = await client.get("/api/v1/memory/notes?device=opencode&unread_only=true")
    assert len(resp.json()) == 1  # opencode HÂLÂ görür (global-flag hatası yok)

    # opencode da okudu → ikisi de read_by'da, ikisi için de okunmamış kalmaz
    resp = await client.put(f"/api/v1/memory/notes/{nid}/read?device=opencode")
    assert set(resp.json()["read_by"]) == {"klipper", "opencode"}
    resp = await client.get("/api/v1/memory/notes?device=opencode&unread_only=true")
    assert len(resp.json()) == 0

    # idempotent: aynı device tekrar → çift eklenmez
    resp = await client.put(f"/api/v1/memory/notes/{nid}/read?device=klipper")
    assert resp.json()["read_by"].count("klipper") == 1


async def test_notes_legacy_read_marks_all_devices(client, memory_db):
    """Geri-uyum: device'sız mark-read (legacy) → tüm device'lar için okundu (read=1)."""
    resp = await client.post(
        "/api/v1/memory/notes",
        json={"from_device": "surer", "title": "legacy", "content": "x"},
    )
    nid = resp.json()["id"]
    await client.put(f"/api/v1/memory/notes/{nid}/read")  # device YOK → legacy global
    for dev in ("klipper", "opencode", "surer"):
        resp = await client.get(f"/api/v1/memory/notes?device={dev}&unread_only=true")
        assert len(resp.json()) == 0  # legacy read=1 herkes için okundu


async def test_notes_mark_read_missing_device_404(client, memory_db):
    resp = await client.put("/api/v1/memory/notes/999999/read?device=klipper")
    assert resp.status_code == 404


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


async def test_session_context_returns_structured_json(client, memory_db):
    # Faz 2 (to_thread offload): davranış korunmalı + bu endpoint daha önce testsizdi.
    await client.post("/api/v1/memory/devices", json={"name": "k", "platform": "linux"})
    await client.post("/api/v1/memory/sessions", json={"device_name": "k", "summary": "son oturum"})
    await client.post("/api/v1/memory/discoveries", json={"project": "alpha", "type": "bug", "title": "açık bug"})
    resp = await client.get("/api/v1/memory/onboard/k/session-context")
    assert resp.status_code == 200
    body = resp.json()
    assert body["device"] == "k"
    assert body["platform"] == "linux"
    assert "recent_sessions" in body
    assert "active_bugs" in body
    assert "token_budget" in body
    assert any(b["title"] == "açık bug" for b in body["active_bugs"])


async def test_session_context_unknown_device_404(client, memory_db):
    resp = await client.get("/api/v1/memory/onboard/ghost-device/session-context")
    assert resp.status_code == 404


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

    # No header → 401. (conftest client GLOBAL X-Memory-Key default gönderir; bu testte
    # GERÇEK no-header path'i test etmek için kaldır — Codex #27: global header missing-
    # header testini maskelemesin.)
    client.headers.pop("X-Memory-Key", None)
    resp = await client.get("/api/v1/memory/devices")
    assert resp.status_code == 401

    # Wrong header → 401
    resp = await client.get("/api/v1/memory/devices", headers={"X-Memory-Key": "wrong"})
    assert resp.status_code == 401

    # Correct header → 200
    resp = await client.get("/api/v1/memory/devices", headers={"X-Memory-Key": "secret-key"})
    assert resp.status_code == 200


async def test_onboard_endpoints_require_key_when_set(client, memory_db, monkeypatch):
    """Regression: onboard responses embed MEMORY_API_KEY in plaintext,
    so they MUST also require it on the request. Previously these were on a
    public_router with no dependencies, leaking the key to anyone on the LAN."""
    from app.api import memory as mem_module

    monkeypatch.setattr(mem_module, "MEMORY_API_KEY", "secret-key")
    # Seed a device so the endpoints would otherwise return 200
    await client.post(
        "/api/v1/memory/devices",
        json={"name": "k", "platform": "linux"},
        headers={"X-Memory-Key": "secret-key"},
    )

    for path in ("/api/v1/memory/onboard/k", "/api/v1/memory/onboard/k/raw", "/api/v1/memory/onboard/k/project-scan"):
        resp = await client.get(path)
        assert resp.status_code == 401, f"{path} must reject unauthenticated requests"
        resp = await client.get(path, headers={"X-Memory-Key": "wrong"})
        assert resp.status_code == 401, f"{path} must reject wrong key"
        resp = await client.get(path, headers={"X-Memory-Key": "secret-key"})
        assert resp.status_code == 200, f"{path} must accept correct key"


# ---------------------------------------------------------------------------
# LIVESYS-MEMSYN: surface + world-model
# ---------------------------------------------------------------------------


async def test_memory_surface_tolerates_no_merged_column(client, memory_db):
    # merged_into kolonu YOK (sentez henüz çalışmadı) → tüm aktifler yüzey
    con = sqlite3.connect(memory_db)
    con.execute("INSERT INTO memories (type,name,description,content) VALUES ('project','a','d','c')")
    con.execute("INSERT INTO memories (type,name,description,content) VALUES ('feedback','b','d','c')")
    con.commit()
    con.close()
    r = await client.get("/api/v1/memory/surface")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2  # P0-c: {total,count,limit,offset,items}
    assert len(body["items"]) == 2
    wm = (await client.get("/api/v1/memory/world-model")).json()
    assert wm["surface_total"] == 2
    assert wm["synthesized"] is False


async def test_memory_surface_limit_and_offset(client, memory_db):
    # P0-c: limit korpus-bombasını sınırlar; total tam-sayı verir; offset sayfalar
    con = sqlite3.connect(memory_db)
    for i in range(5):
        con.execute("INSERT INTO memories (type,name,description,content) VALUES ('project',?,?,?)", (f"m{i}", "d", "c"))
    con.commit()
    con.close()
    r = (await client.get("/api/v1/memory/surface?limit=2")).json()
    assert r["total"] == 5  # tam-sayı (limit'ten bağımsız)
    assert r["count"] == 2  # yalnız 2 döndü (token-limit)
    assert len(r["items"]) == 2
    r2 = (await client.get("/api/v1/memory/surface?limit=2&offset=4")).json()
    assert r2["count"] == 1  # son sayfa
    # limit sınırı: max 500 (validation)
    assert (await client.get("/api/v1/memory/surface?limit=999")).status_code == 422


async def test_memory_surface_excludes_merged(client, memory_db):
    # merged_into uygulanmış: canonical yüzeyde, merged-dup yüzeyde DEĞİL
    con = sqlite3.connect(memory_db)
    con.execute("ALTER TABLE memories ADD COLUMN merged_into INTEGER")
    con.execute("INSERT INTO memories (id,type,name,description,content,active) VALUES (1,'project','canon','d','c',1)")
    con.execute("INSERT INTO memories (id,type,name,description,content,active,merged_into) VALUES (2,'project','dup','d','c',0,1)")
    con.commit()
    con.close()
    names = [m["name"] for m in (await client.get("/api/v1/memory/surface")).json()["items"]]
    assert "canon" in names
    assert "dup" not in names
    wm = (await client.get("/api/v1/memory/world-model")).json()
    assert wm["synthesized"] is True
    assert wm["merged_archived"] == 1
    assert wm["surface_total"] == 1


async def test_memory_surface_type_filter(client, memory_db):
    # P0-c coverage: type filtresi + boş-sonuç (type dalı + total kıyas)
    con = sqlite3.connect(memory_db)
    con.execute("INSERT INTO memories (type,name,description,content) VALUES ('project','p','d','c')")
    con.execute("INSERT INTO memories (type,name,description,content) VALUES ('feedback','f','d','c')")
    con.commit()
    con.close()
    r = (await client.get("/api/v1/memory/surface?type=project")).json()
    assert r["total"] == 1
    assert r["items"][0]["type"] == "project"
    empty = (await client.get("/api/v1/memory/surface?type=user")).json()
    assert empty["total"] == 0
    assert empty["items"] == []


def test_track_read_rejects_unknown_table(memory_db):
    """Savunma-derinliği: _track_read tablo allowlist'i dışını reddeder."""
    from app.api.memory import _track_read

    con = sqlite3.connect(memory_db)
    # Geçerli tablolar sorunsuz çalışır
    con.execute("INSERT INTO memories (type,name,description,content) VALUES ('project','p','d','c')")
    con.commit()
    _track_read(con, "memories", 1)
    assert con.execute("SELECT read_count FROM memories WHERE id=1").fetchone()[0] == 1
    # Allowlist dışı (örn. user-input sızması) ValueError ile reddedilir
    with pytest.raises(ValueError, match="Invalid read-tracking table"):
        _track_read(con, "sqlite_master; DROP TABLE memories", 1)
    con.close()


def test_get_db_sets_busy_timeout(memory_db):
    """get_db busy_timeout ayarlar (kilit-çekişmesinde hata yerine bekleme)."""
    from app.api import memory as memory_mod

    db = memory_mod.get_db()
    try:
        # 0 = sınırsız değil; pozitif bir timeout set edilmiş olmalı (#517 deseni)
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Sinyal-bütünlüğü #2: create_discovery semantic-dedup dalları (NOOP/UPDATE/SUPERSEDE).
# Autouse-gate kapalıyken bu dallar çalışmaz → semantic_dedup'u mock'layıp kapsa (Codecov).
# ---------------------------------------------------------------------------
async def _post_disc(client, title, *, details="d", dtype="bug", project="p"):
    return await client.post(
        "/api/v1/memory/discoveries",
        json={"project": project, "type": dtype, "title": title, "details": details},
    )


async def test_create_dedup_noop_branch(client, memory_db, monkeypatch):
    target = (await _post_disc(client, "ozgun bulgu noop")).json()["id"]
    monkeypatch.setattr(
        "app.api.memory.signal_quality.semantic_dedup",
        lambda **k: {"operation": "NOOP", "target_id": target, "vector": None},
    )
    r2 = await _post_disc(client, "farkli baslik dedup-noop")
    assert r2.json()["status"] == "duplicate_skipped_semantic"
    assert r2.json()["id"] == target


async def test_create_dedup_update_branch(client, memory_db, monkeypatch):
    target = (await _post_disc(client, "bulgu update", details="ilk")).json()["id"]
    monkeypatch.setattr(
        "app.api.memory.signal_quality.semantic_dedup",
        lambda **k: {"operation": "UPDATE", "target_id": target, "vector": None},
    )
    r2 = await _post_disc(client, "bulgu update evrildi", details="guncel")
    assert r2.json()["status"] == "merged_semantic"
    assert r2.json()["id"] == target


async def test_create_dedup_supersede_branch(client, memory_db, monkeypatch):
    old_id = (await _post_disc(client, "eski bulgu supersede")).json()["id"]
    monkeypatch.setattr(
        "app.api.memory.signal_quality.semantic_dedup",
        lambda **k: {"operation": "SUPERSEDE", "target_id": old_id, "vector": None},
    )
    body = (await _post_disc(client, "bulgu tekrar nuksetti")).json()
    assert body["status"] == "created"
    assert body["supersedes_id"] == old_id
    # #208-P1: HALEF aktif+durable; eski superseded — atomik kritik-bölüm (await yok → cancel'da veri-kaybı yok)
    new_id = body["id"]
    r_new = await client.get(f"/api/v1/memory/discoveries/{new_id}")
    assert r_new.json()["status"] == "active"
    r3 = await client.get(f"/api/v1/memory/discoveries/{old_id}")
    assert r3.json()["status"] == "superseded"


async def test_create_dedup_supersede_same_title(client, memory_db, monkeypatch):
    """#212-P1 (Codex regresyon): AYNI (project,type,title) ile SUPERSEDE — idx_discoveries_unique_active
    aktif-başlık-unique'i ihlal etmemeli. Fix: eskiyi-superseded ÖNCE (insert'ten önce, await yok=atomik).
    Hatalı sıra (insert-önce) IntegrityError/500 verirdi."""
    old_id = (await _post_disc(client, "tekrar eden bug", details="ilk")).json()["id"]
    monkeypatch.setattr(
        "app.api.memory.signal_quality.semantic_dedup",
        lambda **k: {"operation": "SUPERSEDE", "target_id": old_id, "vector": None},
    )
    # AYNI başlık ama farklı details → 5dk-exact-window'u atla, SUPERSEDE-path'e ulaş
    r = await _post_disc(client, "tekrar eden bug", details="ikinci")
    assert r.status_code == 200  # IntegrityError YOK (eski önce superseded → unique-conflict yok)
    body = r.json()
    assert body["status"] == "created"
    assert body["supersedes_id"] == old_id
    assert (await client.get(f"/api/v1/memory/discoveries/{old_id}")).json()["status"] == "superseded"
    assert (await client.get(f"/api/v1/memory/discoveries/{body['id']}")).json()["status"] == "active"


async def test_create_concurrent_integrity_returns_existing(client, memory_db, monkeypatch):
    """#208-P2 (Codex): score_importance-await sırasında RAKİP aynı-(project,type,title) active-row
    eklenirse INSERT idx_discoveries_unique_active'i ihlal eder → 500 DEĞİL already_exists_concurrent
    (rollback + kazanan-row). exact-title-check'ten SONRA, INSERT'ten ÖNCE eklenen rakip = race simülasyonu."""
    db_path = memory_db
    monkeypatch.setattr(
        "app.api.memory.signal_quality.semantic_dedup",
        lambda **k: {"operation": "ADD", "vector": None},
    )

    def _rival_then_score(title, details):
        con = sqlite3.connect(db_path)
        con.execute(
            "INSERT INTO discoveries (project, type, title, status, valid_at) VALUES ('p','bug',?,'active',datetime('now'))",
            (title,),
        )
        con.commit()
        con.close()
        return 5

    monkeypatch.setattr("app.api.memory.signal_quality.score_importance", _rival_then_score)
    r = await _post_disc(client, "yaris baslik")
    assert r.status_code == 200  # 500 DEĞİL
    assert r.json()["status"] == "already_exists_concurrent"
