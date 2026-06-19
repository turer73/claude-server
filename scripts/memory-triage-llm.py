"""LLM-based discovery triage agent (Claude Haiku — MAX-ABONELİK CLI).

For each active discovery older than 14 days with read_count=0,
ask LLM to classify: keep / obsolete / superseded / merge_candidate.

Cheap (Haiku 4.5), batch limited to 20 per run.
Updates discoveries.status accordingly.

Triggered: cron 03:30 daily (after rule-based triage 03:15).

NOT: Anthropic API SDK (pay-as-you-go) DEĞİL, `claude` CLI (Max-abonelik OAuth) kullanır —
kullanıcı standing tercihi "API istemiyorum". API-key env'i strip edilir (abonelik kimliği zorunlu).
"""

from __future__ import annotations

import json as _json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DB = "/opt/linux-ai-server/data/claude_memory.db"
LOG = "/opt/linux-ai-server/data/hook-logs/triage-llm.log"
MAX_BATCH = 20
MIN_AGE_DAYS = 14
MODEL = "claude-haiku-4-5"


_CLAUDE_CANDIDATES = [
    os.path.expanduser("~/.npm-global/bin/claude"),
    "/usr/bin/claude",
    "/usr/local/bin/claude",
]


def _find_claude() -> str | None:
    for p in _CLAUDE_CANDIDATES:
        if os.path.exists(p):
            return p
    return shutil.which("claude")


def _claude_env() -> dict:
    # MAX-PLAN ZORUNLU (kullanıcı: "API istemiyorum"): API-key/auth-token strip → claude CLI
    # her zaman abonelik kimliğine (~/.claude OAuth) düşer, pay-per-token API'ye ASLA.
    env = {**os.environ}
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def _claude_cli(binary: str, prompt: str, model: str, timeout: int = 60) -> str | None:
    """claude CLI ile tool-suz salt-üretim (-p headless, JSON çıktı). Fail → None."""
    try:
        proc = subprocess.run(
            [binary, "-p", prompt, "--model", model, "--output-format", "json"],
            capture_output=True,
            text=True,
            env=_claude_env(),
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
        if proc.returncode != 0:
            log(f"claude cli rc={proc.returncode}: {(proc.stderr or '')[:200]}")
            return None
        data = _json.loads(proc.stdout or "{}")
        if data.get("is_error"):
            log(f"claude cli error: {str(data.get('result'))[:200]}")
            return None
        return str(data.get("result", "")).strip()
    except Exception as e:
        log(f"claude cli fail: {e}")
        return None


def log(msg: str):
    try:
        Path(LOG).parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.utcnow().isoformat()}] {msg}\n")
    except Exception:
        pass


def triage_one(binary: str, disc: dict) -> str:
    """Return: keep | obsolete | superseded | unknown"""
    prompt = f"""Discover (otonom hafiza sistemi kaydi):
Project: {disc.get("project", "")}
Type: {disc.get("type", "")}
Title: {disc.get("title", "")[:100]}
Details: {(disc.get("details") or "")[:400]}
Olusturma: {disc.get("date", "")}
Yas: {disc.get("age_days")} gun
Hic okunmadi (read_count=0).

Karar:
- keep: hala gecerli, action gerekiyor (yapilmasi planlanan is veya devam eden teknik ders)
- obsolete: artik gecerli degil (tamamlanmis veya gecersiz hale gelmis). Architecture turu icin: aciklamada degistirildigi/yerine yeni gectigi belirtilen servis (orn. "Coolify ile yonetiliyor" ama baska kayitta "Dokploy'a tasindi" gecmis) — obsolete say.
- superseded: yenilesi gelmis bir kayit olabilir (eski cozum, yeni yaklasimla degistirilmiştir)

Architecture icin emin degilsen "keep" sec — yanlis silmek yanlis tutmaktan daha pahali.

Sadece tek kelime: keep, obsolete, veya superseded"""

    out = _claude_cli(binary, prompt, MODEL)
    if not out:
        return "unknown"
    decision = out.strip().lower().split()[0] if out.strip() else "unknown"
    return decision if decision in ("keep", "obsolete", "superseded") else "unknown"


def main():
    binary = _find_claude()
    if not binary:
        log("FATAL: claude CLI bulunamadı (Max-abonelik gerekli; API kullanılmaz)")
        sys.exit(0)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Two streams:
    #  - learning/workaround/plan/config: read_count=0 AND age > MIN_AGE_DAYS
    #  - architecture: age > 30d regardless of read_count (state snapshots
    #    drift silently when infra changes — bkz. Coolify->Dokploy gecisi
    #    sonrasi 24+ gun stale memory'ler)
    cur.execute(
        f"""
        SELECT id, project, type, title, details,
               date(created_at) as date,
               CAST(julianday('now') - julianday(created_at) AS INT) as age_days
        FROM discoveries
        WHERE status='active'
          AND (
            (read_count=0
             AND type IN ('learning','workaround','plan','config')
             AND julianday('now') - julianday(created_at) > {MIN_AGE_DAYS})
            OR
            (type='architecture'
             AND julianday('now') - julianday(created_at) > 30)
          )
        ORDER BY created_at ASC
        LIMIT {MAX_BATCH}
        """
    )
    rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        log("no candidates (learning/workaround/plan/config age>14d unread, OR architecture age>30d)")
        conn.close()
        return

    counts = {"keep": 0, "obsolete": 0, "superseded": 0, "unknown": 0}
    for r in rows:
        decision = triage_one(binary, r)
        counts[decision] = counts.get(decision, 0) + 1
        if decision in ("obsolete", "superseded"):
            cur.execute(
                "UPDATE discoveries SET status=? WHERE id=? AND status='active'",
                (decision, r["id"]),
            )
            log(f"id={r['id']} {decision}: {r['title'][:60]}")
        time.sleep(0.5)  # gentle pacing

    conn.commit()
    conn.close()
    log(f"batch complete: {counts}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
    sys.exit(0)
