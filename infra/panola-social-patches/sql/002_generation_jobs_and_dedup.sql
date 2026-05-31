-- PSOC-20260531-02 — async job+poll refactor + idempotency migration
-- Target: /opt/panola-social/data/social.db (SQLite)
-- DEPLOYED 2026-05-31 (klipper, user-approved). Re-runnable except the DEDUP
-- block (one-time, destructive — already executed; kept for the record).

-- ── 1. Schema (idempotent; also added to src/db.py init_db) ──────────────────
CREATE TABLE IF NOT EXISTS generation_jobs (
    job_id TEXT PRIMARY KEY,
    product TEXT NOT NULL,
    week_start TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',   -- running / done / failed
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    weekly_plan_id INTEGER,
    error TEXT
);

-- ── 2. DEDUP (ONE-TIME, DESTRUCTIVE — already run 2026-05-31) ─────────────────
-- Must run BEFORE the UNIQUE index. weekly_plans had live duplicates from the
-- pre-idempotency rotation (e.g. 2026-04-13 petvet x8, 2026-06-01 petvet x3).
-- contents dedup is guarded: only non-NULL scheduled_at and never 'published'.
--   Executed result: weekly_plans -11 (20->9), contents -21. 121 NULL + 25
--   published rows preserved.
DELETE FROM weekly_plans
 WHERE id NOT IN (SELECT MAX(id) FROM weekly_plans GROUP BY week_start, product);

DELETE FROM contents
 WHERE scheduled_at IS NOT NULL AND scheduled_at != '' AND status != 'published'
   AND id NOT IN (
        SELECT MAX(id) FROM contents
         WHERE scheduled_at IS NOT NULL AND scheduled_at != '' AND status != 'published'
         GROUP BY product, scheduled_at
   );

-- ── 3. UNIQUE index (requires DEDUP first) ───────────────────────────────────
CREATE UNIQUE INDEX IF NOT EXISTS ux_weekly_plans_week ON weekly_plans(week_start, product);
