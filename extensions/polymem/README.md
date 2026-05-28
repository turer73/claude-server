# polymem

**Multi-device tiered memory for LLM agents — FastAPI router + SQLite-first.**

A small, embedabble memory layer you can mount into any FastAPI app. Tracks
typed memories (`user` / `feedback` / `project` / `reference`), the device
they came from, and the projects each device works on. SQLite by default;
no infrastructure to stand up.

> **Status: v0.1.0 — Slice 1 (memories CRUD).**
> Slices 2 (devices + sessions) and 3 (FTS5 search + alembic migrations) land
> ahead of v0.2.0. The public surface for the memories endpoints is stable
> as documented below.

## Why another memory library?

- **SQLite-first.** No Postgres, no Redis, no vector DB. Stand it up in a
  test, ship it on a single-board computer, run it behind your mesh VPN.
- **Multi-device aware.** Every memory carries an optional `source_device`,
  and `device_projects` lets you ask "what is laptop-1 working on today?"
  This is the differentiating primitive — most memory libraries assume one
  tenant per agent instance.
- **Not a Letta clone.** The tiered-memory ergonomics borrow from the
  [MemGPT paper](https://arxiv.org/abs/2310.08560), but the tool family
  is intentionally smaller and the multi-device tables don't exist in
  Letta upstream. If you need Letta compatibility, a thin adapter layer
  is the right design — out of scope for v1.

## Install

```bash
pip install polymem
```

## Use it as a mountable router

```python
from fastapi import FastAPI
import os

from polymem import create_router

app = FastAPI()
app.include_router(
    create_router(
        db_path="./memory.db",
        api_key=os.environ["MEMORY_API_KEY"],  # pass None to disable auth
    ),
    prefix="/api/v1/memory",
)
```

## Or run it standalone

```bash
MEMORY_API_KEY=$(openssl rand -hex 32) \
  uvicorn polymem.app:create_app --factory \
  --host 0.0.0.0 --port 8420
```

(The `create_app` factory takes the same kwargs as `create_router`.)

## API surface — v0.1.0

All requests carry `X-Memory-Key: <api_key>` unless auth is disabled.

### Memories (`/memories`)

| Verb | Path | Body / Query | Returns |
| --- | --- | --- | --- |
| GET | `/memories?type=&device=&active=&limit=` | filters | array of memory rows |
| GET | `/memories/{id}` | — | single row, 404 if missing |
| POST | `/memories` | `MemoryCreate` | created row, 201 |
| PUT | `/memories/{id}` | `MemoryUpdate` (partial) | updated row, 400 if no fields |
| DELETE | `/memories/{id}` | — | soft-delete (sets `active=0`) |
| PUT | `/memories/{id}/read` | — | increments `read_count`, touches `last_read_at` |

`MemoryCreate`:

```json
{
  "type": "user | feedback | project | reference",
  "name": "short slug",
  "description": "one-line summary",
  "content": "the actual memory body — markdown ok",
  "source_device": "optional — which device wrote this",
  "rationale": "optional — why this was saved"
}
```

`MemoryUpdate` is `description`, `content`, `rationale`, `active` — all optional, any subset.

### Schema

See [`src/polymem/schema.sql`](src/polymem/schema.sql) for the four tables
(`memories`, `devices`, `device_projects`, `sessions`). `bootstrap_schema()`
runs idempotently at startup; alembic migrations land in v0.2.0.

## Blast radius

The router only exposes the documented HTTP surface. It does not shell out,
write files outside the SQLite DB, or reach across HTTP. The trust boundary
is `X-Memory-Key` + whatever transport perimeter you put in front of it
(a mesh VPN, a reverse proxy with mTLS, an internal-only network).

If you co-mount a router that *does* shell out, the guarantee no longer
applies to that combined surface. Choose deliberately.

## Project status & roadmap

| Slice | Scope | Status |
| --- | --- | --- |
| 1 | memories CRUD, auth, SQLite bootstrap | ✅ v0.1.0 |
| 2 | devices, device_projects, sessions | ⏳ v0.2.0 |
| 3 | FTS5 search, alembic migrations | ⏳ v0.2.0 |
| later | MCP client (Goose / Claude Code), Postgres backend, Letta adapter | unscheduled |

## License

Apache-2.0. See [`LICENSE`](LICENSE).
