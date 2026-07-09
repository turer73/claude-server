#!/usr/bin/env python3
"""Self-Improvement Agent — Kendi kodunu iyileştirme önerileri üret.

Pattern Recognition + Reflection sonuçlarını toplar, LLM (Sonnet) ile "Bu pattern'leri
düzeltmek için kod değişikliği öner" analizi yapar. Human-in-the-loop: Dashboard'da
"Önerilen Değişiklik" kartı gösterir, onay → Git branch + PR oluşturur.

Tasarım:
- Salt-okunur: discoveries + remediation_log + thoughts okur, events'e yazar
- LLM-based: Sonnet ile kod değişikliği önerisi üret
- Fail-safe: DB/LLM hatası → OUTCOME:fail, crash yok
- Cron: haftalık (Pazartesi 10:00, reflection'tan sonra)

Onay akışı:
1. self_improvement.py öneri üretir → server.db self_improvement_pending tablosuna kaydeder
2. Kullanıcı Dashboard'da onaylar → POST /api/v1/self-improvement/approve
3. automation/self-improvement-pr.sh branch + commit + gh pr create

Çıktı formatı (OUTCOME marker cron-wrap için):
- OUTCOME: pass | N öneri üretildi, M kaydedildi
- OUTCOME: partial | N öneri, DB yazma hatası: <err>
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
SERVER_DB = os.environ.get("DB_PATH", "/opt/linux-ai-server/data/server.db")

SONNET_MODEL = os.environ.get("SELF_IMPROVEMENT_MODEL", "claude-sonnet-4-6")

_PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS self_improvement_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    priority TEXT DEFAULT 'medium',
    affected_files TEXT,
    status TEXT DEFAULT 'pending',
    suggestion_json TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    approved_at TEXT,
    pr_url TEXT
);
"""


def _ensure_pending_table(server_db: str | None = None) -> None:
    srv_db = server_db or SERVER_DB
    try:
        con = get_conn(srv_db, busy_timeout_ms=10000)
        if con:
            con.executescript(_PENDING_SCHEMA)
            con.commit()
            con.close()
    except sqlite3.Error:
        pass


def _save_suggestion(suggestion: dict, server_db: str | None = None) -> str | None:
    """Öneriyi self_improvement_pending tablosuna kaydet. Döner: id (str) veya None."""
    srv_db = server_db or SERVER_DB
    try:
        con = get_conn(srv_db, busy_timeout_ms=5000)
        if not con:
            return None
        con.execute(
            "INSERT INTO self_improvement_pending (title, description, priority, affected_files, suggestion_json) VALUES (?, ?, ?, ?, ?)",
            (
                suggestion.get("title", "")[:50],
                (suggestion.get("description", "") or "")[:200],
                suggestion.get("priority", "medium"),
                (suggestion.get("affected_files", "") or "")[:500],
                json.dumps(suggestion, ensure_ascii=False),
            ),
        )
        con.commit()
        row_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.close()
        return str(row_id)
    except sqlite3.Error:
        return None


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


def collect_improvement_signals(db_path: str | None = None, server_db: str | None = None) -> dict | None:
    """Pattern + reflection + thought sinyallerini topla.

    Returns: {patterns: [...], low_success_playbooks: [...], low_confidence: [...]}
    None: DB okuma hatası
    """
    memory_db = db_path or MEMORY_DB
    srv_db = server_db or SERVER_DB

    signals = {
        "patterns": [],
        "low_success_playbooks": [],
        "low_confidence": [],
    }

    db_errors = 0

    try:
        con = get_conn(memory_db, readonly=True, busy_timeout_ms=5000)
        if not con:
            db_errors += 1
        else:
            rows = con.execute(
                """
                SELECT title, details, created_at
                FROM discoveries
                WHERE type='learning' AND title LIKE 'Tekrar Eden Pattern%'
                ORDER BY created_at DESC
                LIMIT 5
                """
            ).fetchall()

            for r in rows:
                signals["patterns"].append({
                    "title": r["title"],
                    "details": r["details"][:500],
                    "timestamp": r["created_at"],
                })

            con.close()
    except sqlite3.Error:
        db_errors += 1

    try:
        con = get_conn(srv_db, readonly=True, busy_timeout_ms=5000)
        if not con:
            db_errors += 1
        else:
            rows = con.execute(
                """
                SELECT alert_source, COUNT(*) as total, SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count
                FROM remediation_log
                WHERE executed = 1 AND timestamp > datetime('now', '-30 days')
                GROUP BY alert_source
                HAVING total >= 3 AND CAST(success_count AS FLOAT) / total < 0.3
                ORDER BY total DESC
                LIMIT 5
                """
            ).fetchall()

            for r in rows:
                rate = r["success_count"] / r["total"] if r["total"] > 0 else 0
                signals["low_success_playbooks"].append({
                    "alert_source": r["alert_source"],
                    "total": r["total"],
                    "success_rate": rate,
                })

            con.close()
    except sqlite3.Error:
        db_errors += 1

    try:
        con = get_conn(memory_db, readonly=True, busy_timeout_ms=5000)
        if not con:
            db_errors += 1
        else:
            rows = con.execute(
                """
                SELECT focus, emotion, content, timestamp
                FROM thoughts
                WHERE is_deep = 1 AND timestamp > datetime('now', '-7 days')
                ORDER BY timestamp DESC
                LIMIT 10
                """
            ).fetchall()

            for r in rows:
                signals["low_confidence"].append({
                    "focus": r["focus"],
                    "emotion": r["emotion"],
                    "content": r["content"][:300],
                    "timestamp": r["timestamp"],
                })

            con.close()
    except sqlite3.Error:
        db_errors += 1

    if db_errors >= 2:
        return None

    return signals


def generate_improvement_suggestions(signals: dict, ikey: str) -> list[dict] | None:
    """LLM (Sonnet) ile kod değişikliği önerileri üret.

    Returns: [{title, description, priority, affected_files}]
    None: LLM hatası
    """
    if not ikey:
        return None

    if not signals["patterns"] and not signals["low_success_playbooks"] and not signals["low_confidence"]:
        return []

    prompt = f"""Aşağıdaki sistem sinyallerini analiz et ve SOMUT kod değişikliği önerileri üret:

## Tekrar Eden Pattern'ler:
{json.dumps(signals["patterns"][:3], indent=2, ensure_ascii=False)}

## Düşük Başarı Oranlı Playbook'lar:
{json.dumps(signals["low_success_playbooks"][:3], indent=2, ensure_ascii=False)}

## Düşük Confidence Düşünceler:
{json.dumps(signals["low_confidence"][:3], indent=2, ensure_ascii=False)}

## Görev:
1. Bu sinyallerden YOLA ÇIKARAK somut kod değişikliği önerileri üret
2. Her öneri için:
   - title: Kısa başlık (50 char max)
   - description: Ne yapılmalı, neden (200 char max)
   - priority: high/medium/low
   - affected_files: Hangi dosyalar etkilenecek (örn: "automation/pattern_recognition.py")
3. Sadece GERÇEKTEN uygulanabilir öneriler üret (abstract fikirler değil)
4. JSON array olarak döndür: [{{"title": "...", "description": "...", "priority": "...", "affected_files": "..."}}]

Öneri yoksa boş array döndür: []"""

    try:
        result = _post_json(
            f"{API_BASE}/api/v1/claude/run",
            {
                "prompt": prompt,
                "read_only": True,
                "max_turns": 1,
                "model": SONNET_MODEL,
            },
            {"X-API-Key": ikey},
            180,
        )

        response_text = result.get("result", "").strip()

        if not response_text:
            return []

        import re
        json_match = re.search(r'\[[\s\S]*\]', response_text)
        if json_match:
            suggestions = json.loads(json_match.group())
            return suggestions if isinstance(suggestions, list) else []

        return []
    except Exception:
        return None


def emit_suggestion_event(suggestion: dict, ikey: str) -> str:
    """Öneri için event emit et (Dashboard'da gösterilir). Eskiden ana akıştı;
    şimdi pending tablosuna kaydetme öncelikli; event fallback olarak kalır."""
    if not ikey:
        return "no INTERNAL_API_KEY"

    title = suggestion.get("title", "Kod değişikliği önerisi")
    description = suggestion.get("description", "")
    priority = suggestion.get("priority", "medium")

    severity = "critical" if priority == "high" else "warning" if priority == "medium" else "info"

    detail = (
        f"Self-Improvement önerisi:\n\n"
        f"Başlık: {title}\n"
        f"Açıklama: {description}\n"
        f"Öncelik: {priority}\n"
        f"Etkilenen dosyalar: {suggestion.get('affected_files', '')}\n\n"
        f"--- Onay ---\n"
        f"POST /api/v1/self-improvement/approve ile onayla → otomatik PR oluşur."
    )[:3800]

    try:
        _post_json(
            f"{API_BASE}/api/v1/events",
            {
                "type": "self-improvement:suggestion",
                "source": "self-improvement-agent",
                "title": f"💡 Self-Improvement: {title}",
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
    signals = collect_improvement_signals()
    ikey = _envget("INTERNAL_API_KEY")

    if signals is None:
        print(f"OUTCOME: fail | DB okuma hatası (memory: {MEMORY_DB}, server: {SERVER_DB})")
        return 1

    if not signals["patterns"] and not signals["low_success_playbooks"] and not signals["low_confidence"]:
        print(f"OUTCOME: pass | İyileştirme sinyali yok")
        return 0

    suggestions = generate_improvement_suggestions(signals, ikey)

    if suggestions is None:
        print(f"OUTCOME: fail | LLM öneri üretimi başarısız")
        return 1

    if not suggestions:
        print(f"OUTCOME: pass | {len(signals['patterns']) + len(signals['low_success_playbooks']) + len(signals['low_confidence'])} sinyal analiz edildi, öneri yok")
        return 0

    _ensure_pending_table()
    saved = 0
    errors = []
    for suggestion in suggestions[:5]:
        sid = _save_suggestion(suggestion)
        if sid:
            saved += 1
        else:
            errors.append("DB save failed")
            # Fallback: event emit
            err = emit_suggestion_event(suggestion, ikey)
            if err:
                errors.append(err)

    if errors:
        print(f"OUTCOME: partial | {len(suggestions)} öneri, {saved} kaydedildi, {len(errors)} hata: {errors[0][:100]}")
    else:
        print(f"OUTCOME: pass | {len(suggestions)} öneri üretildi, {saved} kaydedildi")
    return 0


if __name__ == "__main__":
    sys.exit(main())
