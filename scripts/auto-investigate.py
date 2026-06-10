#!/usr/bin/env python3
"""Slice B: tekrarlayan-critical kaynağı SALT-OKUNUR /claude ile otonom incele -> bulgu
discovery'e (gözlemden anlamaya köprüsü).

notify-cron tarafından ARKA-PLANDA tetiklenir (AUTO_INVESTIGATE_ENABLED=true + kaynak
tekrarlayan). Fire-and-forget: notify-cron bloklanmaz. FAZ6 orchestra-boundary: event-
türevli (alert->incele), opt-in + per-source rate-limit (default 1/saat). Salt-okunur
(/claude read_only=allowlist) -> mutasyon yapamaz; yalnız okur+analiz eder.

Kullanım: auto-investigate.py <source> <recur_count>
Çıkış HER ZAMAN 0 (best-effort; tetikleyiciyi düşürme).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://localhost:8420")
ENV_FILE = os.environ.get("NOTIFY_ENV_FILE", "/opt/linux-ai-server/.env")
STATE_DIR = os.environ.get("INVESTIGATE_STATE_DIR", "/opt/linux-ai-server/data/hook-state")
MIN_INTERVAL = int(os.environ.get("INVESTIGATE_MIN_INTERVAL", "3600"))  # saniye, per-source
CLAUDE_TIMEOUT = int(os.environ.get("INVESTIGATE_TIMEOUT", "200"))
CWD = os.environ.get("CLAUDE_TG_CWD", "/opt/linux-ai-server")
RECUR_DAYS = os.environ.get("RECUR_DAYS", "7")


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
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})  # noqa: S310 (localhost)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (localhost)
        return json.loads(resp.read().decode() or "{}")


def _rate_limited(source: str) -> bool:
    """Per-source rate-limit: son inceleme MIN_INTERVAL içinde ise atla."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", source)
    path = os.path.join(STATE_DIR, f"investigate-{safe}")
    try:
        last = os.path.getmtime(path)
        if (time.time() - last) < MIN_INTERVAL:
            return True
    except OSError:
        pass
    return False


def _mark(source: str) -> None:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", source)
    os.makedirs(STATE_DIR, exist_ok=True)
    open(os.path.join(STATE_DIR, f"investigate-{safe}"), "w").close()


def _prompt(source: str, recur: str) -> str:
    # Kaynak-tipine göre SOMUT yer ipucu -> claude keşifte tur harcamaz, çabuk sonuca gider
    # (genel "ilgili log" promptu max_turns'ü tüketip boş dönüyordu).
    base, _, name = source.partition(":")
    if base == "cron" and name:
        hint = f"Logu burada: /var/log/linux-ai-server/{name}.log (son ~30 satırı Read ile oku)."
    elif base == "service" and name:
        hint = f"`systemctl status {name}` ve ilgili son /var/log dosyasına bak."
    elif base == "docker" and name:
        hint = f"`docker logs --tail 50 {name}` çıktısına bak."
    else:
        hint = "İlgili log/durumu (1-2 dosya) oku."
    return (
        f"'{source}' kaynağı son {RECUR_DAYS} günde {recur} kez critical oldu. {hint} "
        f"SADECE 1-2 dosya oku, fazla araştırma YAPMA. Sonra TEK kısa paragraf (3-5 cümle, "
        f"Türkçe) yaz: olası kök-neden + önerilen çözüm. SALT-OKUMA, hiçbir şeyi değiştirme."
    )


def investigate(source: str, recur: str) -> dict:
    """Tek kaynak için: rate-limit -> /claude read_only -> bulgu discovery'e. Best-effort."""
    if _rate_limited(source):
        return {"ok": False, "skipped": "rate-limited"}
    ikey = _envget("INTERNAL_API_KEY")
    mkey = _envget("MEMORY_API_KEY")
    # Codex P2: mkey YOKSA bulguyu kaydedemeyiz -> pahalı /claude run'ı BAŞLATMA (boşa
    # harcama). Her ikisi de şart (claude-çağrısı = ikey, bulgu-kaydı = mkey).
    if not ikey or not mkey:
        return {"ok": False, "skipped": "no INTERNAL_API_KEY/MEMORY_API_KEY"}
    _mark(source)  # önce işaretle (eş-zamanlı 2. tetikleme tekrar başlatmasın)
    try:
        run = _post_json(
            f"{API_BASE}/api/v1/claude/run",
            {"prompt": _prompt(source, recur), "read_only": True, "cwd": CWD, "max_turns": 30},
            {"X-API-Key": ikey},
            CLAUDE_TIMEOUT,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    finding = (run.get("result") or "").strip()
    if not finding:
        return {"ok": False, "error": "boş inceleme"}
    # Teşhisi KALICI hale getir — "teşhis edip bırakma" yerine 3 katman:
    #   (1) discovery FIX-PENDING + status=active: teşhis≠çözüm. Biri yanlışlıkla 'completed'
    #       yapsa bile sonraki recurrence regression-active üretir → SessionStart'ta kalır.
    #   (2) durable memory (type=project, name-deduped upsert): teşhis kurumsal-bilgi olur,
    #       discovery kapansa/silinse bile kaybolmaz.
    if mkey:
        hdr = {"X-Memory-Key": mkey}
        fix_pending = (
            f"⚠️ FIX-PENDING — teşhis tamam, düzeltme UYGULANMADI ({recur}x tekrar).\n🔍 Otonom kök-neden incelemesi:\n{finding[:3400]}"
        )
        try:
            _post_json(
                f"{API_BASE}/api/v1/memory/discoveries",
                {
                    "device_name": "klipper",
                    "project": "linux-ai-server",
                    "type": "bug",
                    "title": f"AUTO-alert: {source}",
                    "details": fix_pending,
                    "status": "active",
                    "rationale": "auto-investigate.py — teşhis YAPILDI, FIX bekliyor (completed işaretleme!).",
                },
                hdr,
                15,
            )
        except Exception:
            pass
        try:
            _post_json(
                f"{API_BASE}/api/v1/memory/memories",
                {
                    "type": "project",
                    "name": f"auto-diagnosis-{source}",
                    "description": f"{source} recurring-critical otomatik kök-neden teşhisi (auto-investigate)",
                    "content": f"{source} {recur}x tekrarlayan critical — otomatik teşhis:\n{finding[:2500]}",
                    "source_device": "klipper",
                    "rationale": "auto-investigate.py durable kayıt — teşhis kaybolmasın, fix-takibi için.",
                },
                hdr,
                15,
            )
        except Exception:
            pass
    return {"ok": True, "source": source, "finding_len": len(finding)}


def main() -> int:
    if len(sys.argv) < 3:
        return 0
    source, recur = sys.argv[1], sys.argv[2]
    if _envget("AUTO_INVESTIGATE_ENABLED").lower() != "true":
        return 0  # opt-in kapı (default kapalı)
    investigate(source, recur)
    return 0


if __name__ == "__main__":
    sys.exit(main())
