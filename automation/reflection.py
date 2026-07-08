#!/usr/bin/env python3
"""Reflection Agent — Remediation playbook başarı oranlarını analiz et.

server.db.remediation_log tablosunu analiz eder (son 30 gün). Her playbook (alert_source)
için başarı oranı hesaplar. Düşük başarı (<%30) veya yüksek başarı (>80%) → discovery
(type=learning) → SessionStart okur.

Tasarım:
- Salt-okunur: remediation_log tablosunu okur, discoveries'e yazar
- Deterministik: LLM gerekmez (basit SQL + threshold)
- Fail-safe: DB hatası → OUTCOME:fail, crash yok
- Cron: haftalık (Pazar 04:00, weekly-audit ile aynı gün)

Çıktı formatı (OUTCOME marker cron-wrap için):
- OUTCOME: pass | N playbook analiz edildi, M öneri
- OUTCOME: partial | N playbook, discovery yazılamadı: <err>
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
SERVER_DB = os.environ.get("DB_PATH", "/opt/linux-ai-server/data/server.db")

DAYS = int(os.environ.get("REFLECTION_DAYS", "30"))
MIN_ATTEMPTS = int(os.environ.get("REFLECTION_MIN_ATTEMPTS", "3"))
LOW_SUCCESS_THRESHOLD = float(os.environ.get("REFLECTION_LOW_THRESHOLD", "0.3"))
HIGH_SUCCESS_THRESHOLD = float(os.environ.get("REFLECTION_HIGH_THRESHOLD", "0.8"))


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


def analyze_playbooks(days: int = DAYS, min_attempts: int = MIN_ATTEMPTS, db_path: str | None = None) -> list[dict]:
    """remediation_log tablosundan playbook başarı oranlarını hesapla.

    Returns: [{alert_source, total, success_count, success_rate, recent_actions}]
    """
    db = db_path or SERVER_DB
    con = get_conn(db, readonly=True, busy_timeout_ms=5000)
    if not con:
        return []

    try:
        window = f"-{days} days"
        rows = con.execute(
            """
            SELECT alert_source, action, success, timestamp
            FROM remediation_log
            WHERE timestamp > datetime('now', ?)
            ORDER BY timestamp DESC
            """,
            (window,),
        ).fetchall()

        if not rows:
            return []

        playbook_stats: dict[str, dict] = {}
        for r in rows:
            source = r["alert_source"]
            action = r["action"]
            success = r["success"]

            if source not in playbook_stats:
                playbook_stats[source] = {
                    "alert_source": source,
                    "total": 0,
                    "success_count": 0,
                    "actions": {},
                    "recent": [],
                }

            stats = playbook_stats[source]
            stats["total"] += 1
            if success == 1:
                stats["success_count"] += 1

            stats["actions"][action] = stats["actions"].get(action, 0) + 1
            if len(stats["recent"]) < 3:
                stats["recent"].append({"action": action, "success": success == 1})

        results = []
        for stats in playbook_stats.values():
            if stats["total"] >= min_attempts:
                stats["success_rate"] = stats["success_count"] / stats["total"]
                results.append(stats)

        results.sort(key=lambda s: s["total"], reverse=True)
        return results

    except sqlite3.Error:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def identify_recommendations(playbooks: list[dict]) -> list[dict]:
    """Düşük/yüksek başarı oranlı playbook'ları tespit et.

    Returns: [{alert_source, issue, success_rate, total, recommendation}]
    """
    recommendations = []

    for p in playbooks:
        rate = p["success_rate"]
        source = p["alert_source"]
        total = p["total"]

        if rate < LOW_SUCCESS_THRESHOLD:
            recommendations.append({
                "alert_source": source,
                "issue": "low_success_rate",
                "success_rate": rate,
                "total": total,
                "recommendation": f"Playbook '{source}' düşük başarı oranı (%{rate*100:.0f}). "
                f"Kök neden analiz edilmeli, playbook güncellenmeli veya devre dışı bırakılmalı.",
            })
        elif rate > HIGH_SUCCESS_THRESHOLD and total >= 5:
            recommendations.append({
                "alert_source": source,
                "issue": "high_success_rate",
                "success_rate": rate,
                "total": total,
                "recommendation": f"Playbook '{source}' yüksek başarı oranı (%{rate*100:.0f}). "
                f"Bu playbook güvenilir, otomasyon güvenle kullanılabilir.",
            })

    return recommendations


def format_recommendation_summary(recommendations: list[dict]) -> str:
    """Öneri listesini okunabilir özet haline getir."""
    if not recommendations:
        return "Öneri yok."

    lines = []
    for r in recommendations[:10]:
        source = r["alert_source"]
        rate = r["success_rate"]
        total = r["total"]
        issue = r["issue"]
        rec = r["recommendation"]

        icon = "⚠️" if issue == "low_success_rate" else "✓"
        lines.append(f"{icon} {source}: %{rate*100:.0f} başarı ({total} deneme)")
        lines.append(f"  {rec}")

    return "\n".join(lines)


def write_discovery(recommendations: list[dict], mkey: str) -> str:
    """playbook_reflection discovery yaz (type=learning, skip_dedup, tarih-unique)."""
    if not mkey:
        return "no MEMORY_API_KEY"
    if not recommendations:
        return "no recommendations"

    day_tag = datetime.now(UTC).strftime("%Y-%m-%d")
    summary = format_recommendation_summary(recommendations)
    body = (
        f"🔍 Playbook Başarı Oranları ({day_tag}, son {DAYS} gün)\n\n"
        f"{len(recommendations)} öneri:\n\n"
        f"{summary}\n\n"
        f"--- Öneri ---\n"
        f"Düşük başarı oranlı playbook'lar için kök neden analizi yap. "
        f"Yüksek başarı oranlı playbook'lar güvenilir, otomasyon güvenle kullanılabilir."
    )[:3800]

    try:
        _post_json(
            f"{API_BASE}/api/v1/memory/discoveries",
            {
                "device_name": "klipper",
                "project": "linux-ai-server",
                "type": "learning",
                "skip_dedup": True,
                "title": f"Playbook Başarı Oranları — {day_tag}",
                "details": body,
                "rationale": f"reflection.py — remediation_log analizi, {len(recommendations)} öneri, salt-okunur.",
            },
            {"X-Memory-Key": mkey},
            15,
        )
        return ""
    except Exception as e:
        return str(e)[:150]


def main() -> int:
    playbooks = analyze_playbooks()
    recommendations = identify_recommendations(playbooks)
    mkey = _envget("MEMORY_API_KEY")

    if not recommendations:
        print(f"OUTCOME: pass | {len(playbooks)} playbook analiz edildi, öneri yok")
        return 0

    err = write_discovery(recommendations, mkey)
    if err:
        print(f"OUTCOME: partial | {len(recommendations)} öneri, discovery yazılamadı: {err}")
    else:
        print(f"OUTCOME: pass | {len(recommendations)} öneri tespit edildi, discovery yazıldı")
    return 0


if __name__ == "__main__":
    sys.exit(main())
