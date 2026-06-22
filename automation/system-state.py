#!/usr/bin/env python3
"""Sistem Durumu — YAŞAYAN sistem farkındalığı sentezi (LSA Faz-2). Salt-okunur.

Haftalarca süren olayları (durable stores: events 60g, cron_outcomes 90g, alerts, discoveries
KALICI) longitudinal toplayıp TEK LLM-sentezli anlatı üretir: bu-dönem özeti + trend (tekrar-fail,
kendi-iyileşen incident, N-gündür çözülmemiş, Haiku kod-review aktivitesi) + dikkat-gereken.
Sonuç → ortak-hafıza discovery(type=learning, skip_dedup, tarih-unique) → SessionStart okur.

Desen: agent-health-report.py mirror'ı (/api/v1/claude/run read_only sentez + discovery write).
Kullanım: system-state.py [--now] [--days N]   (cron: günlük). OUTCOME marker cron-wrap için.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
from datetime import UTC, datetime

ENV_FILE = os.environ.get("NOTIFY_ENV_FILE", "/opt/linux-ai-server/.env")
API_BASE = os.environ.get("API_BASE", "http://localhost:8420")
SRV_DB = os.environ.get("SERVER_DB", "/opt/linux-ai-server/data/server.db")
MEM_DB = os.environ.get("MEMORY_DB", "/opt/linux-ai-server/data/claude_memory.db")
# Sentez = Sonnet (anlatı zenginliği). /api/v1/claude/run model-param (LLMCore task-route DEĞİL).
SYNTH_MODEL = os.environ.get("SYSTEM_STATE_MODEL", "claude-sonnet-4-6")
DAYS = int(os.environ.get("SYSTEM_STATE_DAYS", "7"))


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


def _q(db: str, sql: str, params: tuple = ()) -> list:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


def gather_state(days: int) -> dict:
    """Durable store'lardan longitudinal sistem-durumu verisi (salt-okunur, fail-safe)."""
    w = f"-{days} days"
    st: dict = {"days": days}
    # Bu dönem: events severity dağılımı
    st["events_by_sev"] = dict(
        _q(SRV_DB, "SELECT severity, COUNT(*) FROM events WHERE timestamp > datetime('now',?) GROUP BY severity", (w,))
    )
    # Unhandled exception (gap-2 producer): fingerprint-gruplu — recurring server-bug trendi
    st["exceptions_by_fp"] = _q(
        SRV_DB,
        "SELECT title, COUNT(*) c FROM events WHERE type='exception' AND timestamp > datetime('now',?) "
        "GROUP BY source ORDER BY c DESC LIMIT 8",
        (w,),
    )
    # Cron: pass/fail/partial + TEKRAR-FAIL job'lar (>=3 fail = trend)
    st["cron_result"] = dict(
        _q(SRV_DB, "SELECT result, COUNT(*) FROM cron_outcomes WHERE timestamp > datetime('now',?) GROUP BY result", (w,))
    )
    st["cron_recurring_fail"] = _q(
        SRV_DB,
        "SELECT job, COUNT(*) c FROM cron_outcomes WHERE result='fail' AND timestamp > datetime('now',?) "
        "GROUP BY job HAVING c >= 3 ORDER BY c DESC LIMIT 8",
        (w,),
    )
    # Alarmlar: dönemde fırlayan + KENDİ-İYİLEŞEN (resolved) + HÂLÂ AÇIK
    st["alerts_fired"] = _q(
        SRV_DB,
        "SELECT source, COUNT(*) c, SUM(resolved) r FROM alerts WHERE timestamp > datetime('now',?) "
        "GROUP BY source ORDER BY c DESC LIMIT 8",
        (w,),
    )
    st["alerts_open_aging"] = _q(
        SRV_DB,
        "SELECT severity, source, substr(message,1,50), round(julianday('now')-julianday(timestamp),1) age_d "
        "FROM alerts WHERE resolved=0 ORDER BY timestamp ASC LIMIT 8",
    )
    # Discoveries: dönemde yeni + açık-bug yaşlanması
    st["new_discoveries"] = _q(
        MEM_DB,
        "SELECT type, COUNT(*) FROM discoveries WHERE created_at > datetime('now',?) GROUP BY type",
        (w,),
    )
    st["open_bugs_aging"] = _q(
        MEM_DB,
        "SELECT project, substr(title,1,55), round(julianday('now')-julianday(created_at),1) age_d "
        "FROM discoveries WHERE type='bug' AND status='active' ORDER BY created_at ASC LIMIT 8",
    )
    # Haiku kod-review aktivitesi (heartbeat + commit-bulguları)
    st["code_review_findings"] = _q(
        SRV_DB,
        "SELECT COUNT(*) FROM events WHERE source LIKE 'code-review:%' AND title LIKE '%(commit)%' AND timestamp > datetime('now',?)",
        (w,),
    )
    hb = "/opt/linux-ai-server/data/hook-state/last-code-review.json"
    try:
        with open(hb) as f:
            st["last_review"] = json.load(f)
    except (OSError, ValueError):
        st["last_review"] = None
    return st


def render_data(st: dict) -> str:
    """LLM'e verilecek ham-veri özeti (Türkçe etiketli)."""
    lr = st.get("last_review")
    lr_s = (
        f"{'TEMİZ' if lr.get('clean') else str(lr.get('findings')) + ' bulgu'} ({lr.get('files')} dosya) {lr.get('ts', '')[:16]}"
        if lr
        else "kayıt yok"
    )
    L = [
        f"SİSTEM DURUMU HAM VERİ (son {st['days']} gün):",
        f"Olaylar (severity): {st['events_by_sev'] or 'yok'}",
        f"Unhandled exception (fingerprint, adet): {[(t, c) for t, c in st['exceptions_by_fp']] or 'yok'}",
        f"Cron sonuç: {st['cron_result'] or 'yok'}",
        f"TEKRAR-FAIL cron (>=3): {[(j, c) for j, c in st['cron_recurring_fail']] or 'yok'}",
        f"Alarm fırlayan (source, adet, çözülen): {[(s, c, r) for s, c, r in st['alerts_fired']] or 'yok'}",
        f"AÇIK alarm (yaş-gün): {[(sev, src, m, a) for sev, src, m, a in st['alerts_open_aging']] or 'yok'}",
        f"Yeni discovery (tip): {st['new_discoveries'] or 'yok'}",
        f"AÇIK bug (yaş-gün): {[(p, t, a) for p, t, a in st['open_bugs_aging']] or 'yok'}",
        f"Haiku kod-review bulgu (dönem): {st['code_review_findings'][0][0] if st['code_review_findings'] else 0}",
        f"Son Haiku-review verdict: {lr_s}",
    ]
    return "\n".join(L)


def synthesize(summary: str, ikey: str) -> str:
    """Sonnet ile YAŞAYAN sistem anlatısı: bu-dönem + trend + dikkat-gereken. Best-effort."""
    if not ikey:
        return ""
    prompt = (
        "Aşağıda bir AI-server'ın son günlerdeki sistem-durumu ham verisi var. Türkçe, 4-7 cümlelik "
        "YAŞAYAN SİSTEM ANLATISI yaz (klipper ajanına brifing): (1) genel durum, (2) TREND — neyin "
        "tekrar-bozulduğu/kendi-iyileştiği/yaşlandığı, (3) EN ÇOK dikkat-gereken 1-3 şey. Kendi-iyileşen "
        "incident'i (alarm fırlayıp çözülmüş) 'sorun' sayma ama belirt. Çözülmemiş + yaşlanan = gerçek-borç. "
        "Abartma, sadece veriye dayan. Sadece anlatıyı döndür.\n\n" + summary
    )
    try:
        out = _post_json(
            f"{API_BASE}/api/v1/claude/run",
            {"prompt": prompt, "read_only": True, "max_turns": 1, "model": SYNTH_MODEL},
            {"X-API-Key": ikey},
            180,
        )
        return (out.get("result") or "").strip()
    except Exception as e:
        return f"(Sonnet sentez başarısız: {str(e)[:80]})"


def write_state(summary: str, narrative: str, mkey: str) -> str:
    """discovery(type=learning, skip_dedup, tarih-unique) → SessionStart okur. agent-health-report deseni."""
    if not mkey:
        return "no MEMORY_API_KEY"
    day_tag = datetime.now(UTC).strftime("%Y-%m-%d")
    body = (f"🛰️ Sistem Durumu ({day_tag})\n\n{narrative}\n\n--- Ham veri ---\n{summary}")[:3800]
    try:
        _post_json(
            f"{API_BASE}/api/v1/memory/discoveries",
            {
                "device_name": "klipper",
                "project": "linux-ai-server",
                "type": "learning",
                "skip_dedup": True,  # günlük-log; semantic-dedup ardışık günleri merge etmesin (Codex#176 dersi)
                "title": f"Sistem Durumu — {day_tag}",
                "details": body,
                "rationale": "system-state.py (LSA Faz-2) — longitudinal sentez; Sonnet; salt-okunur.",
            },
            {"X-Memory-Key": mkey},
            15,
        )
        return ""
    except Exception as e:
        return str(e)[:150]


def main() -> int:
    days = DAYS
    if "--days" in sys.argv:
        try:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        except (ValueError, IndexError):
            pass
    st = gather_state(days)
    summary = render_data(st)
    ikey = _envget("INTERNAL_API_KEY")  # /api/v1/claude/run X-API-Key (agent-health-report deseni)
    mkey = _envget("MEMORY_API_KEY")
    narrative = synthesize(summary, ikey)
    if "--now" in sys.argv:  # on-demand: anlatıyı stdout'a da bas
        print(narrative or "(sentez yok)")
    err = write_state(summary, narrative, mkey)
    n_open = len(st["alerts_open_aging"]) + len(st["open_bugs_aging"])
    if err:
        print(f"OUTCOME: partial | sentez OK ama discovery yazılamadı: {err}")
    elif not narrative or narrative.startswith("("):
        print(f"OUTCOME: partial | sentez zayıf/başarısız ({n_open} açık-borç); ham-veri yazıldı")
    else:
        print(f"OUTCOME: pass | Sistem Durumu yazıldı ({days}g, {n_open} açık-borç)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
