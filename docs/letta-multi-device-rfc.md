# RFC: Multi-Device Memory Sync — Extending Letta beyond Single-Tenant

**Status:** Draft (to be filed at letta-ai/letta as Discussion / Issue)
**Date:** 2026-05-27
**Author:** Klipper-Server project (FastAPI + Claude Code agent infrastructure)
**Reference impl:** github.com/turer73/claude-server

---

## TL;DR

Letta's tiered memory architecture (core / recall / archival, MemGPT paper) is excellent for single-agent state, but assumes one tenant per agent instance. We have a working production implementation where **the same logical memory layer is accessed from 4 devices (Linux server "klipper", Windows desktop, Windows laptop, Android phone) across 5 projects** — and Letta cannot model this today without forking the schema. This RFC describes the missing primitives (`devices`, `device_projects`) and asks whether upstream is interested in a contribution.

---

## Why this matters

A real engineering workflow lives across devices:

- Linux server runs Claude Code (autonomous + interactive)
- Windows desktop runs Claude Desktop / Cursor
- Phone notes feed into the memory system via Telegram → klipper note-poller
- Multiple projects (server, web apps, CLI tools) all share a memory namespace

Today, every Letta agent gets isolated memory. To model the above with vanilla Letta you'd run 4 agents and copy-paste — losing the "single source of truth" property that makes tiered memory valuable in the first place.

---

## Concrete use case

```
              ┌──────────────────┐
              │  Memory layer    │
              │  (single tenant) │
              └────────┬─────────┘
                       │ today's Letta: 1 agent per "user"
        ┌──────────────┼──────────────┬────────────────┐
        ▼              ▼              ▼                ▼
   klipper        windows-desktop  windows-laptop  android-phone
   (Linux,        (Claude          (Cursor)        (Telegram
   FastAPI,       Desktop)                          notes only)
   kernel mods)
```

The 4 devices need:

1. **Read same memory** — a fix discovered on klipper is visible on windows-desktop next session.
2. **Write attributed memory** — when windows-desktop writes a memory, the source is preserved.
3. **Per-device + per-project filtering** — "show me memories from android-phone tagged project=panola".
4. **Cross-device discovery** — "find memories about OAuth race" should return klipper, surer, laptop entries.

---

## Reference implementation (working, in production)

We extended a SQLite-first memory schema with two relational primitives:

```sql
CREATE TABLE memories (
    id            INTEGER PRIMARY KEY,
    type          TEXT NOT NULL,           -- user|feedback|project|reference
    name          TEXT NOT NULL,           -- semantic slug
    description   TEXT NOT NULL,
    content       TEXT NOT NULL,
    source_device TEXT DEFAULT 'klipper',  -- WHO wrote this
    -- ... timestamps, read_count, rationale, active
);

CREATE TABLE devices (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,         -- 'klipper', 'windows-desktop'
    platform        TEXT NOT NULL,         -- 'linux', 'windows', 'android'
    hostname        TEXT,
    ip              TEXT,
    tailscale_ip    TEXT,                  -- mesh network identity
    os_version      TEXT,
    claude_version  TEXT
);

CREATE TABLE device_projects (
    id            INTEGER PRIMARY KEY,
    device_name   TEXT NOT NULL,           -- FK -> devices.name
    project       TEXT NOT NULL,
    local_path    TEXT,                    -- where on this device
    last_activity TEXT DEFAULT CURRENT_TIMESTAMP
);
```

Query patterns this enables:

```sql
-- "Recent android-phone memories that touched project=panola"
SELECT m.* FROM memories m
JOIN device_projects dp ON m.source_device = dp.device_name
WHERE dp.device_name = 'android-phone' AND dp.project = 'panola'
ORDER BY m.created_at DESC LIMIT 10;

-- "Which projects has klipper worked on this week?"
SELECT DISTINCT project FROM device_projects
WHERE device_name = 'klipper' AND last_activity > datetime('now','-7 days');
```

API surface (FastAPI):

```
POST /api/v1/memory/memories       (write — body includes source_device)
GET  /api/v1/memory/memories?device=X&project=Y
POST /api/v1/memory/devices        (register a new device)
POST /api/v1/memory/devices/X/projects  (attach project to device)
```

**Auth model:** single `X-Memory-Key` shared across devices. Trust boundary is the Tailscale mesh, not per-device tokens.

---

## How this could land in Letta

Two options, ordered by upstream effort:

### Option A — `device` as first-class field on memory blocks (minimal change)

Add `source_device: Optional[str]` to memory blocks. Tools (`core_memory_replace`, `archival_memory_insert`) accept and preserve it. Query API exposes `device=` filter.

Pros: tiny patch, no new tables, agents that don't care ignore it.
Cons: doesn't model device-to-project relationships; consumers reinvent the join.

### Option B — `devices` + `device_projects` relations (full proposal)

Adds the two tables above. New tool calls:
- `register_device(name, platform, ...)`
- `attach_device_project(device, project, path)`
- `query_memories_by_device(device, project=None)`

Pros: complete schema, matches reference impl.
Cons: bigger surface; raises governance questions (who owns devices? cross-agent sharing?).

---

## Open questions for upstream

1. **Identity boundary** — does a "device" belong to an agent, a user, or an organization? Letta's current ownership model is user→agent→memory; devices may need to be peers of agents.
2. **Sync model** — push vs. pull vs. CRDT? Our impl is server-side single SQLite with HTTP write — simple but assumes always-online.
3. **Conflict resolution** — two devices writing the same memory slug at once. We use last-write-wins (`updated_at`), but archival/core distinction may need stronger semantics.
4. **Tiered memory + multi-device** — should tier (core/recall/archival) be per-device or shared? Our impl shares; this means a "core memory" on one device shows up everywhere, which is what we want, but Letta may have reasons to scope tighter.

---

## What we'd like

- **A signal** — is this in scope for Letta core, or should we fork as `letta-multi-device`?
- **Schema review** — if in scope, what's the right shape of `devices` + `device_projects` for Letta's existing migrations?
- **Tool-call API** — should device awareness be a new tool family, or extend existing memory tools with optional kwargs?

We've been running the reference implementation since 2026-04 across 4 devices and 5 projects without issues. Happy to open a PR if there's interest.

---

## Appendix: production stats (as of 2026-05-27)

- **Devices registered:** 4 (klipper-linux, windows-desktop, windows-laptop, android-phone)
- **Projects:** 5 (linux-ai-server, panola, bilge-arena, petvet, kuafor)
- **Total memories:** 698 (across all devices)
- **Memory tiers (post tiered-migration commit):** Core 6, Recall 32, Archival 8 (file-based MEMORY.md index; full body in SQLite + disk)
- **Cross-device reads/day:** ~200 (most: klipper → SessionStart hook auto-inject)

---

*Reference implementation files (klipper-server repo):*
- `app/api/memory/` — FastAPI routes
- `app/api/memory/schema.sql` — SQLite DDL
- `scripts/claude-memory.sh` — CLI helper
- `data/claude_memory.db` — production DB

*Letta files this touches (best guess):*
- `letta/schemas/memory.py`
- `letta/services/memory_service.py`
- `letta/server/rest_api/routers/v1/memory.py`
