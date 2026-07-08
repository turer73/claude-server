#!/usr/bin/env python3
"""Pattern Recognition Agent — Bilinç düşüncelerinde tekrar eden pattern'leri tespit et.

ConsciousnessStream'in thoughts tablosunu analiz eder (son 24h). Aynı focus ≥eşik kez
tekrar ediyorsa → "recurring_pattern" discovery (type=learning) → SessionStart okur.

Tasarım:
- Salt-okunur: thoughts tablosunu okur, discoveries'e yazar
- Deterministik: LLM gerekmez (basit SQL + threshold)
- Fail-safe: DB hatası → OUTCOME:fail, crash yok
- Cron: günlük (03:45, memory-triage'den sonra)

Çıktı formatı (OUTCOME marker cron-wrap için):
- OUTCOME: pass | N pattern tespit edildi
- OUTCOME: partial | N pattern, discovery yazılamadı: <err>
- OUTCOME: fail | <hata>
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.data_layer import get_conn

ENV_FILE = os.environ.get("NOTIFY_ENV_FILE", "/opt/linux-ai-server/.env")
API_BASE = os.environ.get("API_BASE", "http://localhost:8420")
MEMORY_DB = os.environ.get("MEMORY_DB", "/opt/linux-ai-server/data/claude_memory.db")

THRESHOLD = int(os.environ.get("PATTERN_THRESHOLD", "5"))
HOURS = int(os.environ.get("PATTERN_HOURS", "24"))


def _envget(key: str) -> str:
    v = os.environ.get(key)
    if v:
        return v
    try:
        with open(ENV_FILE) as fh:
            for line in fh:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def _post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    import urllib.request

    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def analyze_patterns(hours: int = HOURS, threshold: int = THRESHOLD, db_path: str | None = None) -> list[dict]:
    """thoughts tablosundan son N saatteki tekrar eden focus'ları bul.

    Returns: [{focus, count, emotion_distribution, sample_contents}]
    """
    db = db_path or MEMORY_DB
    con = get_conn(db, readonly=True, busy_timeout_ms=5000)
    if not con:
        return []

    try:
        window = f"-{hours} hours"
        rows = con.execute(
            """
            SELECT focus, emotion, content, timestamp
            FROM thoughts
            WHERE timestamp > datetime('now', ?)
            ORDER BY timestamp DESC
            """,
            (window,),
        ).fetchall()

        if not rows:
            return []

        focus_groups: dict[str, dict] = {}
        for r in rows:
            focus = r["focus"]
            emotion = r["emotion"]
            content = r["content"]

            if focus not in focus_groups:
                focus_groups[focus] = {
                    "focus": focus,
                    "count": 0,
                    "emotions": {},
                    "samples": [],
                }

            g = focus_groups[focus]
            g["count"] += 1
            g["emotions"][emotion] = g["emotions"].get(emotion, 0) + 1
            if len(g["samples"]) < 3:
                g["samples"].append(content[:200])

        patterns = [g for g in focus_groups.values() if g["count"] >= threshold]
        patterns.sort(key=lambda p: p["count"], reverse=True)
        return patterns

    except sqlite3.Error:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def format_pattern_summary(patterns: list[dict]) -> str:
    """Pattern listesini okunabilir özet haline getir."""
    if not patterns:
        return "Tekrar eden pattern yok."

    lines = []
    for p in patterns[:10]:
        focus = p["focus"]
        count = p["count"]
        emotions = ", ".join(f"{e}:{c}" for e, c in sorted(p["emotions"].items(), key=lambda x: -x[1])[:3])
        sample = p["samples"][0] if p["samples"] else "(örnek yok)"
        lines.append(f"- {focus}: {count} kez ({emotions})")
        lines.append(f"  Örnek: {sample[:120]}")

    return "\n".join(lines)


def write_discovery(patterns: list[dict], mkey: str) -> str:
    """recurring_pattern discovery yaz (type=learning, skip_dedup, tarih-unique)."""
    if not mkey:
        return "no MEMORY_API_KEY"
    if not patterns:
        return "no patterns"

    day_tag = datetime.now(UTC).strftime("%Y-%m-%d")
    summary = format_pattern_summary(patterns)
    body = (
        f"🔁 Tekrar Eden Pattern'ler ({day_tag}, son {HOURS}h)\n\n"
        f"{len(patterns)} focus ≥{THRESHOLD} kez tekrar etti:\n\n"
        f"{summary}\n\n"
        f"--- Öneri ---\n"
        f"Bu pattern'lerin kök nedenini analiz et. Özellikle 'concerned'/'restless' "
        f"duygularıyla tekrar edenler acil dikkat gerektirir."
    )[:3800]

    try:
        _post_json(
            f"{API_BASE}/api/v1/memory/discoveries",
            {
                "device_name": "klipper",
                "project": "linux-ai-server",
                "type": "learning",
                "skip_dedup": True,
                "title": f"Tekrar Eden Pattern'ler — {day_tag}",
                "details": body,
                "rationale": f"pattern-recognition.py — thoughts analizi, {len(patterns)} pattern ≥{THRESHOLD}x, salt-okunur.",
            },
            {"X-Memory-Key": mkey},
            15,
        )
        return ""
    except Exception as e:
        return str(e)[:150]


def main() -> int:
    patterns = analyze_patterns()
    mkey = _envget("MEMORY_API_KEY")

    if not patterns:
        print(f"OUTCOME: pass | Tekrar eden pattern yok (son {HOURS}h, eşik ≥{THRESHOLD})")
        return 0

    err = write_discovery(patterns, mkey)
    if err:
        print(f"OUTCOME: partial | {len(patterns)} pattern tespit edildi, discovery yazılamadı: {err}")
    else:
        print(f"OUTCOME: pass | {len(patterns)} tekrar eden pattern tespit edildi, discovery yazıldı")
    return 0


if __name__ == "__main__":
    sys.exit(main())
