-- polymem schema (V1) — SQLite
-- Four tables + FTS5 virtual tables with triggers (bootstrap also runs rebuild for safety).

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

-- ----- FTS5 (contentless, external-content style) -----

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    name, description, content,
    content='memories', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, name, description, content)
    VALUES (new.id, new.name, new.description, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, name, description, content)
    VALUES('delete', old.id, old.name, old.description, old.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, name, description, content)
    VALUES('delete', old.id, old.name, old.description, old.content);
    INSERT INTO memories_fts(rowid, name, description, content)
    VALUES (new.id, new.name, new.description, new.content);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    summary, project,
    content='sessions', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS sessions_ai AFTER INSERT ON sessions BEGIN
    INSERT INTO sessions_fts(rowid, summary, project)
    VALUES (new.id, new.summary, COALESCE(new.project, ''));
END;

CREATE TRIGGER IF NOT EXISTS sessions_ad AFTER DELETE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, summary, project)
    VALUES('delete', old.id, old.summary, COALESCE(old.project, ''));
END;

CREATE TRIGGER IF NOT EXISTS sessions_au AFTER UPDATE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, summary, project)
    VALUES('delete', old.id, old.summary, COALESCE(old.project, ''));
    INSERT INTO sessions_fts(rowid, summary, project)
    VALUES (new.id, new.summary, COALESCE(new.project, ''));
END;
