#!/usr/bin/env python3
"""Daily ops digest — read-only observer over local memory + public GitHub.

Walks the central memory DB, queries public GitHub commit feeds, and reads
local cron logs to produce a per-project status summary. Pure observer:
projects (bilge-arena, koken-akademi, etc.) are NOT touched.

Usage:
  python3 automation/digest.py                # dry-run, plain text to stdout
  python3 automation/digest.py --html         # HTML output
  python3 automation/digest.py --json         # machine-readable
  python3 automation/digest.py --send         # post HTML to Telegram

Designed to run from cron at low frequency (≤daily). Total runtime <30s.
Returns 0 on success, 1 on send-failure. Stays silent (NOTHING_NEW) if
nothing material changed in the window.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DB_PATH = "/opt/linux-ai-server/data/claude_memory.db"
ENV_PATH = "/opt/linux-ai-server/.env"
PENTEST_LOG_ROOT = Path("/var/log/self-pentest")

REPOS: dict[str, str] = {
    "linux-ai-server": "turer73/claude-server",
    "bilge-arena": "turer73/bilge-arena",
    "renderhane": "turer73/renderhane",
    "kuafor": "turer73/kuafor",
    "petvet": "turer73/petvet",
    "koken-akademi": "turer73/koken-akademi",
}

WINDOW_HOURS = 24


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


def memory_delta(window_hours: int) -> dict:
    """24h delta from claude_memory.db — open bugs, recent flips, unread notes."""
    since = (dt.datetime.now() - dt.timedelta(hours=window_hours)).strftime("%Y-%m-%d %H:%M:%S")
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        open_bugs = [
            dict(r)
            for r in db.execute(
                "SELECT id, project, title, date(created_at) AS date "
                "FROM discoveries WHERE type='bug' AND status='active' "
                "ORDER BY project, id"
            ).fetchall()
        ]
        # status='active' filter: pencerede açılıp aynı pencerede obsolete
        # edilen bug'lar (bugün hook revize ederken tetiklenip kapatılan
        # snapshot'lar gibi) "yeni bug" sayısını şişiriyor; sadece halen
        # üzerine düşmesi gereken kayıtları göster.
        new_bugs = [
            dict(r)
            for r in db.execute(
                "SELECT id, project, title FROM discoveries WHERE type='bug' AND status='active' AND created_at > ? ORDER BY id DESC",
                (since,),
            ).fetchall()
        ]
        # NOT: discoveries tablosunda updated_at yok, bu yüzden "pencerede
        # resolve edilen" sayısını veremiyoruz. Yeni açılan + halen açık olan
        # iki sinyal zaten yeterli — kapanan iş zaten görünmez olarak temizleniyor.
        unread_notes = [
            dict(r) for r in db.execute("SELECT id, title, content FROM notes WHERE COALESCE(read,0)=0 ORDER BY id DESC").fetchall()
        ]
        return {
            "open_bugs": open_bugs,
            "new_bugs": new_bugs,
            "unread_notes": unread_notes,
        }
    finally:
        db.close()


def github_commits(repo: str, since_iso: str, token: str | None = None, timeout: float = 4.0) -> list[dict]:
    """GitHub commit feed. Public repos work without auth; private repos
    require GITHUB_TOKEN with `repo` scope. 404 → silently returns []."""
    url = f"https://api.github.com/repos/{repo}/commits?since={urllib.parse.quote(since_iso)}&per_page=20"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "klipper-digest/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 — hardcoded HTTPS
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return []
        raise
    except (urllib.error.URLError, TimeoutError):
        return []
    out = []
    for c in data:
        msg = (c.get("commit", {}).get("message") or "").splitlines()[0][:90]
        out.append({"sha": (c.get("sha") or "")[:8], "msg": msg})
    return out


def all_commits(window_hours: int, token: str | None = None) -> dict[str, list[dict]]:
    since_iso = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=window_hours)).isoformat(timespec="seconds")
    out: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=len(REPOS)) as ex:
        futs = {ex.submit(github_commits, repo, since_iso, token): proj for proj, repo in REPOS.items()}
        for fut in as_completed(futs):
            proj = futs[fut]
            try:
                out[proj] = fut.result()
            except Exception:
                out[proj] = []
    return out


def cron_health() -> dict:
    """Latest self-pentest run summary + age."""
    out: dict = {"self_pentest": None}
    if not PENTEST_LOG_ROOT.exists():
        return out
    days = sorted([p for p in PENTEST_LOG_ROOT.iterdir() if p.is_dir()], reverse=True)
    if not days:
        return out
    latest = days[0]
    age = (dt.date.today() - dt.date.fromisoformat(latest.name)).days
    summary = latest / "summary.tsv"
    findings: list[dict] = []
    if summary.exists():
        for line in summary.read_text().splitlines():
            parts = line.split("|")
            if len(parts) < 6:
                continue
            domain, n_content, n_headers, n_tls, n_cookies, n_bundles = parts[:6]
            total = sum(int(x or 0) for x in parts[1:6] if x.isdigit())
            if total > 0:
                findings.append(
                    {
                        "domain": domain,
                        "content": int(n_content or 0),
                        "headers": int(n_headers or 0),
                        "tls": int(n_tls or 0),
                        "cookies": int(n_cookies or 0),
                        "bundles": int(n_bundles or 0),
                    }
                )
    out["self_pentest"] = {"date": latest.name, "age_days": age, "findings": findings}
    return out


def system_health() -> dict:
    def _run(cmd: list[str]) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
        except Exception:
            return ""

    svc = _run(["systemctl", "is-active", "linux-ai-server"]) or "unknown"
    df = _run(["df", "-h", "/"]).splitlines()
    disk = df[1].split() if len(df) > 1 else []
    free = _run(["free", "-m"]).splitlines()
    mem = free[1].split() if len(free) > 1 else []
    return {
        "service": svc,
        "disk_used_pct": disk[4] if len(disk) >= 5 else "?",
        "disk_avail": disk[3] if len(disk) >= 4 else "?",
        "mem_used_mb": mem[2] if len(mem) >= 3 else "?",
        "mem_total_mb": mem[1] if len(mem) >= 2 else "?",
    }


def has_signal(d: dict) -> bool:
    """Decide whether to emit at all — 'NOTHING_NEW' if nothing actionable."""
    m = d["memory"]
    if m["new_bugs"] or m["unread_notes"]:
        return True
    if any(v for v in d["commits"].values()):
        return True
    sp = d["cron"].get("self_pentest")
    if sp and sp["findings"]:
        return True
    return d["system"]["service"] != "active"


def render_text(d: dict) -> str:
    L: list[str] = []
    today = dt.date.today().isoformat()
    L.append(f"═══ Digest — {today} ═══")
    L.append("")

    m = d["memory"]
    L.append(f"Açık bug ({len(m['open_bugs'])}):")
    for b in m["open_bugs"]:
        L.append(f"  [{b['project']:<22}] #{b['id']:<4} {b['title'][:70]}")
    L.append("")

    L.append(f"Son {WINDOW_HOURS}h:")
    L.append(f"  + {len(m['new_bugs'])} yeni bug, {len(m['unread_notes'])} okunmamış not")
    for b in m["new_bugs"][:5]:
        L.append(f"    yeni: [{b['project']}] #{b['id']} {b['title'][:60]}")
    L.append("")

    L.append("Commit aktivitesi:")
    any_commits = False
    for proj, commits in sorted(d["commits"].items()):
        if not commits:
            continue
        any_commits = True
        L.append(f"  {proj} ({len(commits)})")
        for c in commits[:5]:
            L.append(f"    {c['sha']} {c['msg']}")
    if not any_commits:
        L.append("  (none)")
    L.append("")

    sp = d["cron"].get("self_pentest")
    if sp:
        age_note = "bugün" if sp["age_days"] == 0 else f"{sp['age_days']}g önce"
        L.append(f"Self-pentest son: {sp['date']} ({age_note}), {len(sp['findings'])} bulgulu domain")
        for f in sp["findings"]:
            parts = []
            for k in ("content", "headers", "tls", "cookies", "bundles"):
                if f[k]:
                    parts.append(f"{k}={f[k]}")
            L.append(f"  ⚠ {f['domain']}: {' '.join(parts)}")
    L.append("")

    s = d["system"]
    svc_glyph = "✓" if s["service"] == "active" else "✗"
    L.append(
        f"Sistem: {svc_glyph} linux-ai-server {s['service']}  |  "
        f"disk {s['disk_used_pct']} (free {s['disk_avail']})  |  "
        f"ram {s['mem_used_mb']}/{s['mem_total_mb']} MB"
    )
    return "\n".join(L)


def render_html(d: dict) -> str:
    """Telegram parse_mode=HTML — only <b>, <i>, <code>, <pre> are safe (no <br>)."""
    today = dt.date.today().isoformat()
    m = d["memory"]
    parts: list[str] = []
    parts.append(f"<b>Digest — {today}</b>")
    parts.append("")
    parts.append(f"<b>Açık bug ({len(m['open_bugs'])})</b>")
    for b in m["open_bugs"][:10]:
        parts.append(f"  [<code>{b['project']}</code>] #{b['id']} {b['title'][:70]}")
    if len(m["open_bugs"]) > 10:
        parts.append(f"  … (+{len(m['open_bugs']) - 10})")
    parts.append("")
    parts.append(f"<b>Son {WINDOW_HOURS}h:</b> +{len(m['new_bugs'])} yeni bug / {len(m['unread_notes'])} okunmamış not")
    parts.append("")
    parts.append("<b>Commit:</b>")
    any_commits = False
    for proj, commits in sorted(d["commits"].items()):
        if not commits:
            continue
        any_commits = True
        parts.append(f"  <i>{proj}</i> ({len(commits)})")
        for c in commits[:3]:
            parts.append(f"    <code>{c['sha']}</code> {c['msg']}")
    if not any_commits:
        parts.append("  (none)")
    parts.append("")
    sp = d["cron"].get("self_pentest")
    if sp and sp["findings"]:
        parts.append(f"<b>Pentest ({sp['date']}):</b> {len(sp['findings'])} bulgulu")
        for f in sp["findings"]:
            sub = ", ".join(f"{k}={f[k]}" for k in ("content", "headers", "tls", "cookies", "bundles") if f[k])
            parts.append(f"  ⚠ <code>{f['domain']}</code> {sub}")
    s = d["system"]
    parts.append(f"<b>Sistem:</b> {s['service']} | disk {s['disk_used_pct']} | ram {s['mem_used_mb']}/{s['mem_total_mb']}MB")
    return "\n".join(parts)


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
            ok = resp.status == 200
    except Exception as e:
        print(f"ERR: Telegram send failed: {e}", file=sys.stderr)
        return False
    return ok


def gather(token: str | None = None) -> dict:
    return {
        "memory": memory_delta(WINDOW_HOURS),
        "commits": all_commits(WINDOW_HOURS, token),
        "cron": cron_health(),
        "system": system_health(),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--html", action="store_true", help="render HTML to stdout instead of plain text")
    p.add_argument("--json", action="store_true", help="dump raw gathered data as JSON")
    p.add_argument("--send", action="store_true", help="render HTML and POST to Telegram")
    p.add_argument("--force", action="store_true", help="ignore NOTHING_NEW guard")
    args = p.parse_args()

    env = load_env()
    data = gather(token=env.get("GITHUB_TOKEN") or None)

    if args.json:
        print(json.dumps(data, default=str, indent=2))
        return 0

    if not has_signal(data) and not args.force:
        print("NOTHING_NEW")
        return 0

    if args.send:
        ok = send_telegram(render_html(data), env)
        return 0 if ok else 1

    print(render_html(data) if args.html else render_text(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
