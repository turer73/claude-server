#!/usr/bin/env python3
"""Predictive Agent — Gelecek sorunları tahmin et, proactive alert üret.

metrics_history tablosundan son 7 günün verilerini analiz eder. Basit linear regression
ile trend analizi yapar. Disk/CPU/Memory trendi kritik eşiğe yaklaşıyorsa → proactive
alert (type=learning) → SessionStart okur + notify-cron Telegram'a çevirir.

Tasarım:
- Salt-okunur: metrics_history tablosunu okur, events'e yazar
- Deterministik: LLM gerekmez (basit linear regression)
- Fail-safe: DB hatası → OUTCOME:fail, crash yok
- Cron: günlük (06:00, daily-backup'ten sonra)

Çıktı formatı (OUTCOME marker cron-wrap için):
- OUTCOME: pass | N trend tespit edildi, M proactive alert
- OUTCOME: partial | N trend tespit edildi, alert yazılamadı: <err>
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

DAYS = int(os.environ.get("PREDICTIVE_DAYS", "7"))
DISK_THRESHOLD = float(os.environ.get("PREDICTIVE_DISK_THRESHOLD", "85.0"))
CPU_THRESHOLD = float(os.environ.get("PREDICTIVE_CPU_THRESHOLD", "80.0"))
MEMORY_THRESHOLD = float(os.environ.get("PREDICTIVE_MEMORY_THRESHOLD", "80.0"))


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


def _linear_regression(x: list[float], y: list[float]) -> tuple[float, float]:
    """Basit linear regression: y = slope * x + intercept. Returns (slope, intercept)."""
    n = len(x)
    if n < 2:
        return 0.0, y[0] if n == 1 else 0.0

    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    sum_x2 = sum(xi * xi for xi in x)

    denominator = n * sum_x2 - sum_x * sum_x
    if denominator == 0:
        return 0.0, sum_y / n if n > 0 else 0.0

    slope = (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n

    return slope, intercept


def analyze_metrics(days: int = DAYS, db_path: str | None = None) -> list[dict] | None:
    """metrics_history tablosundan son N günün verilerini analiz et.

    Returns: [{metric, current_value, trend_slope, days_to_threshold, threshold}]
    None: DB okuma hatası
    """
    db = db_path or SERVER_DB
    try:
        con = get_conn(db, readonly=True, busy_timeout_ms=5000)
    except sqlite3.Error:
        return None
    if not con:
        return None

    try:
        window = f"-{days} days"
        now_utc = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        rows = con.execute(
            """
            SELECT timestamp, cpu_usage, memory_usage, disk_usage
            FROM metrics_history
            WHERE datetime(timestamp) > datetime(?, ?)
            ORDER BY timestamp ASC
            """,
            (now_utc, window),
        ).fetchall()

        if not rows:
            return []

        timestamps = []
        cpu_values = []
        memory_values = []
        disk_values = []

        for i, r in enumerate(rows):
            timestamps.append(float(i))
            if r["cpu_usage"] is not None:
                cpu_values.append((i, r["cpu_usage"]))
            if r["memory_usage"] is not None:
                memory_values.append((i, r["memory_usage"]))
            if r["disk_usage"] is not None:
                disk_values.append((i, r["disk_usage"]))

        trends = []

        if disk_values:
            x_disk = [v[0] for v in disk_values]
            y_disk = [v[1] for v in disk_values]
            slope, intercept = _linear_regression(x_disk, y_disk)
            current_disk = disk_values[-1][1]

            if slope > 0 and current_disk < DISK_THRESHOLD:
                days_to_threshold = (DISK_THRESHOLD - current_disk) / slope if slope > 0 else float("inf")
                if days_to_threshold < 30:
                    trends.append({
                        "metric": "disk_usage",
                        "current_value": current_disk,
                        "trend_slope": slope,
                        "days_to_threshold": days_to_threshold,
                        "threshold": DISK_THRESHOLD,
                    })

        if cpu_values:
            x_cpu = [v[0] for v in cpu_values]
            y_cpu = [v[1] for v in cpu_values]
            slope, intercept = _linear_regression(x_cpu, y_cpu)
            current_cpu = cpu_values[-1][1]

            if slope > 0 and current_cpu < CPU_THRESHOLD:
                days_to_threshold = (CPU_THRESHOLD - current_cpu) / slope if slope > 0 else float("inf")
                if days_to_threshold < 30:
                    trends.append({
                        "metric": "cpu_usage",
                        "current_value": current_cpu,
                        "trend_slope": slope,
                        "days_to_threshold": days_to_threshold,
                        "threshold": CPU_THRESHOLD,
                    })

        if memory_values:
            x_mem = [v[0] for v in memory_values]
            y_mem = [v[1] for v in memory_values]
            slope, intercept = _linear_regression(x_mem, y_mem)
            current_mem = memory_values[-1][1]

            if slope > 0 and current_mem < MEMORY_THRESHOLD:
                days_to_threshold = (MEMORY_THRESHOLD - current_mem) / slope if slope > 0 else float("inf")
                if days_to_threshold < 30:
                    trends.append({
                        "metric": "memory_usage",
                        "current_value": current_mem,
                        "trend_slope": slope,
                        "days_to_threshold": days_to_threshold,
                        "threshold": MEMORY_THRESHOLD,
                    })

        return trends

    except sqlite3.Error:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def format_trend_summary(trends: list[dict]) -> str:
    """Trend listesini okunabilir özet haline getir."""
    if not trends:
        return "Proaktif uyarı yok."

    lines = []
    for t in trends[:10]:
        metric = t["metric"]
        current = t["current_value"]
        days = t["days_to_threshold"]
        threshold = t["threshold"]

        if days < 1:
            urgency = "🔴 ACİL"
        elif days < 3:
            urgency = "🟠 YÜKSEK"
        elif days < 7:
            urgency = "🟡 ORTA"
        else:
            urgency = "🟢 DÜŞÜK"

        lines.append(f"{urgency} {metric}: %{current:.1f} → %{threshold:.0f} eşiğine {days:.1f} gün")

    return "\n".join(lines)


def emit_event(trends: list[dict], ikey: str) -> str:
    """Proactive alert event emit et (notify-cron Telegram'a çevirir)."""
    if not ikey:
        return "no INTERNAL_API_KEY"
    if not trends:
        return "no trends"

    summary = format_trend_summary(trends)
    min_days = min(t["days_to_threshold"] for t in trends)

    if min_days < 1:
        severity = "critical"
        title = "🔴 Proaktif Uyarı: Kritik eşik <1 gün"
    elif min_days < 3:
        severity = "critical"
        title = "🟠 Proaktif Uyarı: Kritik eşik <3 gün"
    elif min_days < 7:
        severity = "warning"
        title = "🟡 Proaktif Uyarı: Trend analizi"
    else:
        severity = "info"
        title = "🟢 Proaktif Uyarı: Düşük öncelik"

    detail = (
        f"Predictive trend analizi (son {DAYS} gün):\n\n"
        f"{summary}\n\n"
        f"--- Öneri ---\n"
        f"Bu trendler devam ederse kritik eşikler aşılacak. "
        f"Önlem almak için kaynak tahsisi veya optimizasyon değerlendir."
    )[:3800]

    try:
        _post_json(
            f"{API_BASE}/api/v1/events",
            {
                "type": "predictive:alert",
                "source": "predictive-agent",
                "title": title,
                "severity": severity,
                "detail": detail,
            },
            {"X-API-Key": ikey},
            15,
        )
        return ""
    except Exception as e:
        return str(e)[:150]


def main() -> int:
    trends = analyze_metrics()
    ikey = _envget("INTERNAL_API_KEY")

    if trends is None:
        print(f"OUTCOME: fail | metrics_history DB okuma hatası (DB: {SERVER_DB})")
        return 1

    if not trends:
        print(f"OUTCOME: pass | Proaktif uyarı yok (son {DAYS} gün)")
        return 0

    err = emit_event(trends, ikey)
    if err:
        print(f"OUTCOME: partial | {len(trends)} trend tespit edildi, event emit edilemedi: {err}")
    else:
        print(f"OUTCOME: pass | {len(trends)} proaktif trend tespit edildi, event emit edildi")
    return 0


if __name__ == "__main__":
    sys.exit(main())
