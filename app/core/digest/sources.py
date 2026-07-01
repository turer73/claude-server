"""Digest veri-toplayıcıları (collectors) + sabitler + GitHub/CI/VPS/cron sağlık.

Tüm module-sabitleri burada (COVERAGE_DB_PATH/REVIEW_REPOS testlerde patch'lenir →
`app.core.digest.sources.<CONST>`). render+facade buradan import eder."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

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

REVIEW_REPOS = [
    "turer73/claude-server",
    "turer73/panola",
    "turer73/kuafor",
    "turer73/petvet",
    "turer73/bilge-arena",
    "turer73/renderhane",
    "turer73/koken-akademi",
]


def memory_delta(window_hours: int) -> dict[str, Any]:
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
        # Per-device okunmamış (#647): legacy global read=0 DEĞİL — sistem per-device
        # read_by'a geçti, klipper okuyunca read_by'a eklenir (read=1 değil) → global
        # sayaç ŞİŞİK çıkıyordu. Doğru: klipper'a/broadcast'e ait + klipper'ın read_by'da
        # OLMADIĞI notlar (SessionStart hook ile aynı semantik). read_by kolonu yoksa
        # (eski/minimal DB) legacy global'e düş — savunmacı (_has_merged_into deseni).
        _note_cols = {r[1] for r in db.execute("PRAGMA table_info(notes)").fetchall()}
        if "read_by" in _note_cols:
            _unread_q = (
                "SELECT id, title, content FROM notes "
                "WHERE (to_device='klipper' OR to_device IS NULL) "
                "AND COALESCE(read,0)=0 AND (read_by IS NULL OR read_by NOT LIKE '%|klipper|%') "
                "ORDER BY id DESC"
            )
        else:
            _unread_q = "SELECT id, title, content FROM notes WHERE COALESCE(read,0)=0 ORDER BY id DESC"
        unread_notes = [dict(r) for r in db.execute(_unread_q).fetchall()]
        return {"open_bugs": open_bugs, "new_bugs": new_bugs, "unread_notes": unread_notes}
    finally:
        db.close()


def github_commits(repo: str, since_iso: str, token: str | None = None, timeout: float = 4.0) -> list[dict[str, Any]]:
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


def all_commits(window_hours: int, token: str | None = None) -> dict[str, list[dict[str, Any]]]:
    since_iso = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=window_hours)).isoformat(timespec="seconds")
    out: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=len(REPOS)) as ex:
        futs = {ex.submit(github_commits, repo, since_iso, token): proj for proj, repo in REPOS.items()}
        for fut in as_completed(futs):
            proj = futs[fut]
            try:
                out[proj] = fut.result()
            except Exception:
                out[proj] = []
    return out


def cron_health() -> dict[str, Any]:
    """Latest self-pentest run summary + age."""
    out: dict[str, Any] = {"self_pentest": None}
    if not PENTEST_LOG_ROOT.exists():
        return out
    days = sorted([p for p in PENTEST_LOG_ROOT.iterdir() if p.is_dir()], reverse=True)
    if not days:
        return out
    latest = days[0]
    age = (dt.date.today() - dt.date.fromisoformat(latest.name)).days
    summary = latest / "summary.tsv"
    findings: list[dict[str, Any]] = []
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


def cron_outcomes_health() -> dict[str, Any]:
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


def _gh_json(args: list[str], timeout: float = 8.0) -> Any:
    """gh CLI → parsed JSON. Hata/timeout/non-zero → None (fetch-fail ayırt edilir,
    sessiz-[] DEĞİL — kendi PR-poller'ımızda yakaladığımız Codex-P1 dersi)."""
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)  # noqa: S603,S607
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout or "null")
    except ValueError:
        return None


def _pr_ci_state(rollup: list[Any]) -> str:
    """statusCheckRollup → green | failing | pending | unknown. CheckRun(.status/
    .conclusion) + legacy StatusContext(.state) ikisini de değerlendirir."""
    if not rollup:
        return "unknown"

    def _m(item: Any, pat: str) -> bool:
        v = f"{item.get('conclusion') or ''} {item.get('state') or ''} {item.get('status') or ''}"
        return bool(re.search(pat, v, re.I))

    if any(_m(i, r"FAIL|ERROR|CANCELLED|TIMED_OUT") for i in rollup):
        return "failing"
    if any(_m(i, r"IN_PROGRESS|QUEUED|PENDING|EXPECTED") for i in rollup):
        return "pending"
    if any(_m(i, r"SUCCESS|NEUTRAL|SKIPPED") for i in rollup):
        return "green"
    return "unknown"


def pr_review_health() -> dict[str, Any]:
    """LIVESYS PR-review FAZ1 (ÜCRETSİZ): 7 repo açık PR + Codex-inline + CI durumu
    topla → digest review-triyaj sinyali. Pure observer (gh okuma; otonom Claude
    YOK = token-maliyeti yok). fetch-fail ayırt edilir (sessiz-sıfır değil)."""
    prs: list[dict[str, Any]] = []
    fetch_fail = False
    for repo in REVIEW_REPOS:
        data = _gh_json(["pr", "list", "-R", repo, "--state", "open", "--json", "number,title,isDraft,statusCheckRollup"])
        if data is None:
            fetch_fail = True
            continue
        for pr in data:
            if pr.get("isDraft"):
                continue
            num = pr["number"]
            ci = _pr_ci_state(pr.get("statusCheckRollup") or [])
            codex = _gh_json(
                [
                    "api",
                    f"repos/{repo}/pulls/{num}/comments",
                    "--jq",
                    '[.[]|select(.user.login=="chatgpt-codex-connector[bot]")]|length',
                ]
            )
            # Codex-fetch None (rate-limit/auth/timeout) = "0 yorum" DEĞİL "bilinmiyor":
            # codex=None + fetch_fail (sessiz "Codex-temiz" raporlamayi onle — Codex-P2).
            if codex is None:
                fetch_fail = True
                codex_val: int | None = None
            else:
                codex_val = codex if isinstance(codex, int) else 0
            prs.append(
                {
                    "repo": repo.split("/")[-1],
                    "num": num,
                    "title": (pr.get("title") or "")[:60],
                    "ci": ci,
                    "codex": codex_val,
                }
            )
    return {"prs": prs, "signaled": prs, "fetch_fail": fetch_fail}


def _liveness_health() -> dict[str, Any]:
    """LIVESYS Faz 2 liveness monitor (app.core.liveness). dead/stale kaynakları
    yüzeye çıkarır. Hata/yokluk halinde {} (dijest yine de üretilir)."""
    try:
        from app.core import liveness

        return liveness.check_all()
    except Exception:
        return {}


def system_health() -> dict[str, Any]:
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


def vps_health() -> dict[str, Any]:
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


def ci_health() -> dict[str, Any]:
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


def _ci_projects(row: Any) -> dict[str, Any]:
    """{project: passed_count} from a test_runs.details JSON column."""
    if row is None:
        return {}
    try:
        return {p: info.get("passed", 0) for p, info in json.loads(row["details"] or "{}").items()}
    except (ValueError, TypeError, AttributeError):
        return {}


def _project_trend(cur_row: Any, prev_row: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Per-project passed-count delta of the latest run vs the previous one.

    Returns (all_changes, regressions). Consecutive-run window keeps day-to-day
    deltas small; a project that drops count or vanishes is a regression (e.g.
    tests deleted or a project that stopped being tested), growth is info-only.
    """
    cur, prev = _ci_projects(cur_row), _ci_projects(prev_row)
    if not prev:
        return [], []
    changes: list[dict[str, Any]] = []
    for proj in sorted(set(cur) | set(prev)):
        if proj not in prev:
            changes.append({"project": proj, "kind": "new", "to": cur[proj]})
        elif proj not in cur:
            changes.append({"project": proj, "kind": "dropped", "from": prev[proj]})
        elif cur[proj] != prev[proj]:
            changes.append({"project": proj, "kind": "delta", "from": prev[proj], "to": cur[proj], "delta": cur[proj] - prev[proj]})
    regressions = [c for c in changes if c["kind"] == "dropped" or (c["kind"] == "delta" and c["delta"] < 0)]
    return changes, regressions
