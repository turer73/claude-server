"""Daily ops digest — read-only observer over local memory + GitHub.

Walks the central memory DB, queries GitHub commit feeds, reads local
cron logs, samples local service health. Pure observer: never writes
to project codebases or runtimes (bilge-arena, koken-akademi, etc.).

Both the CLI in `automation/digest.py` and the API route in
`app/api/digest.py` import from here so the data path stays single-source.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DB_PATH = "/opt/linux-ai-server/data/claude_memory.db"
# Live test results: run-all-tests.sh writes coverage.db daily. (ci_tests.db is
# orphaned — no automation ever wrote it; see LAISRV-20260601-01 investigation.)
COVERAGE_DB_PATH = "/opt/linux-ai-server/data/coverage.db"
ENV_PATH = "/opt/linux-ai-server/.env"
PENTEST_LOG_ROOT = Path("/opt/linux-ai-server/logs/self-pentest")

# test-runner is scheduled daily; a latest run older than this is "stale"
CI_STALE_DAYS = 2

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
        # edilen bug'lar (hook revize ederken tetiklenip kapatılan snapshot'lar)
        # "yeni bug" sayısını şişirir; sadece halen açık olanları göster.
        new_bugs = [
            dict(r)
            for r in db.execute(
                "SELECT id, project, title FROM discoveries WHERE type='bug' AND status='active' AND created_at > ? ORDER BY id DESC",
                (since,),
            ).fetchall()
        ]
        unread_notes = [
            dict(r) for r in db.execute("SELECT id, title, content FROM notes WHERE COALESCE(read,0)=0 ORDER BY id DESC").fetchall()
        ]
        return {"open_bugs": open_bugs, "new_bugs": new_bugs, "unread_notes": unread_notes}
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


def cron_outcomes_health() -> dict:
    """Latest REAL outcome per cron job from server.db.cron_outcomes (written by
    klipper-cron-wrap.sh, LIVESYS Faz 1). Surfaces jobs whose result — not just
    rc — is fail/partial within the window. Complements Uptime-Kuma ('never
    ran'); this catches 'ran but bad'. Returns {} on any error."""
    try:
        db = sqlite3.connect(_server_db_path())
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT job, result, rc, source, detail, timestamp FROM cron_outcomes c "
                "WHERE id = (SELECT MAX(id) FROM cron_outcomes WHERE job = c.job) "
                "AND timestamp > datetime('now', ?) ORDER BY job",
                (f"-{WINDOW_HOURS} hours",),
            ).fetchall()
        finally:
            db.close()
    except Exception:
        return {}
    jobs = [dict(r) for r in rows]
    return {"jobs": jobs, "bad": [j for j in jobs if j.get("result") != "pass"]}


def _liveness_health() -> dict:
    """LIVESYS Faz 2 liveness monitor (app.core.liveness). dead/stale kaynakları
    yüzeye çıkarır. Hata/yokluk halinde {} (dijest yine de üretilir)."""
    try:
        from app.core import liveness

        return liveness.check_all()
    except Exception:
        return {}


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


def _server_db_path() -> str:
    # server.db lives alongside the memory DB in the data dir; the service sets
    # DB_PATH to exactly this. Honor DB_PATH when present (service/cron env),
    # else fall back to the data-dir sibling — never the empty /var/lib default.
    return os.environ.get("DB_PATH") or str(Path(DB_PATH).with_name("server.db"))


def vps_health() -> dict:
    """Latest VPS sample from server.db.vps_metrics_history (written by DevOpsAgent).

    Returns {} when no data exists yet — digest sections degrade gracefully.
    """
    try:
        db = sqlite3.connect(_server_db_path())
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("SELECT * FROM vps_metrics_history ORDER BY timestamp DESC LIMIT 1").fetchone()
        finally:
            db.close()
    except Exception:
        return {}
    if not row:
        return {}
    return {
        "timestamp": row["timestamp"],
        "online": bool(row["online"]),
        "cpu": row["cpu_usage"],
        "mem": row["memory_usage"],
        "disk": row["disk_usage"],
        "containers_total": row["containers_total"],
        "containers_up": row["containers_up"],
    }


def ci_health() -> dict:
    """Latest test run from coverage.db — totals, failing projects, staleness.

    coverage.db.test_runs is written daily by run-all-tests.sh. Returns {} when
    no data exists. `age_days` surfaces silent staleness (the runner is daily);
    `stale` is True past CI_STALE_DAYS.
    """
    try:
        db = sqlite3.connect(COVERAGE_DB_PATH)
        db.row_factory = sqlite3.Row
        try:
            runs = db.execute("SELECT * FROM test_runs ORDER BY id DESC LIMIT 2").fetchall()
        finally:
            db.close()
    except Exception:
        return {}
    if not runs:
        return {}
    run = runs[0]

    age_days = None
    try:
        # timestamp like '2026-06-01T06:01:10+03:00'
        started = dt.datetime.fromisoformat(run["timestamp"])
        now = dt.datetime.now(started.tzinfo) if started.tzinfo else dt.datetime.now()
        age_days = (now - started).days
    except (ValueError, TypeError):
        pass

    failing = []
    try:
        for proj, info in json.loads(run["details"] or "{}").items():
            if info.get("failed"):
                passed = info.get("passed", 0)
                failing.append({"project": proj, "passed": passed, "total": passed + info["failed"]})
    except (ValueError, TypeError, AttributeError):
        pass

    trend, regressions = _project_trend(run, runs[1] if len(runs) > 1 else None)

    return {
        "started_at": run["timestamp"],
        "age_days": age_days,
        "stale": age_days is not None and age_days > CI_STALE_DAYS,
        "total": run["total_tests"],
        "passed": run["total_passed"],
        "failed": run["total_failed"],
        "failing_projects": failing,
        "trend": trend,
        "regressions": regressions,
        "open_failures": [],
    }


def _ci_projects(row) -> dict:
    """{project: passed_count} from a test_runs.details JSON column."""
    if row is None:
        return {}
    try:
        return {p: info.get("passed", 0) for p, info in json.loads(row["details"] or "{}").items()}
    except (ValueError, TypeError, AttributeError):
        return {}


def _project_trend(cur_row, prev_row) -> tuple[list[dict], list[dict]]:
    """Per-project passed-count delta of the latest run vs the previous one.

    Returns (all_changes, regressions). Consecutive-run window keeps day-to-day
    deltas small; a project that drops count or vanishes is a regression (e.g.
    tests deleted or a project that stopped being tested), growth is info-only.
    """
    cur, prev = _ci_projects(cur_row), _ci_projects(prev_row)
    if not prev:
        return [], []
    changes: list[dict] = []
    for proj in sorted(set(cur) | set(prev)):
        if proj not in prev:
            changes.append({"project": proj, "kind": "new", "to": cur[proj]})
        elif proj not in cur:
            changes.append({"project": proj, "kind": "dropped", "from": prev[proj]})
        elif cur[proj] != prev[proj]:
            changes.append({"project": proj, "kind": "delta", "from": prev[proj], "to": cur[proj], "delta": cur[proj] - prev[proj]})
    regressions = [c for c in changes if c["kind"] == "dropped" or (c["kind"] == "delta" and c["delta"] < 0)]
    return changes, regressions


def _trend_tokens(trend: list[dict]) -> list[str]:
    """Compact per-project change tokens, e.g. '↑bilge-arena +6', '⊘old-proj'."""
    toks: list[str] = []
    for c in trend:
        if c["kind"] == "dropped":
            toks.append(f"⊘{c['project']}")
        elif c["kind"] == "new":
            toks.append(f"+{c['project']}(yeni)")
        else:
            arrow = "↑" if c["delta"] > 0 else "↓"
            sign = f"+{c['delta']}" if c["delta"] > 0 else str(c["delta"])
            toks.append(f"{arrow}{c['project']} {sign}")
    return toks


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
    if d["system"]["service"] != "active":
        return True
    v = d.get("vps") or {}
    if v and (not v.get("online") or (v.get("cpu") or 0) >= 90 or (v.get("mem") or 0) >= 90 or (v.get("disk") or 0) >= 90):
        return True
    if (d.get("cron_jobs") or {}).get("bad"):
        return True
    if (d.get("liveness") or {}).get("dead"):
        return True
    ci = d.get("ci") or {}
    return bool(ci and ((ci.get("failed") or 0) > 0 or ci.get("stale") or ci.get("regressions")))


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
            sub_parts = []
            for k in ("content", "headers", "tls", "cookies", "bundles"):
                if f[k]:
                    sub_parts.append(f"{k}={f[k]}")
            L.append(f"  ⚠ {f['domain']}: {' '.join(sub_parts)}")
    L.append("")
    cj = d.get("cron_jobs") or {}
    if cj.get("jobs"):
        bad = cj.get("bad") or []
        if bad:
            L.append(f"Cron işleri ({len(bad)} sorunlu / {len(cj['jobs'])} izlenen):")
            for j in bad:
                L.append(f"  ⚠ {j['job']}: {j['result']} (rc={j['rc']}, {j['source']}) {(j.get('detail') or '')[:60]}")
        else:
            L.append(f"Cron işleri: ✓ {len(cj['jobs'])} iş izlendi, hepsi pass")
        L.append("")
    lv = d.get("liveness") or {}
    bad_lv = (lv.get("dead") or []) + (lv.get("stale") or [])
    if bad_lv:
        L.append(f"Liveness ({len(lv.get('dead') or [])} ölü / {len(lv.get('stale') or [])} stale):")
        for r in bad_lv:
            L.append(f"  {'☠' if r['status'] == 'dead' else '⚠'} {r['source']} [{r['klass']}]: {r['detail'][:55]}")
        L.append("")
    s = d["system"]
    svc_glyph = "✓" if s["service"] == "active" else "✗"
    L.append(
        f"Sistem: {svc_glyph} linux-ai-server {s['service']}  |  "
        f"disk {s['disk_used_pct']} (free {s['disk_avail']})  |  "
        f"ram {s['mem_used_mb']}/{s['mem_total_mb']} MB"
    )
    v = d.get("vps") or {}
    if v:
        if v.get("online"):
            L.append(
                f"VPS: ✓ cpu {v['cpu']:.0f}%  |  ram {v['mem']:.0f}%  |  "
                f"disk {v['disk']:.0f}%  |  {v['containers_up']}/{v['containers_total']} container"
            )
        else:
            L.append("VPS: ✗ erişilemiyor")
    ci = d.get("ci") or {}
    if ci:
        age = "?" if ci["age_days"] is None else f"{ci['age_days']}g önce"
        stale = " ⚠ BAYAT" if ci.get("stale") else ""
        L.append(f"CI: son run {ci['started_at'][:10]} ({age}{stale})  |  {ci['passed']}/{ci['total']} geçti, {ci['failed']} fail")
        for fp in ci.get("failing_projects", []):
            L.append(f"  ✗ {fp['project']}: {fp['passed']}/{fp['total']}")
        toks = _trend_tokens(ci.get("trend", []))
        if toks:
            L.append("  trend (vs önceki run): " + ", ".join(toks))
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
    cj = d.get("cron_jobs") or {}
    if cj.get("bad"):
        parts.append(f"<b>Cron ({len(cj['bad'])} sorunlu / {len(cj['jobs'])}):</b>")
        for j in cj["bad"]:
            parts.append(f"  ⚠ <code>{j['job']}</code> {j['result']} (rc={j['rc']}) {(j.get('detail') or '')[:50]}")
    elif cj.get("jobs"):
        parts.append(f"<b>Cron:</b> ✓ {len(cj['jobs'])} iş pass")
    lv = d.get("liveness") or {}
    bad_lv = (lv.get("dead") or []) + (lv.get("stale") or [])
    if bad_lv:
        parts.append(f"<b>Liveness ({len(lv.get('dead') or [])} ölü / {len(lv.get('stale') or [])} stale):</b>")
        for r in bad_lv:
            parts.append(f"  {'☠' if r['status'] == 'dead' else '⚠'} <code>{r['source']}</code> {r['detail'][:50]}")
    s = d["system"]
    parts.append(f"<b>Sistem:</b> {s['service']} | disk {s['disk_used_pct']} | ram {s['mem_used_mb']}/{s['mem_total_mb']}MB")
    v = d.get("vps") or {}
    if v:
        if v.get("online"):
            parts.append(
                f"<b>VPS:</b> cpu {v['cpu']:.0f}% | ram {v['mem']:.0f}% | "
                f"disk {v['disk']:.0f}% | {v['containers_up']}/{v['containers_total']} container"
            )
        else:
            parts.append("<b>VPS:</b> ✗ erişilemiyor")
    ci = d.get("ci") or {}
    if ci:
        age = "?" if ci["age_days"] is None else f"{ci['age_days']}g önce"
        stale = " ⚠ BAYAT" if ci.get("stale") else ""
        fp = ci.get("failing_projects", [])
        fp_note = (" — fail: " + ", ".join(p["project"] for p in fp)) if fp else ""
        parts.append(
            f"<b>CI:</b> {ci['started_at'][:10]} ({age}{stale}) | {ci['passed']}/{ci['total']} geçti, {ci['failed']} fail{fp_note}"
        )
        toks = _trend_tokens(ci.get("trend", []))
        if toks:
            parts.append("  <i>trend:</i> " + ", ".join(toks))
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
        "cron_jobs": cron_outcomes_health(),
        "liveness": _liveness_health(),
        "system": system_health(),
        "vps": vps_health(),
        "ci": ci_health(),
    }
