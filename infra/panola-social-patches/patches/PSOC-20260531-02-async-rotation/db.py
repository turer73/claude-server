"""Panola Social — SQLite Database Module."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path("/opt/panola-social/data/social.db")


def get_connection():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS contents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product TEXT NOT NULL,
                content_type TEXT NOT NULL,
                pillar TEXT NOT NULL,
                title TEXT,
                caption TEXT,
                hashtags TEXT,
                media_urls TEXT,
                slide_texts TEXT,
                raw_response TEXT,
                status TEXT DEFAULT 'draft',
                scheduled_at TEXT,
                published_at TEXT,
                platform TEXT DEFAULT 'instagram',
                ig_post_id TEXT,
                error_message TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS weekly_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start TEXT NOT NULL,
                product TEXT NOT NULL,
                plan_data TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS post_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id INTEGER REFERENCES contents(id),
                likes INTEGER DEFAULT 0,
                comments INTEGER DEFAULT 0,
                shares INTEGER DEFAULT 0,
                saves INTEGER DEFAULT 0,
                reach INTEGER DEFAULT 0,
                impressions INTEGER DEFAULT 0,
                engagement_rate REAL DEFAULT 0,
                collected_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS prompt_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id INTEGER REFERENCES contents(id),
                prompt_template TEXT,
                prompt_variables TEXT,
                model TEXT,
                response_tokens INTEGER,
                quality_score INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS generation_jobs (
                job_id TEXT PRIMARY KEY,
                product TEXT NOT NULL,
                week_start TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT,
                weekly_plan_id INTEGER,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_contents_status ON contents(status);
            CREATE INDEX IF NOT EXISTS idx_contents_product ON contents(product);
            CREATE INDEX IF NOT EXISTS idx_contents_scheduled ON contents(scheduled_at);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_weekly_plans_week ON weekly_plans(week_start, product);
            CREATE INDEX IF NOT EXISTS idx_metrics_content ON post_metrics(content_id);
        """)
    print("DB initialized:", DB_PATH)


# --- Content CRUD ---

def create_content(product, content_type, pillar, title, caption, hashtags,
                   raw_response=None, slide_texts=None, media_urls=None,
                   scheduled_at=None, platform="instagram"):
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO contents
               (product, content_type, pillar, title, caption, hashtags,
                raw_response, slide_texts, media_urls, scheduled_at, platform)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (product, content_type, pillar, title, caption,
             json.dumps(hashtags, ensure_ascii=False) if isinstance(hashtags, list) else hashtags,
             json.dumps(raw_response, ensure_ascii=False) if isinstance(raw_response, dict) else raw_response,
             json.dumps(slide_texts, ensure_ascii=False) if isinstance(slide_texts, list) else slide_texts,
             json.dumps(media_urls, ensure_ascii=False) if isinstance(media_urls, list) else media_urls,
             scheduled_at, platform)
        )
        return cur.lastrowid


def get_content(content_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM contents WHERE id = ?", (content_id,)).fetchone()
        return dict(row) if row else None


def list_contents(status=None, product=None, limit=50):
    with get_db() as conn:
        query = "SELECT * FROM contents WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if product:
            query += " AND product = ?"
            params.append(product)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def update_content_status(content_id, status, **kwargs):
    with get_db() as conn:
        sets = ["status = ?", "updated_at = datetime('now')"]
        params = [status]
        for k, v in kwargs.items():
            sets.append(f"{k} = ?")
            params.append(v)
        params.append(content_id)
        conn.execute(f"UPDATE contents SET {', '.join(sets)} WHERE id = ?", params)




def update_content_media(content_id, file_paths):
    """Update media file paths for a content item."""
    with get_db() as conn:
        conn.execute(
            "UPDATE contents SET media_urls = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(file_paths, ensure_ascii=False), content_id)
        )

def get_scheduled_contents(before=None):
    """Get contents scheduled before a given time (or all scheduled)."""
    with get_db() as conn:
        if before:
            rows = conn.execute(
                "SELECT * FROM contents WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at",
                (before,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM contents WHERE status = 'scheduled' ORDER BY scheduled_at"
            ).fetchall()
        return [dict(r) for r in rows]


# --- Weekly Plans ---

def create_weekly_plan(week_start, product, plan_data):
    # Idempotent: ux_weekly_plans_week UNIQUE(week_start, product). On conflict
    # do nothing and return the existing id (defense-in-depth; planner skips
    # earlier on the default path).
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO weekly_plans (week_start, product, plan_data) VALUES (?, ?, ?) "
            "ON CONFLICT(week_start, product) DO NOTHING",
            (week_start, product, json.dumps(plan_data, ensure_ascii=False))
        )
        if cur.rowcount and cur.lastrowid:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM weekly_plans WHERE week_start = ? AND product = ?",
            (week_start, product)
        ).fetchone()
        return row[0] if row else None


def get_weekly_plan_by_week(week_start, product):
    """Return the plan for an exact (week_start, product), or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM weekly_plans WHERE week_start = ? AND product = ?",
            (week_start, product)
        ).fetchone()
        if row:
            d = dict(row)
            d["plan_data"] = json.loads(d["plan_data"])
            return d
        return None


def delete_weekly_plan_for_week(week_start, product):
    """force=replace helper: drop the plan + that week's non-published contents.
    Contents have no FK to weekly_plans, so the week is matched via scheduled_at
    date range [week_start, week_start+6]. Published content is preserved."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM weekly_plans WHERE week_start = ? AND product = ?",
            (week_start, product)
        )
        conn.execute(
            "DELETE FROM contents WHERE product = ? AND status != 'published' "
            "AND scheduled_at IS NOT NULL AND date(scheduled_at) BETWEEN ? AND date(?, '+6 days')",
            (product, week_start, week_start)
        )


def get_current_plan(product):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM weekly_plans WHERE product = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
            (product,)
        ).fetchone()
        if row:
            d = dict(row)
            d["plan_data"] = json.loads(d["plan_data"])
            return d
        return None


# --- Generation Jobs (async) ---

def create_generation_job(job_id, product, week_start):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO generation_jobs (job_id, product, week_start, status) "
            "VALUES (?, ?, ?, 'running')",
            (job_id, product, week_start)
        )


def finish_generation_job(job_id, status, weekly_plan_id=None, error=None):
    with get_db() as conn:
        conn.execute(
            "UPDATE generation_jobs SET status = ?, finished_at = datetime('now'), "
            "weekly_plan_id = ?, error = ? WHERE job_id = ?",
            (status, weekly_plan_id, error, job_id)
        )


def get_generation_job(job_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM generation_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None


# --- Metrics ---

def save_metrics(content_id, likes=0, comments=0, shares=0, saves=0,
                 reach=0, impressions=0, engagement_rate=0.0):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO post_metrics
               (content_id, likes, comments, shares, saves, reach, impressions, engagement_rate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (content_id, likes, comments, shares, saves, reach, impressions, engagement_rate)
        )


def get_metrics(content_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM post_metrics WHERE content_id = ? ORDER BY collected_at DESC",
            (content_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_analytics_overview(days=7):
    """Get aggregate metrics for last N days."""
    with get_db() as conn:
        row = conn.execute("""
            SELECT
                COUNT(DISTINCT c.id) as total_posts,
                COALESCE(SUM(m.likes), 0) as total_likes,
                COALESCE(SUM(m.comments), 0) as total_comments,
                COALESCE(SUM(m.saves), 0) as total_saves,
                COALESCE(SUM(m.reach), 0) as total_reach,
                COALESCE(AVG(m.engagement_rate), 0) as avg_engagement
            FROM contents c
            LEFT JOIN post_metrics m ON m.content_id = c.id
            WHERE c.status = 'published'
              AND c.published_at >= datetime('now', ?)
        """, (f"-{days} days",)).fetchone()
        return dict(row) if row else {}


def get_top_posts(limit=5):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT c.*, m.likes, m.comments, m.saves, m.reach, m.engagement_rate
            FROM contents c
            JOIN post_metrics m ON m.content_id = c.id
            WHERE c.status = 'published'
            ORDER BY m.engagement_rate DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


# --- Prompt History ---

def save_prompt_history(content_id, prompt_template, prompt_variables, model, response_tokens):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO prompt_history
               (content_id, prompt_template, prompt_variables, model, response_tokens)
               VALUES (?, ?, ?, ?, ?)""",
            (content_id, prompt_template,
             json.dumps(prompt_variables, ensure_ascii=False), model, response_tokens)
        )


# --- Stats ---

def get_content_stats():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT status, COUNT(*) as count FROM contents GROUP BY status
        """).fetchall()
        return {r["status"]: r["count"] for r in rows}


if __name__ == "__main__":
    init_db()
    print("Stats:", get_content_stats())
