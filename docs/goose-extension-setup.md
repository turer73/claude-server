# Goose Self-Pentest Extension — Setup

This extension lets Goose drive the Klipper self-pentest workflow over MCP.
The MCP server lives at `app/mcp/goose_pentest_server.py`; it speaks
`stdio` and proxies six tools to Klipper's FastAPI.

> See [`goose-self-pentest-extension-plan.md`](goose-self-pentest-extension-plan.md)
> for the design plan, and the Phase-1 correction memo for the risks this
> setup is structured to avoid.

## 1. Install

On any machine running the Goose CLI:

```bash
pip install -e '.[goose]'   # adds the `mcp` SDK only
```

This is an **optional dep group**. Production Klipper installs don't pull
`mcp` unless someone runs Goose on the box.

## 2. Configure Goose

`~/.config/goose/config.yaml` — pick one of the two deployments below.

### 2a. Remote (recommended): Goose on your workstation, Klipper over Tailscale

```yaml
extensions:
  klipper_pentest:
    type: stdio
    name: klipper_pentest
    display_name: "Klipper Self-Pentest"
    description: "Read findings and trigger scans on owned domains"
    enabled: true
    bundled: false
    timeout: 600
    cmd: python
    args:
      - -m
      - app.mcp.goose_pentest_server
    env_keys:
      - MEMORY_API_KEY
    envs:
      MEMORY_API_BASE: "http://100.84.251.49:8420"
```

### 2b. Local: Goose on Klipper itself

```yaml
extensions:
  klipper_pentest:
    type: stdio
    name: klipper_pentest
    display_name: "Klipper Self-Pentest (local)"
    description: "Read findings and trigger scans on owned domains"
    enabled: true
    bundled: false
    timeout: 600
    cmd: /opt/linux-ai-server/venv/bin/python
    args:
      - -m
      - app.mcp.goose_pentest_server
    env_keys:
      - MEMORY_API_KEY
    envs:
      MEMORY_API_BASE: "http://127.0.0.1:8420"
```

`MEMORY_API_KEY` is the same value as Klipper's `.env`. Goose reads it from
your shell environment via `env_keys`; do **not** paste it into `envs:`,
or you'll commit a live key to dotfiles.

## 3. File permissions

`~/.config/goose/config.yaml` may end up holding the live `MEMORY_API_KEY`
in your shell init (depends on how you export it). Either way:

```bash
chmod 600 ~/.config/goose/config.yaml
```

If a `restic`/`borg`/`rclone` job covers `~/.config/`, the API key lands
in those backups. Rotate keys after install if backups already ran. See
the dual-source key memo for what gets used where — for now,
`MEMORY_API_KEY` is the single user-facing secret for this extension.

## 4. What the extension exposes

| Tool                       | Endpoint                                            |
| -------------------------- | --------------------------------------------------- |
| `pentest_list_targets`     | `GET  /api/v1/security/pentest/targets`             |
| `pentest_run_scan`         | `POST /api/v1/security/pentest/run`                 |
| `pentest_get_run`          | `GET  /api/v1/security/pentest/runs/{job_id}`       |
| `pentest_recent_findings`  | `GET  /api/v1/memory/discoveries?type=bug&...`      |
| `pentest_get_finding`      | `GET  /api/v1/memory/discoveries/{id}`              |
| `pentest_resolve_finding`  | `PUT  /api/v1/memory/discoveries/{id}/resolve`      |

The target whitelist is **server-enforced** from
`/opt/linux-ai-server/automation/self-pentest.domains` — there is no
client-side allowlist to drift. Asking Goose to scan a domain not in that
file gives back HTTP 400 from `pentest_run_scan` and the LLM sees the
error.

## 5. Why this is safe to run on Klipper itself

The MCP server's entire surface is **HTTP calls to Klipper's own FastAPI**
with the existing `X-Memory-Key` auth. It does not:

- shell out
- read or write files outside what the API contract permits
- have any built-in escape hatch like a "raw query" or "exec" tool

So even when Goose runs on Klipper (config 2b above), the worst it can
do is hammer `/api/v1/security/pentest/run` for whitelisted domains
(rate-limited by the existing exec bucket), or call
`pentest_resolve_finding` against the wrong row. Neither expands the
blast radius vs. an attacker who already had `MEMORY_API_KEY`.

That said: if you change `available_tools:` to add anything else, this
guarantee evaporates. Keep the list empty (all tools, narrow surface) or
explicitly enumerated; never add other extensions like `developer`
(shell exec) alongside this one if you care about the blast-radius
property above.

## 6. First smoke test

```text
You: list owned pentest targets
Goose: [calls pentest_list_targets] → panola.app, petvet.panola.app, ...

You: any recent findings for panola.app?
Goose: [calls pentest_recent_findings(project="panola.app")] → ...
```

If `pentest_list_targets` returns `{"error": true, "status_code": 401}`,
`MEMORY_API_KEY` isn't reaching the subprocess. Check `env_keys:` and
your shell export. If you get a connection error, `MEMORY_API_BASE` is
wrong or Klipper isn't reachable (Tailscale up? Service running?).
