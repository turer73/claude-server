#!/usr/bin/env python3
"""Haftalık VERİ-ANALİSTİ oturumu — server.db + coverage.db trendlerini SALT-OKUNUR
/claude (Max-plan) ile yorumlar; bulgu discovery'e + Telegram özetine.

Tasarım (auto-investigate.py kardeşi, FAZ6 orchestra-boundary uyumlu):
- Zamanlanmış (cron, haftalık) — event-türevli değil ama AYNI sınır: salt-okunur,
  mutasyon YOK. /claude read_only=allowlist → yalnız db-query.sh + Read ile okur.
- DB erişimi YALNIZ scripts/db-query.sh (sqlite3 -readonly -safe) üzerinden → analist
  DELETE/UPDATE yazsa bile MOTOR reddeder. Başka dosya/DB açılamaz (alias-guard).
- Opt-in kapı (DATA_ANALYST_ENABLED=true) — pilot güvenliği. Best-effort, çıkış HER ZAMAN 0.
- Max-plan: /claude/run ANTHROPIC_API_KEY'i strip eder (abonelik, per-token API YOK).

Kullanım: data-analyst.py            (haftalık tam analiz)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://localhost:8420")
ENV_FILE = os.environ.get("NOTIFY_ENV_FILE", "/opt/linux-ai-server/.env")
CWD = os.environ.get("CLAUDE_TG_CWD", "/opt/linux-ai-server")
CLAUDE_TIMEOUT = int(os.environ.get("ANALYST_TIMEOUT", "600"))
MAX_TURNS = int(os.environ.get("ANALYST_MAX_TURNS", "40"))
TG_HELPER = os.environ.get("ANALYST_TG_HELPER", "/opt/linux-ai-server/automation/telegram-alert.sh")
DAYS = os.environ.get("ANALYST_DAYS", "7")


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
    req = urllib.request.Request(  # noqa: S310 (localhost)
        url, data=data, headers={"Content-Type": "application/json", **headers}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (localhost)
        return json.loads(resp.read().decode() or "{}")


def _prompt() -> str:
    # Somut yönerge: keşifte tur harcamasın, doğrudan db-query.sh ile veriye gitsin.
    # KRİTİK timestamp-format uyarısı: metrics_history/vps_metrics_history/alerts ISO-T
    # ('T'-ayraçlı) yazılır; datetime('now') BOŞLUK-ayraçlı → ham compare bozuk pencere
    # verir (bkz fix(devops) ISO-T bug). Bu tablolar için replace(...,' ','T') şart.
    return (
        "Sen bir VERİ ANALİSTİsin. Bu sunucunun (klipper) son "
        f"{DAYS} günlük operasyonel verisini analiz et ve TÜRKÇE kısa bir rapor yaz.\n\n"
        "VERİ ERİŞİMİ — SADECE şu komutu kullan (başka DB/dosya açma):\n"
        '  bash /opt/linux-ai-server/scripts/db-query.sh server "<SQL>"\n'
        '  bash /opt/linux-ai-server/scripts/db-query.sh coverage "<SQL>"\n'
        "(salt-okunur motor; her sorguda LIMIT kullan, çıktı ~40KB ile sınırlı).\n\n"
        "ZAMAN FİLTRESİ FORMAT UYARISI (önemli):\n"
        "- server.db tabloları metrics_history, vps_metrics_history, alerts → timestamp "
        "ISO-T formatında ('2026-..T..+00:00'). Bunlarda zaman filtresi için:\n"
        f"    WHERE timestamp > replace(datetime('now','-{DAYS} days'),' ','T')\n"
        "- server.db events, cron_outcomes ve coverage.db test_runs → boşluk-formatlı; "
        f"normal datetime('now','-{DAYS} days') kullan.\n\n"
        "İNCELE (en az şunlar):\n"
        "1) coverage.db test_runs: pass/fail trend, son durum, kötüleşen var mı.\n"
        "2) server.db metrics_history: cpu/mem/disk/temperature ortalama+tepe, trend.\n"
        "3) server.db cron_outcomes: hangi job'lar fail/partial (sıklık).\n"
        "4) server.db events: son dönem critical/warn desenleri (kaynak bazında).\n\n"
        "ÇIKTI (yalnız bu; markdown başlık kullanma, düz Türkçe):\n"
        "- 4-7 maddelik BULGULAR (her madde somut sayı içersin).\n"
        "- 1-3 maddelik ÖNERİ (aksiyon-önerili, ne yapılmalı).\n"
        "- Bir cümlelik GENEL DURUM (iyi/dikkat/kritik).\n"
        "SALT-OKUMA: hiçbir şeyi DEĞİŞTİRME, yalnız sorgula ve yorumla."
    )


def _send_telegram(report: str) -> bool:
    """telegram-alert.sh generic ile özet gönder. Best-effort."""
    if not os.path.exists(TG_HELPER):
        return False
    # HTML-escape minimal + <pre> blok (Telegram parse_mode=HTML helper'da).
    safe = report.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = "📊 <b>Haftalık Veri Analizi</b>\n<pre>" + safe[:3500] + "</pre>"
    try:
        r = subprocess.run(
            [TG_HELPER, "--kind", "generic", "--text", text],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return r.returncode == 0
    except Exception:
        return False


def run() -> dict:
    ikey = _envget("INTERNAL_API_KEY")
    mkey = _envget("MEMORY_API_KEY")
    # mkey YOKSA bulguyu kaydedemeyiz → pahalı /claude run'ı başlatma (auto-investigate dersi).
    if not ikey or not mkey:
        return {"ok": False, "skipped": "no INTERNAL_API_KEY/MEMORY_API_KEY"}
    try:
        out = _post_json(
            f"{API_BASE}/api/v1/claude/run",
            {"prompt": _prompt(), "read_only": True, "cwd": CWD, "max_turns": MAX_TURNS},
            {"X-API-Key": ikey},
            CLAUDE_TIMEOUT,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    report = (out.get("result") or "").strip()
    if not report:
        return {"ok": False, "error": "boş analiz"}

    # 1) Discovery'e yaz (SessionStart görünürlüğü; dedup: aynı başlık → details güncellenir).
    try:
        _post_json(
            f"{API_BASE}/api/v1/memory/discoveries",
            {
                "device_name": "klipper",
                "project": "linux-ai-server",
                "type": "note",
                "title": "Haftalık veri analizi (data-analyst)",
                "details": f"📊 Otonom haftalık analiz ({DAYS}g):\n{report[:3800]}",
                "rationale": "data-analyst.py (zamanlanmış, salt-okunur /claude + db-query.sh).",
            },
            {"X-Memory-Key": mkey},
            15,
        )
    except Exception:
        pass

    # 2) Telegram özeti (best-effort; başarısız olsa da discovery kalır).
    tg = _send_telegram(report)
    return {"ok": True, "report_len": len(report), "telegram": tg}


def main() -> int:
    # klipper-cron-wrap.sh yalnız pass|partial|fail tanır. Bilinçli-kapalı = pass (kasıtlı,
    # gürültü yok); misconfig (key-yok) ve hata = fail (görünür); başarı = pass.
    if _envget("DATA_ANALYST_ENABLED").lower() != "true":
        print("OUTCOME: pass | skipped — DATA_ANALYST_ENABLED!=true (opt-in kapalı, kasıtlı)")
        return 0  # opt-in kapı (default kapalı)
    res = run()
    if res.get("ok"):
        print(f"OUTCOME: pass | analiz {res['report_len']} char, telegram={res['telegram']}")
    else:
        # best-effort: hata/misconfig'te cron'u düşürme ama OUTCOME:fail ile görünür yap.
        print(f"OUTCOME: fail | {res.get('skipped') or res.get('error', 'bilinmeyen')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
