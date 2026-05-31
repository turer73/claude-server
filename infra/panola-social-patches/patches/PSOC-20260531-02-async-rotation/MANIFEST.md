# PSOC-20260531-02 — panola_weekly_gen async job+poll + real rotation

**DEPLOYED 2026-05-31** (klipper, user-approved). Closes the weekly rotation
ECONNABORTED timeout + the duplicate-plan idempotency bug. Discovery-first, 3
validate rounds (notes #99650/#99652/#99654), deploy report #99656, closed #99657.

> panola-social has no git repo; these are the authoritative copies of the
> deployed VPS files (`/opt/panola-social/`). On redeploy, restore from here.

## Files (full deployed versions)
| Patch file | VPS path | Change |
|---|---|---|
| `db.py` | `src/db.py` | idempotent `create_weekly_plan` (ON CONFLICT), `get_weekly_plan_by_week`, `delete_weekly_plan_for_week`, `generation_jobs` helpers; `init_db` gains `generation_jobs` + UNIQUE `ux_weekly_plans_week` |
| `planner.py` | `src/planner.py` | `generate_weekly_plan(force=False)` skip/replace idempotency; `generate_week_content` skip short-circuit; `compute_week_start` + `get_rotation_product` |
| `main.py` | `main.py` | `generate-week` gains `--job-id` / `--force`; writes `generation_jobs` status |
| `webhook_server.py` | `webhook_server.py` | `POST /api/generate-week-async` (detached Popen) + `GET /api/generate-week-status/{job_id}` + `X-Webhook-Key` auth |
| `../../sql/002_generation_jobs_and_dedup.sql` | DB | generation_jobs + DEDUP (one-time) + UNIQUE index |

## Rotation
`get_rotation_product(target_week_start) = PRODUCTS[iso_week(target) % len(PRODUCTS)]`,
PRODUCTS = config/products.yml order `[petvet, kuafor, panola_erp, renderhane]` (len 4).
Deterministic → re-run for the same week resolves to the same product (idempotency-friendly).

## Auth
`WEBHOOK_SECRET` via systemd drop-in `/etc/systemd/system/panola-social-webhook.service.d/secret.conf`
(NOT in this repo). Async endpoints require `X-Webhook-Key`; key-less → 401.

## n8n (klipper)
Workflow `panola_weekly_gen` rebuilt: schedule (Pazar 10:00, untouched) → POST async
→ Wait 12m → GET status → IF done → Telegram OK/Fail. Export backup:
`n8n-backups/rotasyon-pre-async-20260531-1541.json`.
Known simplification vs spec: single Wait(12m) instead of a 30s poll-loop
(surer-approved #99657 — the held-connection bug is already gone; loop is fragile via raw API).

## Rollback
- Code: VPS `*.bak-pre-psoc02` (+ `db.py.bak-pre-initdb`).
- DB: `data/social.db.bak-pre-psoc02-20260531-1541` (DEDUP is irreversible without restore).
- n8n: `n8n-backups/rotasyon-pre-async-20260531-1541.json`.

## Open
- n8n workflow not runtime-tested (manual trigger would real-generate via rotation).
  First real run = natural Sunday trigger 2026-06-07.
