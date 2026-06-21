#!/usr/bin/env python3
"""Haftalık Ajan Sağlık Raporu — tüm ajanların GERÇEKTEN çalıştığını doğrula + bulguları
sentezle. Salt-okunur.

Veri: cron_outcomes (her ajanın freshness'i, DATA-DRIVEN cadence = geçmiş aralıkların
medyanı → haftalık/günlük job'ları absolüt-saatle yanlış-stale damgalamaz) + discoveries
(aktif bulgular) + alerts (çözülmemiş). Sentez = Haiku (/api/v1/claude/run). Sonuç →
ortak-hafıza (type=learning) + stale/fail ajan varsa Telegram.

Kullanım: agent-health-report.py   (cron: haftalık). OUTCOME marker cron-wrap için.
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
import subprocess
import urllib.request

ENV_FILE = os.environ.get("NOTIFY_ENV_FILE", "/opt/linux-ai-server/.env")
API_BASE = os.environ.get("API_BASE", "http://localhost:8420")
SRV_DB = os.environ.get("SERVER_DB", "/opt/linux-ai-server/data/server.db")
MEM_DB = os.environ.get("MEMORY_DB", "/opt/linux-ai-server/data/claude_memory.db")
TG_HELPER = os.environ.get("GSC_TG_HELPER", "/opt/linux-ai-server/automation/telegram-alert.sh")
HAIKU = os.environ.get("AGENT_REPORT_MODEL", "claude-haiku-4-5-20251001")
# Yaş > cadence × bu kat → STALE (ajan beklenen periyodunda koşmamış). Tolerans cron-jitter için.
STALE_TOLERANCE = 2.2
# job-adı olmayan çöp satırları (CPU%/access-log mis-write) ele
GARBAGE = ("no-id",)


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
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode() or "{}")


def expected_agents() -> set[str]:
    """BEKLENEN cron-ajanları = automation/crontab + canlı crontab'ta klipper-cron-wrap.sh ile
    sarılan job adları. Codex#5: retired/renamed job (cron_outcomes'ta var ama crontab'da yok)
    rapordan dışlanır; Codex#2: beklenen-ama-sessiz olan STALE gösterilir (45g-pencere düşürmez)."""
    names: set[str] = set()
    texts: list[str] = []
    try:
        with open("/opt/linux-ai-server/automation/crontab") as fh:
            texts.append(fh.read())
    except OSError:
        pass
    try:
        texts.append(subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5).stdout)
    except Exception:
        pass
    for txt in texts:
        names.update(_parse_wrap_jobs(txt))
    return names


def _parse_wrap_jobs(text: str) -> set[str]:
    """crontab metninden klipper-cron-wrap.sh job adları — YORUM satırları atlanır (Codex#176:
    retired/yorumlu cron expected-sayılmamalı → yanlış-STALE önle)."""
    import re

    out: set[str] = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = re.search(r"klipper-cron-wrap\.sh\s+(\S+)", line)
        if m:
            out.add(m.group(1))
    return out


def agent_freshness(db: str, expected: set[str] | None = None) -> list[dict]:
    """Her cron-ajanı için son-çalışma + DATA-DRIVEN cadence (geçmiş aralıkların medyanı) →
    status: healthy / stale (periyodunda koşmadı=gerçek sorun) / son-fail (son sonuç pass değil).
    Codex#2+#5: BEKLENEN ajan-listesi (crontab) ile çapraz-referans — retired-dışla, sessiz-beklenen=STALE."""
    if expected is None:
        expected = expected_agents()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # Codex#2: 45g-pencere uzun-sessiz ajanı düşürüyordu; 180g'e genişlet (beklenen-ajan yine de
    # hiç-satırı yoksa aşağıda STALE eklenir). Codex#5: beklenen-listede olmayan job atlanır.
    rows = conn.execute(
        "SELECT job, result, julianday(timestamp) AS jd, "
        "(julianday('now')-julianday(timestamp))*86400 AS age_s "
        "FROM cron_outcomes WHERE timestamp > datetime('now','-180 days') "
        "AND job GLOB '*[a-z]*' AND job NOT GLOB '*[0-9].[0-9]*' "
        "ORDER BY job, jd"
    ).fetchall()
    conn.close()
    by_job: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        if r["job"] in GARBAGE:
            continue
        # Codex#176: relay'lenen job'ları (klipper-cron-wrap DIŞI, ör. vps-backup-push direkt-INSERT)
        # DIŞLAMA — rapordan kaybolmasınlar. STALE-alarm aşağıda yalnız EXPECTED'e uygulanır.
        by_job.setdefault(r["job"], []).append(r)
    out = []
    for job, recs in by_job.items():
        last = recs[-1]
        age_s = last["age_s"] or 0
        age_h = age_s / 3600.0
        # cadence = ardışık çalışma aralıklarının medyanı (saniye); GÜVENİLİR ancak >=3 örnek +
        # makul-aralık (>60s) ise (2-örnek/kümelenmiş job sahte ~0 cadence verir → yanlış-stale).
        jds = [r["jd"] for r in recs]
        gaps = [(jds[i] - jds[i - 1]) * 86400 for i in range(1, len(jds))]
        cadence_s = statistics.median(gaps) if gaps else None
        reliable = cadence_s if (len(recs) >= 3 and cadence_s and cadence_s > 60) else None
        last_ok = str(last["result"]).lower() == "pass"
        # stale = GERÇEK SORUN (periyodunda KOŞMAMIŞ → cron bozuk/kapalı). Az-veri + 14g sessiz = dormant.
        # Codex#5+#176: STALE-alarm yalnız EXPECTED (crontab) ajanlara — retired/relay job overdue olsa
        # da false-alarm vermez (managed-ajan değil); expected boşsa (cross-ref yok) eski davranış.
        stale_eligible = (not expected) or (job in expected)
        overdue = bool(reliable and age_s > reliable * STALE_TOLERANCE)
        dormant = len(recs) < 3 and age_s > 14 * 86400
        if (overdue or dormant) and stale_eligible:
            status = "stale"
        elif not last_ok:
            # periyodunda koştu ama son-sonuç pass değil → geçici/fixli olabilir (bayat-fail dersi)
            status = "son-fail"
        else:
            status = "healthy"
        out.append(
            {
                "job": job,
                "status": status,
                "last_result": last["result"],
                "age_h": round(age_h, 1),
                "cadence_h": round(cadence_s / 3600, 1) if cadence_s else None,
                "runs": len(recs),
            }
        )
    # Codex#2: BEKLENEN ama 180g'de HİÇ-satırı-yok ajan = bozuk/kapalı → STALE (raporun asıl amacı
    # bu — haftalarca sessiz/kırık ajanı yakalamak; eski pencere onu görünmez yapıyordu).
    seen = {a["job"] for a in out}
    for name in sorted(expected - seen):
        out.append({"job": name, "status": "stale", "last_result": None, "age_h": None, "cadence_h": None, "runs": 0})
    return sorted(out, key=lambda a: (a["status"] == "healthy", a["job"]))


def gather_findings(mem_db: str, srv_db: str) -> dict:
    """Aktif bulgular: discoveries (proje bazında), çözülmemiş alerts, 7g cron-fail."""
    m = sqlite3.connect(f"file:{mem_db}?mode=ro", uri=True)
    disc = m.execute(
        "SELECT project, COUNT(*) FROM discoveries WHERE status='active' AND type='bug' GROUP BY project ORDER BY 2 DESC"
    ).fetchall()
    disc_total = m.execute("SELECT COUNT(*) FROM discoveries WHERE status='active'").fetchone()[0]
    m.close()
    s = sqlite3.connect(f"file:{srv_db}?mode=ro", uri=True)
    alerts = s.execute("SELECT severity, COUNT(*) FROM alerts WHERE resolved=0 GROUP BY severity").fetchall()
    fails = s.execute(
        "SELECT job, COUNT(*) FROM cron_outcomes WHERE result='fail' AND timestamp>datetime('now','-7 days') GROUP BY job"
    ).fetchall()
    s.close()
    return {
        "discoveries_active_total": disc_total,
        "discoveries_bug_by_project": dict(disc),
        "alerts_unresolved": dict(alerts),
        "cron_fails_7d": dict(fails),
    }


def build_summary(agents: list[dict], findings: dict) -> str:
    stale = [a for a in agents if a["status"] == "stale"]
    sonfail = [a for a in agents if a["status"] == "son-fail"]
    healthy = [a for a in agents if a["status"] == "healthy"]
    lines = [
        f"AJAN SAĞLIK: {len(healthy)} healthy, {len(stale)} STALE (periyodunda koşmadı = gerçek sorun), "
        f"{len(sonfail)} son-fail (koştu ama son-sonuç pass değil = geçici/fixli olabilir).",
        "",
        "⏰ STALE (KOŞMUYOR — incele):",
    ]
    lines += [
        (
            f"  - {a['job']}: HİÇ koşmadı (180g+, beklenen-ama-sessiz = bozuk/kapalı)"
            if a["age_h"] is None
            else f"  - {a['job']}: son {a['age_h']}h önce, cadence ~{a['cadence_h']}h ({a['runs']} çalışma)"
        )
        for a in stale
    ] or ["  (yok)"]
    lines += ["", "⚠️ SON-FAIL (son çalışma başarısız — sonraki turda doğrulanır):"]
    lines += [f"  - {a['job']}: son-sonuç={a['last_result']}, {a['age_h']}h önce (cadence ~{a['cadence_h']}h)" for a in sonfail] or [
        "  (yok)"
    ]
    lines += [
        "",
        f"BULGULAR: {findings['discoveries_active_total']} aktif discovery.",
        f"  bug/proje: {findings['discoveries_bug_by_project']}",
        f"  çözülmemiş alert: {findings['alerts_unresolved'] or 'yok'}",
        f"  7g cron-fail: {findings['cron_fails_7d'] or 'yok'}",
    ]
    return "\n".join(lines)


def synthesize(summary: str, ikey: str) -> str:
    """Haiku ile yönetici-özeti: 3-5 cümle, durum + en kritik 1-2 aksiyon. Best-effort."""
    if not ikey:
        return ""
    prompt = (
        "Aşağıda bir AI-server'ın haftalık ajan-sağlık + bulgu verisi var. Türkçe, 3-5 cümlelik "
        "YÖNETİCİ ÖZETİ yaz: genel sağlık durumu + EN KRİTİK 1-2 aksiyon (varsa). STALE = gerçek "
        "sorun (ajan koşmuyor). SON-FAIL = son çalışma başarısız ama sonraki turda düzelmiş olabilir "
        "(haftalık-fail'i acil sanma). Abartma, sadece veriye dayan. Sadece özeti döndür.\n\n" + summary
    )
    try:
        out = _post_json(
            f"{API_BASE}/api/v1/claude/run",
            {"prompt": prompt, "read_only": True, "max_turns": 1, "model": HAIKU},
            {"X-API-Key": ikey},
            120,
        )
        return (out.get("result") or "").strip()
    except Exception as e:
        return f"(Haiku sentez başarısız: {str(e)[:80]})"


def write_report(summary: str, narrative: str, n_problem: int, mkey: str) -> str:
    if not mkey:
        return "no MEMORY_API_KEY"
    # Codex#170: başlık SABİT olursa memory-dedup (project+type+title) her hafta ÜZERİNE yazıyor →
    # geçmiş kayboluyor. ISO-hafta ekle → her hafta UNIQUE kayıt (history korunur).
    from datetime import UTC, datetime

    iso = datetime.now(UTC).isocalendar()
    week_tag = f"{iso[0]}-W{iso[1]:02d}"
    body = (f"📊 Haftalık Ajan Sağlık Raporu ({week_tag})\n\n{narrative}\n\n--- Ham veri ---\n{summary}")[:3800]
    try:
        _post_json(
            f"{API_BASE}/api/v1/memory/discoveries",
            {
                "device_name": "klipper",
                "project": "linux-ai-server",
                "type": "learning",
                "title": f"Haftalık Ajan Sağlık Raporu — {week_tag}",
                "details": body,
                "rationale": f"agent-health-report.py — {n_problem} stale/failing ajan; Haiku-sentez; salt-okunur.",
            },
            {"X-Memory-Key": mkey},
            15,
        )
        return ""
    except Exception as e:
        return str(e)[:150]


def send_telegram(stale_failing: list[dict], narrative: str) -> bool:
    if not stale_failing or not os.path.exists(TG_HELPER):
        return False
    rows = "\n".join(f"  • {a['job']}: {a['status']}" for a in stale_failing[:10])
    safe = f"{narrative}\n\nSorunlu ajanlar:\n{rows}".replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = f"📊 <b>Haftalık Ajan Sağlık</b>\n<pre>{safe[:3500]}</pre>"
    try:
        r = subprocess.run([TG_HELPER, "--kind", "generic", "--text", text], capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def main() -> int:
    agents = agent_freshness(SRV_DB)
    findings = gather_findings(MEM_DB, SRV_DB)
    summary = build_summary(agents, findings)
    narrative = synthesize(summary, _envget("INTERNAL_API_KEY"))
    stale = [a for a in agents if a["status"] == "stale"]
    sonfail = [a for a in agents if a["status"] == "son-fail"]
    err = write_report(summary, narrative, len(stale) + len(sonfail), _envget("MEMORY_API_KEY"))
    tg = send_telegram(stale, narrative)  # yalnız STALE (gerçek-koşmuyor) Telegram; son-fail spam-değil
    print(narrative or summary)
    n_h = sum(1 for a in agents if a["status"] == "healthy")
    # Codex#225: STALE-ajan var ama Telegram TESLİM-EDİLEMEDİ → partial (kritik alert sessizce
    # cron-loga gömülmesin; tam o stale-senaryo direkt-Telegram'a bağımlı).
    tg_fail = bool(stale) and not tg
    bad = err or (tg_fail and "telegram-teslim-fail (stale-ajan var)")
    tail = f"{len(agents)} ajan ({n_h} healthy, {len(stale)} stale, {len(sonfail)} son-fail), telegram={tg}"
    print(f"\nOUTCOME: {'partial' if bad else 'pass'} | {tail}" + (f", FAIL: {bad}" if bad else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
