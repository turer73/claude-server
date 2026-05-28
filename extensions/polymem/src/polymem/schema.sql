-- polymem schema (V1) — SQLite
-- Four tables, no FTS index here (created lazily on first /search call).

CREATE TABLE IF NOT EXISTS memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT NOT NULL CHECK(type IN ('user','feedback','project','reference')),
    name            TEXT NOT NULL,
    description     TEXT NOT NULL,
    content         TEXT NOT NULL,
    source_device   TEXT,                              -- nullable: single-tenant adopters can ignore
    rationale       TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    read_count      INTEGER NOT NULL DEFAULT 0,
    last_read_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_memories_type   ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_active ON memories(active);
CREATE INDEX IF NOT EXISTS idx_memories_device ON memories(source_device);

CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT UNIQUE NOT NULL,
    platform        TEXT NOT NULL,                     -- 'linux'|'macos'|'windows'|'android'|'ios'|...
    hostname        TEXT,
    ip              TEXT,
    mesh_ip         TEXT,                              -- generic 'mesh' (Tailscale/Nebula/Headscale/...)
    os_version      TEXT,
    client_version  TEXT,                              -- agent build identifier (free-form)
    notes           TEXT,
    last_seen       TEXT NOT NULL DEFAULT (datetime('now')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS device_projects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_name     TEXT NOT NULL,
    project         TEXT NOT NULL,
    local_path      TEXT,
    last_activity   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(device_name, project)
);

CREATE INDEX IF NOT EXISTS idx_device_projects ON device_projects(device_name);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_name     TEXT,
    project         TEXT,
    date            TEXT NOT NULL DEFAULT (date('now')),
    summary         TEXT NOT NULL,
    metadata        TEXT,                              -- JSON-encoded free-form (tasks_completed, files_changed, ...)
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_device ON sessions(device_name);
CREATE INDEX IF NOT EXISTS idx_sessions_date   ON sessions(date);
