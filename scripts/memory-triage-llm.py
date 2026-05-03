"""LLM-based discovery triage agent (Anthropic Claude Haiku).

For each active discovery older than 14 days with read_count=0,
ask LLM to classify: keep / obsolete / superseded / merge_candidate.

Cheap (Haiku 4.5), batch limited to 20 per run.
Updates discoveries.status accordingly.

Triggered: cron 03:30 daily (after rule-based triage 03:15).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# anthropic SDK installed in /opt/linux-ai-server/venv
try:
    import anthropic
except ImportError:
    print("FATAL: anthropic SDK not installed. /opt/linux-ai-server/venv/bin/pip install anthropic", file=sys.stderr)
    sys.exit(0)

DB = "/opt/linux-ai-server/data/claude_memory.db"
LOG = "/opt/linux-ai-server/data/hook-logs/triage-llm.log"
ENV_FILE = "/opt/linux-ai-server/.env"
MAX_BATCH = 20
MIN_AGE_DAYS = 14
MODEL = "claude-haiku-4-5"


def load_env():
    if "ANTHROPIC_API_KEY" in os.environ:
        return os.environ["ANTHROPIC_API_KEY"]
    try:
        for line in Path(ENV_FILE).read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def log(msg: str):
    try:
        Path(LOG).parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.utcnow().isoformat()}] {msg}\n")
    except Exception:
        pass


def triage_one(client: anthropic.Anthropic, disc: dict) -> str:
    """Return: keep | obsolete | superseded | unknown"""
    prompt = f"""Discover (otonom hafiza sistemi kaydi):
Project: {disc.get('project','')}
Type: {disc.get('type','')}
Title: {disc.get('title','')[:100]}
Details: {(disc.get('details') or '')[:400]}
Olusturma: {disc.get('date','')}
Yas: {disc.get('age_days')} gun
Hic okunmadi (read_count=0).

Karar:
- keep: hala gecerli, action gerekiyor (yapilmasi planlanan is veya devam eden teknik ders)
- obsolete: artik gecerli degil (tamamlanmis veya gecersiz hale gelmis). Architecture turu icin: aciklamada degistirildigi/yerine yeni gectigi belirtilen servis (orn. "Coolify ile yonetiliyor" ama baska kayitta "Dokploy'a tasindi" gecmis) — obsolete say.
- superseded: yenilesi gelmis bir kayit olabilir (eski cozum, yeni yaklasimla degistirilmiştir)

Architecture icin emin degilsen "keep" sec — yanlis silmek yanlis tutmaktan daha pahali.

Sadece tek kelime: keep, obsolete, veya superseded"""

    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        decision = msg.content[0].text.strip().lower().split()[0]
        if decision in ("keep", "obsolete", "superseded"):
            return decision
        return "unknown"
    except Exception as e:
        log(f"LLM call fail for id={disc.get('id')}: {e}")
        return "unknown"


def main():
    api_key = load_env()
    if not api_key:
        log("FATAL: ANTHROPIC_API_KEY not found")
        sys.exit(0)

    client = anthropic.Anthropic(api_key=api_key)

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
        decision = triage_one(client, r)
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
