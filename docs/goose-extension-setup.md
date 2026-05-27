# Goose Self-Pentest Extension on Klipper

Klipper ships only the **backend** of the self-pentest workflow; the MCP
server is the generic OSS package at
[`extensions/goose-pentest-mcp/`](../extensions/goose-pentest-mcp/). One
package, one source of truth — no Klipper-specific flavour.

> Design and earlier decisions:
> [`goose-self-pentest-extension-plan.md`](goose-self-pentest-extension-plan.md).

## What Klipper provides

Six endpoints implementing
[`BACKEND_CONTRACT.md`](../extensions/goose-pentest-mcp/BACKEND_CONTRACT.md):

| Tool contract path                            | Klipper route                                       |
| --------------------------------------------- | --------------------------------------------------- |
| `GET  /pentest/targets`                       | `GET  /api/v1/security/pentest/targets`             |
| `POST /pentest/run`                           | `POST /api/v1/security/pentest/run`                 |
| `GET  /pentest/runs/{job_id}`                 | `GET  /api/v1/security/pentest/runs/{job_id}`       |
| `GET  /pentest/findings`                      | `GET  /api/v1/security/pentest/findings`            |
| `GET  /pentest/findings/{id}`                 | `GET  /api/v1/security/pentest/findings/{id}`       |
| `PUT  /pentest/findings/{id}/resolve`         | `PUT  /api/v1/security/pentest/findings/{id}/resolve` |

The `findings` paths are thin adapters over `/api/v1/memory/discoveries`
(type pinned to `bug`). Auth header `X-Pentest-Key` is accepted; the
legacy `X-Memory-Key` is also accepted with the same value, for
backward-compat callers.

## Install (Klipper-local Goose)

```bash
pip install -e /opt/linux-ai-server/extensions/goose-pentest-mcp
```

(Or from the future PyPI release: `pip install goose-pentest-mcp`.)

## Configure Goose

`~/.config/goose/config.yaml`:

```yaml
extensions:
  pentest:
    type: stdio
    name: pentest
    display_name: "Self-Pentest"
    description: "Klipper-backed: list targets, run scans, manage findings"
    enabled: true
    bundled: false
    timeout: 600
    cmd: /opt/linux-ai-server/venv/bin/python
    args:
      - -m
      - goose_pentest_mcp.server
    env_keys:
      - PENTEST_API_KEY
    envs:
      PENTEST_API_BASE: "http://127.0.0.1:8420/api/v1/security"
```

Shell rc:

```bash
# Re-use the existing MEMORY_API_KEY — they are the same secret.
export PENTEST_API_KEY="$MEMORY_API_KEY"
```

## Remote Goose (workstation -> Klipper via Tailscale)

Same config, replace base URL:

```yaml
    envs:
      PENTEST_API_BASE: "http://100.84.251.49:8420/api/v1/security"
```

## Blast radius

The MCP server is HTTP-only — see
[`extensions/goose-pentest-mcp/README.md`](../extensions/goose-pentest-mcp/README.md#blast-radius).
Running Goose on Klipper itself does not expand attack surface beyond what
an authenticated holder of `MEMORY_API_KEY` could already do. If you
co-install the `developer` extension (which has shell exec), that
property no longer holds.

## Permissions

```bash
chmod 600 ~/.config/goose/config.yaml
```

Backup scope (`restic`, `borg`, `rclone`) covering `~/.config/` carries
exported secrets. Rotate `MEMORY_API_KEY` if your backups have already
captured it.
