"""Digest paketi — facade. load_env/gather/send_telegram burada; collector'lar
(sources) ve render re-export edilir → `from app.core.digest import X` ve
`core_digest.X` (test/automation) korunur."""

from __future__ import annotations

import sys
import urllib.parse
import urllib.request
from typing import Any

from app.core.digest.render import _trend_tokens, has_signal, render_html, render_text  # noqa: F401 (re-export)
from app.core.digest.sources import (  # noqa: F401 (re-export)
    CI_STALE_DAYS,
    COVERAGE_DB_PATH,
    DB_PATH,
    ENV_PATH,
    PENTEST_LOG_ROOT,
    REPOS,
    REVIEW_REPOS,
    WINDOW_HOURS,
    _ci_projects,
    _gh_json,
    _liveness_health,
    _pr_ci_state,
    _project_trend,
    _server_db_path,
    all_commits,
    ci_health,
    cron_health,
    cron_outcomes_health,
    github_commits,
    memory_delta,
    pr_review_health,
    system_health,
    vps_health,
)


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        with open(ENV_PATH) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def send_telegram(html: str, env: dict[str, str]) -> bool:
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("ERR: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": html, "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(url, data=body, method="POST")  # noqa: S310 — hardcoded HTTPS
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
            ok = bool(resp.status == 200)
    except Exception as e:
        print(f"ERR: Telegram send failed: {e}", file=sys.stderr)
        return False
    return ok


def gather(token: str | None = None) -> dict[str, Any]:
    return {
        "memory": memory_delta(WINDOW_HOURS),
        "commits": all_commits(WINDOW_HOURS, token),
        "cron": cron_health(),
        "cron_jobs": cron_outcomes_health(),
        "pr_review": pr_review_health(),
        "liveness": _liveness_health(),
        "system": system_health(),
        "vps": vps_health(),
        "ci": ci_health(),
    }
