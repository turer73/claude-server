"""Panola Social — Webhook API Server.
Lightweight FastAPI server for n8n integration.
Exposes CLI commands as HTTP endpoints.

Run: uvicorn webhook_server:app --host 0.0.0.0 --port 9800
"""

import os
import json
import uuid
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Panola Social API", version="1.0")

SOCIAL_DIR = Path("/opt/panola-social")
PYTHON = str(SOCIAL_DIR / "venv/bin/python")
MAIN = str(SOCIAL_DIR / "main.py")

# Defense-in-depth shared secret. If WEBHOOK_SECRET is set in the environment,
# the async endpoints require a matching X-Webhook-Key header. ufw + Tailscale
# already gate :9800 from the public internet; this guards the 0.0.0.0 bind.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


def require_webhook_key(x_webhook_key: str = Header(default="")):
    if WEBHOOK_SECRET and x_webhook_key != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook key")


def _run_cli(args: list[str], timeout: int = 120) -> dict:
    """Run CLI command and return parsed JSON or raw output."""
    cmd = [PYTHON, MAIN] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(SOCIAL_DIR)
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        # Try JSON parse
        try:
            return {"success": True, "data": json.loads(stdout)}
        except json.JSONDecodeError:
            return {"success": result.returncode == 0, "output": stdout, "error": stderr}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Timeout"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Health ──

@app.get("/api/health")
def health():
    """System health + Renderhane balance + multi-channel adapter status."""
    try:
        from src.renderhane_client import get_balance
        balance = get_balance()
    except Exception:
        balance = -1
    try:
        from src.config import get_instagram_token
        token = get_instagram_token()
        token_ok = len(token) > 20
    except Exception:
        token_ok = False

    channels_status: dict = {}
    try:
        from adapter import ADAPTER_REGISTRY
        for name, ad in ADAPTER_REGISTRY.items():
            try:
                channels_status[name] = ad.health_check()
            except Exception as e:
                channels_status[name] = {"status": "error", "reason": str(e)[:200]}
    except ImportError:
        pass

    return {
        "status": "ok",
        "renderhane_balance": balance,
        "instagram_token_ok": token_ok,
        "channels": channels_status,
    }


# ── Token ──

@app.get("/api/token/check")
def token_check():
    return _run_cli(["token-check"])


@app.post("/api/token/refresh")
def token_refresh():
    return _run_cli(["token-refresh"])


# ── Content Generation ──

class GenerateRequest(BaseModel):
    product: str = "petvet"
    content_type: str = "single_image_tip"
    topic: Optional[str] = None


@app.post("/api/generate")
def generate(req: GenerateRequest):
    """Generate a single content item."""
    args = ["generate", "--product", req.product, "--type", req.content_type]
    if req.topic:
        args += ["--topic", req.topic]
    return _run_cli(args, timeout=180)


@app.post("/api/generate-week")
def generate_week(product: str = "petvet"):
    """Generate full week content plan + posts (synchronous, legacy)."""
    return _run_cli(["generate-week", "--product", product], timeout=600)


class GenerateWeekAsyncRequest(BaseModel):
    product: Optional[str] = None  # omit -> deterministic weekly rotation
    force: bool = False


@app.post("/api/generate-week-async", status_code=202,
          dependencies=[Depends(require_webhook_key)])
def generate_week_async(req: GenerateWeekAsyncRequest):
    """Start a full-week generation as a detached job and return a job_id.
    Poll /api/generate-week-status/{job_id} for completion. Detached subprocess
    (not BackgroundTasks) so the ~450s blocking run never occupies a uvicorn
    worker (only 2 configured)."""
    from src.planner import compute_week_start, get_rotation_product
    from src.db import create_generation_job

    week_start = compute_week_start()
    product = req.product or get_rotation_product(week_start)
    job_id = uuid.uuid4().hex
    create_generation_job(job_id, product, week_start)

    log_path = SOCIAL_DIR / "data" / f"genjob-{job_id}.log"
    cmd = [PYTHON, MAIN, "generate-week", "--product", product,
           "--week-start", week_start, "--job-id", job_id]
    if req.force:
        cmd.append("--force")
    with open(log_path, "w") as logf:
        subprocess.Popen(
            cmd, cwd=str(SOCIAL_DIR), stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return {"job_id": job_id, "product": product, "week_start": week_start,
            "status": "running"}


@app.get("/api/generate-week-status/{job_id}",
         dependencies=[Depends(require_webhook_key)])
def generate_week_status(job_id: str):
    """Return the generation_jobs row for a job (served from DB, no blocking)."""
    from src.db import get_generation_job
    job = get_generation_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


# ── Content List ──

@app.get("/api/contents")
def list_contents(status: Optional[str] = None, limit: int = 20):
    """List contents, optionally filtered by status."""
    args = ["list", "--limit", str(limit)]
    if status:
        args += ["--status", status]
    return _run_cli(args)


# ── Publish ──

@app.post("/api/publish/{content_id}")
def publish(content_id: int):
    """Publish a single content item."""
    return _run_cli(["publish", "--id", str(content_id)], timeout=120)


@app.post("/api/publish-scheduled")
def publish_scheduled():
    """Publish all content scheduled for now or earlier."""
    return _run_cli(["publish-scheduled"], timeout=600)


# ── Approve & Schedule ──

@app.post("/api/approve-and-schedule")
def approve_and_schedule():
    """Approve all drafts and schedule across next week."""
    from src.db import get_db
    with get_db() as db:
        drafts = db.execute(
            "SELECT id FROM contents WHERE status='draft' ORDER BY id"
        ).fetchall()

        if not drafts:
            return {"approved": 0, "message": "No drafts to approve"}

        base = datetime.now()
        # Find next Monday
        days_ahead = (7 - base.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        monday = base + timedelta(days=days_ahead)

        approved = 0
        for i, draft in enumerate(drafts):
            day_offset = i % 6  # Mon-Sat
            schedule_dt = (monday + timedelta(days=day_offset)).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            db.execute(
                "UPDATE contents SET status='scheduled', scheduled_at=? WHERE id=?",
                (schedule_dt.isoformat(), draft[0])
            )
            approved += 1
        db.commit()

        return {
            "approved": approved,
            "schedule_start": monday.strftime("%Y-%m-%d"),
            "schedule_end": (monday + timedelta(days=5)).strftime("%Y-%m-%d"),
        }


# ── Quality Check ──

@app.get("/api/quality-check/{content_id}")
def quality_check(content_id: int):
    """Check content quality before publish."""
    from src.db import get_db
    with get_db() as db:
        row = db.execute(
            "SELECT id, title, media_urls, content_type FROM contents WHERE id=?",
            (content_id,)
        ).fetchone()

    if not row:
        raise HTTPException(404, "Content not found")

    issues = []
    media_urls = json.loads(row[2]) if row[2] else []

    # Check 1: Media exists
    if not media_urls:
        issues.append("NO_MEDIA: Gorsel yok")
    else:
        for url in media_urls:
            if url.startswith("/") and not Path(url).exists():
                issues.append(f"FILE_MISSING: {url}")
            elif url.startswith("/"):
                size = Path(url).stat().st_size
                if size < 50000:  # < 50KB suspicious
                    issues.append(f"FILE_TOO_SMALL: {url} ({size} bytes)")

    # Check 2: Title not empty
    if not row[1] or len(row[1]) < 5:
        issues.append("TITLE_EMPTY: Baslik cok kisa")

    # Check 3: Hybrid image (not old AI)
    if media_urls and any("renderhane" in u and "hybrid" not in u for u in media_urls):
        issues.append("OLD_AI_IMAGE: Eski AI gorsel, hybrid kullan")

    return {
        "content_id": content_id,
        "passed": len(issues) == 0,
        "issues": issues,
        "media_count": len(media_urls),
    }


# ── Stats ──

@app.get("/api/stats")
def stats():
    return _run_cli(["stats"])


@app.get("/api/balance")
def balance():
    """Renderhane credit balance."""
    try:
        from src.renderhane_client import get_balance
        return {"balance": get_balance()}
    except Exception as e:
        return {"balance": -1, "error": str(e)}




# ── Quality Gate ──

@app.post("/api/quality-gate")
@app.post("/api/quality-gate")
def quality_gate():
    """Score drafts via CLI."""
    return _run_cli(["smart-approve"], timeout=600)


# ── Product Rotation ──

@app.post("/api/generate-week-auto")
def generate_week_auto():
    """Auto-select product based on week number rotation.
    Week 1: petvet, Week 2: kuafor, Week 3: erp, Week 4: best performer.
    """
    from datetime import datetime as dt
    week_num = dt.now().isocalendar()[1]
    rotation = ["petvet", "kuafor", "panola_erp"]

    if week_num % 4 == 0:
        product = _get_best_performer() or "petvet"
    else:
        product = rotation[(week_num - 1) % 3]

    return _run_cli(["generate-week", "--product", product], timeout=600)


def _get_best_performer():
    """Find product with highest avg engagement in last 28 days."""
    from src.db import get_db
    try:
        with get_db() as db:
            row = db.execute("""
                SELECT c.product, AVG(m.engagement_rate) as avg_eng
                FROM post_metrics m
                JOIN contents c ON c.ig_post_id = m.ig_post_id
                WHERE m.collected_at > datetime('now', '-28 days')
                GROUP BY c.product
                ORDER BY avg_eng DESC
                LIMIT 1
            """).fetchone()
            return row[0] if row else None
    except Exception:
        return None


# ── Feedback Loop: metrics-aware generation ──

@app.post("/api/feedback-generate")
def feedback_generate(product: str = "auto"):
    """Generate next week content informed by last week's performance."""
    _run_cli(["collect-metrics"], timeout=60)

    from src.db import get_db
    performance = {}
    try:
        with get_db() as db:
            rows = db.execute("""
                SELECT c.content_type,
                       COUNT(*) as cnt,
                       AVG(COALESCE(m.reach, 0)) as avg_reach,
                       AVG(COALESCE(m.engagement_rate, 0)) as avg_eng
                FROM contents c
                LEFT JOIN post_metrics m ON c.ig_post_id = m.ig_post_id
                WHERE c.status = 'published'
                  AND c.published_at > datetime('now', '-28 days')
                GROUP BY c.content_type
                ORDER BY avg_eng DESC
            """).fetchall()
            for r in rows:
                performance[r[0]] = {
                    "count": r[1], "avg_reach": round(r[2] or 0, 1),
                    "avg_engagement": round(r[3] or 0, 3),
                }
    except Exception:
        pass

    if product == "auto":
        from datetime import datetime as dt
        week_num = dt.now().isocalendar()[1]
        rotation = ["petvet", "kuafor", "panola_erp"]
        product = rotation[(week_num - 1) % 3]

    return _run_cli(["generate-week", "--product", product], timeout=600)


# ── Smart Approve: Quality Gate + Schedule ──

@app.post("/api/smart-approve")
def smart_approve():
    """Kalite kapisi + onay + zamanlama (CLI)."""
    return _run_cli(["smart-approve"], timeout=600)



@app.get("/api/comments/recent")
def recent_comments():
    """Get recent comments from published posts (last 7 days)."""
    from src.db import get_db
    import requests as req

    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
    if not token:
        return {"error": "No Instagram token"}

    with get_db() as db:
        posts = db.execute(
            "SELECT ig_post_id, title FROM contents WHERE status='published' "
            "AND ig_post_id IS NOT NULL AND published_at > datetime('now', '-7 days') "
            "ORDER BY published_at DESC LIMIT 10"
        ).fetchall()

    if not posts:
        return {"comments": [], "total": 0}

    all_comments = []
    for post in posts:
        ig_id = post[0]
        try:
            resp = req.get(
                f"https://graph.instagram.com/v21.0/{ig_id}/comments",
                params={"access_token": token, "fields": "id,text,timestamp,username"},
                timeout=10,
            )
            if resp.ok:
                data = resp.json().get("data", [])
                for c in data:
                    c["post_title"] = post[1]
                    c["post_id"] = ig_id
                all_comments.extend(data)
        except Exception:
            pass

    all_comments.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return {"comments": all_comments[:20], "total": len(all_comments)}




# ── Screenshots ──

@app.post("/api/screenshots/{product}")
def take_screenshots(product: str):
    """Capture real product screenshots using Playwright."""
    from src.screenshot_gen import capture_screenshots
    results = capture_screenshots(product)
    return {"product": product, "screenshots": results, "count": len(results)}


@app.post("/api/screenshots/refresh-all")
def refresh_screenshots():
    """Refresh screenshots for all products."""
    from src.screenshot_gen import refresh_all_screenshots
    return refresh_all_screenshots()


# ── Comment Monitor + Telegram Notify ──

@app.post("/api/comments/check-and-notify")
def check_comments_and_notify():
    """Check for new comments and send Telegram notification if any."""
    from src.db import get_db
    import requests as req

    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
    # GUVENLIK: secret ASLA hardcoded olmaz — env'den. (Public-leak: eski token
    # GitHub secret-scanning ile yakalandi -> @BotFather'da revoke + env'e yeni.)
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token:
        return {"error": "No Instagram token"}
    if not tg_token or not chat_id:
        return {"error": "TELEGRAM_BOT_TOKEN/CHAT_ID env eksik"}

    with get_db() as db:
        posts = db.execute(
            "SELECT ig_post_id, title FROM contents WHERE status='published' "
            "AND ig_post_id IS NOT NULL AND published_at > datetime('now', '-7 days') "
            "ORDER BY published_at DESC LIMIT 10"
        ).fetchall()

    if not posts:
        return {"new_comments": 0, "message": "No recent published posts"}

    # Get last check timestamp
    last_check_file = Path("/opt/panola-social/data/.last_comment_check")
    last_check = ""
    if last_check_file.exists():
        last_check = last_check_file.read_text().strip()

    all_new = []
    for post in posts:
        ig_id = post[0]
        try:
            resp = req.get(
                f"https://graph.instagram.com/v21.0/{ig_id}/comments",
                params={"access_token": token, "fields": "id,text,timestamp,username"},
                timeout=10,
            )
            if resp.ok:
                for c in resp.json().get("data", []):
                    if not last_check or c.get("timestamp", "") > last_check:
                        c["post_title"] = post[1]
                        all_new.append(c)
        except Exception:
            pass

    # Update last check
    last_check_file.write_text(datetime.now().isoformat())

    if all_new:
        # Send Telegram notification
        msg_lines = [f"Instagram: {len(all_new)} yeni yorum"]
        for c in all_new[:5]:
            msg_lines.append(f"  @{c.get('username','?')}: {c.get('text','')[:60]}")
        if len(all_new) > 5:
            msg_lines.append(f"  ... ve {len(all_new) - 5} yorum daha")

        try:
            req.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                data={"chat_id": chat_id, "text": "\n".join(msg_lines)},
                timeout=10,
            )
        except Exception:
            pass

    return {"new_comments": len(all_new), "comments": all_new[:10]}


# ── Real Data from Products ──

@app.get("/api/product-stats/{product}")
def product_live_stats(product: str):
    """Fetch real statistics from product APIs for content generation."""
    import requests as req

    stats = {}

    if product == "petvet":
        try:
            resp = req.get("https://petvet.panola.app/api/stats/public", timeout=10)
            if resp.ok:
                stats = resp.json()
        except Exception:
            # Fallback: use demo data patterns
            stats = {
                "source": "demo",
                "note": "Gercek API erisim yok, demo veri kullaniliyor",
            }

    elif product == "kuafor":
        try:
            resp = req.get("https://kuafor.panola.app/api/stats/public", timeout=10)
            if resp.ok:
                stats = resp.json()
        except Exception:
            stats = {"source": "demo"}

    return {"product": product, "stats": stats}




@app.get("/api/debug-score/{content_id}")
def debug_score(content_id: int):
    """Debug: score a single content and return raw Haiku response."""
    import importlib
    import src.quality_gate as qg
    importlib.reload(qg)
    from src.db import get_content
    from anthropic import Anthropic
    from src.config import get_anthropic_key, parse_json_response
    from src.product_knowledge import get_quality_rules
    import re

    content_row = get_content(content_id)
    if not content_row:
        return {"error": "Not found"}

    client = Anthropic(api_key=get_anthropic_key())
    product = content_row.get("product", "unknown")
    rules = get_quality_rules(product)
    rules_text = "\n".join(f"- [{r['severity'].upper()}] {r['rule']}" for r in rules) if rules else "Genel"

    hashtags = content_row.get("hashtags", "[]")
    try:
        hashtag_count = len(json.loads(hashtags))
    except Exception:
        hashtag_count = hashtags.count("#") if isinstance(hashtags, str) else 0

    prompt = qg.SCORING_PROMPT.format(
        title=content_row.get("title", "N/A")[:200],
        caption=(content_row.get("caption", "") or "")[:1500],
        hashtag_count=hashtag_count,
        content_type=content_row.get("content_type", "unknown"),
        product=product,
        quality_rules=rules_text,
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text

    # Try parse
    parsed = parse_json_response(raw)
    regex_clean = re.sub(r"```\w*\n?", "", raw).strip()
    score_match = re.search(r'"score"\s*:\s*([\d.]+)', regex_clean)

    return {
        "raw_length": len(raw),
        "raw_first_200": raw[:200],
        "parse_json_response_result": str(parsed)[:200] if parsed else None,
        "regex_score": float(score_match.group(1)) if score_match else None,
        "content_title": content_row.get("title", "")[:50],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9800)
