#!/usr/bin/env python3
"""Meta-Cognition Agent — Düşünce kalitesini değerlendir, confidence score hesapla.

ConsciousnessStream'in thoughts tablosunu analiz eder (son 24h). Düşüncelerin tutarlılığını,
duygu dağılımını ve içerik çeşitliliğini değerlendirir. Düşük confidence score → "need_more_data"
event emit eder (notify-cron Telegram'a çevirir).

Tasarım:
- Salt-okunur: thoughts tablosunu okur, events'e yazar
- Deterministik: LLM gerekmez (basit istatistik + heuristic)
- Fail-safe: DB hatası → OUTCOME:fail, crash yok
- Cron: günlük (04:00, pattern-recognition'dan sonra)

Çıktı formatı (OUTCOME marker cron-wrap için):
- OUTCOME: pass | Confidence score: X.XX, N thought analiz edildi
- OUTCOME: partial | Confidence score: X.XX, event emit edilemedi: <err>
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

HOURS = int(os.environ.get("META_COGNITION_HOURS", "24"))
LOW_CONFIDENCE_THRESHOLD = float(os.environ.get("META_COGNITION_LOW_THRESHOLD", "0.5"))


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


def analyze_thought_quality(hours: int = HOURS, db_path: str | None = None) -> dict | None:
    """thoughts tablosundan son N saatteki düşüncelerin kalitesini analiz et.

    Returns: {confidence_score, total_thoughts, unique_focuses, emotion_distribution, issues}
    None: DB okuma hatası
    """
    db = db_path or MEMORY_DB
    try:
        con = get_conn(db, readonly=True, busy_timeout_ms=5000)
    except sqlite3.Error:
        return None
    if not con:
        return None

    try:
        window = f"-{hours} hours"
        now_utc = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        rows = con.execute(
            """
            SELECT focus, emotion, content, timestamp
            FROM thoughts
            WHERE datetime(timestamp) > datetime(?, ?)
            ORDER BY timestamp DESC
            """,
            (now_utc, window),
        ).fetchall()

        if not rows:
            return {
                "confidence_score": 0.0,
                "total_thoughts": 0,
                "unique_focuses": 0,
                "emotion_distribution": {},
                "issues": ["No thoughts in time window"],
            }

        total = len(rows)
        focuses = set()
        emotions = {}
        content_lengths = []
        idle_calm_count = 0

        for r in rows:
            focus = r["focus"]
            emotion = r["emotion"]
            content = r["content"]

            focuses.add(focus)
            emotions[emotion] = emotions.get(emotion, 0) + 1
            content_lengths.append(len(content))

            if focus == "idle" and emotion == "calm":
                idle_calm_count += 1

        unique_focuses = len(focuses)
        avg_content_length = sum(content_lengths) / len(content_lengths) if content_lengths else 0

        issues = []
        confidence_score = 1.0

        if idle_calm_count / total > 0.8:
            issues.append("High idle/calm ratio (>80%)")
            confidence_score -= 0.3

        if unique_focuses < 3 and total > 10:
            issues.append("Low focus diversity")
            confidence_score -= 0.2

        if avg_content_length < 50:
            issues.append("Short content length")
            confidence_score -= 0.1

        dominant_emotion = max(emotions.values()) if emotions else 0
        if dominant_emotion / total > 0.9:
            issues.append("Emotion monotony")
            confidence_score -= 0.2

        if total < 10:
            issues.append("Low thought count")
            confidence_score -= 0.2

        confidence_score = max(0.0, confidence_score)

        return {
            "confidence_score": confidence_score,
            "total_thoughts": total,
            "unique_focuses": unique_focuses,
            "emotion_distribution": emotions,
            "avg_content_length": avg_content_length,
            "idle_calm_ratio": idle_calm_count / total if total > 0 else 0,
            "issues": issues,
        }

    except sqlite3.Error:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def format_quality_summary(quality: dict) -> str:
    """Kalite analizini okunabilir özet haline getir."""
    score = quality["confidence_score"]
    total = quality["total_thoughts"]
    focuses = quality["unique_focuses"]
    issues = quality["issues"]

    if score >= 0.8:
        status = "✅ YÜKSEK"
    elif score >= 0.6:
        status = "🟡 ORTA"
    elif score >= 0.4:
        status = "🟠 DÜŞÜK"
    else:
        status = "🔴 KRİTİK"

    lines = [
        f"{status} Confidence Score: {score:.2f}",
        f"Toplam thought: {total}, Unique focus: {focuses}",
    ]

    if issues:
        lines.append("Sorunlar:")
        for issue in issues[:5]:
            lines.append(f"  - {issue}")

    return "\n".join(lines)


def emit_event(quality: dict, ikey: str) -> str:
    """Düşük confidence score → need_more_data event emit et."""
    if not ikey:
        return "no INTERNAL_API_KEY"

    score = quality["confidence_score"]
    if score >= LOW_CONFIDENCE_THRESHOLD:
        return "confidence adequate"

    summary = format_quality_summary(quality)
    detail = (
        f"Meta-Cognition analizi (son {HOURS}h):\n\n"
        f"{summary}\n\n"
        f"--- Öneri ---\n"
        f"Düşük confidence score: düşünce kalitesi yetersiz. "
        f"Daha fazla veri toplamak için ConsciousnessStream interval'ı düşürülebilir "
        f"veya ek state reader'lar eklenebilir."
    )[:3800]

    try:
        _post_json(
            f"{API_BASE}/api/v1/events",
            {
                "type": "meta-cognition:low-confidence",
                "source": "meta-cognition-agent",
                "title": "🧠 Meta-Cognition: Düşük düşünce kalitesi",
                "severity": "warning",
                "detail": detail,
            },
            {"X-API-Key": ikey},
            15,
        )
        return ""
    except Exception as e:
        return str(e)[:150]


def main() -> int:
    quality = analyze_thought_quality()
    ikey = _envget("INTERNAL_API_KEY")

    if quality is None:
        print(f"OUTCOME: fail | thoughts DB okuma hatası (DB: {MEMORY_DB})")
        return 1

    score = quality["confidence_score"]
    total = quality["total_thoughts"]

    if total == 0:
        print(f"OUTCOME: pass | Confidence score: {score:.2f}, thought yok (son {HOURS}h)")
        return 0

    err = emit_event(quality, ikey)
    if err and "confidence adequate" not in err:
        print(f"OUTCOME: partial | Confidence score: {score:.2f}, event emit edilemedi: {err}")
    elif "confidence adequate" in err:
        print(f"OUTCOME: pass | Confidence score: {score:.2f}, {total} thought analiz edildi")
    else:
        print(f"OUTCOME: pass | Confidence score: {score:.2f}, {total} thought analiz edildi, event emit edildi")
    return 0


if __name__ == "__main__":
    sys.exit(main())
