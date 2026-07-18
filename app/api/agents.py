"""Agent management API endpoints."""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.agent_system import AgentDefinition as AgentDef
from app.core.agent_system import AgentRegistry
from app.db.data_layer import MEMORY_DB, get_conn, server_db_path
from app.middleware.dependencies import require_auth, require_write
from app.models.schemas import AgentDefinition

_LOG_DIR = "/var/log/linux-ai-server"
_AUTOMATION = "/opt/linux-ai-server/automation"

# Karar-ajanları manifesti — sürekli(inmem) dışındaki on-demand + cron ajanları.
# tip: ondemand(research) | cron(log mtime + events). script: manuel-tetikleme (allowlist).
# (mypy "# type:" ile baslayan yorumu eski-tarz type-comment sanip parse-hatasi veriyordu)
_AGENT_MANIFEST = [
    {
        "key": "research",
        "name": "Araştırma Ajanı",
        "role": "İnternet araştırma · grounding · sentez",
        "type": "ondemand",
        "schedule": "istek-üzerine",
        "models": ["qwen2.5:3b / gemma3:12b-it-qat", "claude CLI (sentez)"],
        "src": "research",
    },
    {
        "key": "ad-advisor",
        "name": "Reklam Danışmanı",
        "role": "AdSense strateji uzmanı (LLM)",
        "type": "cron",
        "schedule": "haftalık",
        "models": ["claude-sonnet-4-6 (CLI)"],
        "log": "ad-advisor.log",
        "evsrc": "ad-advisor",
        "script": "ad-advisor.sh",
        # Gercek ciktisi server.db.events'te DEGIL, discoveries'te (bkz _discoveries_for).
        "disc_like": "%(ad-advisor)%",
    },
    {
        "key": "adsense-readiness",
        "name": "AdSense Hazırlık",
        "role": "Site yayın-hazırlık denetimi",
        "type": "cron",
        "schedule": "haftalık",
        "models": ["kural-tabanlı"],
        "log": "adsense-readiness.log",
        "evsrc": "adsense",
        "script": "adsense-readiness.sh",
    },
    {
        "key": "data-analyst",
        "name": "Veri Analisti",
        "role": "Metrik/trend analizi",
        "type": "cron",
        "schedule": "haftalık",
        "models": ["veri-script"],
        "log": "data-analyst.log",
        "evsrc": "data-analyst",
        "script": "data-analyst.sh",
        "disc_like": "%(data-analyst)%",
    },
    {
        "key": "seo-audit",
        "name": "SEO Denetçi",
        "role": "On-page SEO denetimi",
        "type": "cron",
        "schedule": "haftalık",
        "models": ["veri-script"],
        "log": "seo-audit.log",
        "evsrc": "seo-audit",
        "script": "seo-audit.sh",
        "disc_like": "%(seo-audit)%",
    },
    {
        "key": "seo-gsc",
        "name": "SEO Search Console",
        "role": "GSC sıralama/tıklama takibi",
        "type": "cron",
        "schedule": "haftalık",
        "models": ["GSC API"],
        "log": "seo-gsc.log",
        "evsrc": "seo-gsc",
        "script": "seo-gsc.sh",
        # "(seo-gsc)" degil "GSC..." basligi kullanir (bkz GSC firsati/GSC: sc-domain hata).
        "disc_like": "GSC%",
        "disc_types": ("learning", "bug"),
    },
    {
        "key": "seo-plausible",
        "name": "SEO Plausible",
        "role": "Plausible analitik özet",
        "type": "cron",
        "schedule": "haftalık",
        "models": ["Plausible API"],
        "log": None,
        "evsrc": "plausible",
        "script": "seo-plausible.sh",
    },
    {
        "key": "memory-synthesize",
        "name": "Hafıza Sentezi",
        "role": "Tekrar-eden bulgu → ders",
        "type": "cron",
        "schedule": "haftalık",
        "models": ["kural-tabanlı"],
        "log": "memory-synth.log",
        "evsrc": "memory-synth",
        "script": "memory-synthesize.sh",
        # cron_outcomes.job = 'memory-synth', manifest-key'den FARKLI (bkz _cron_success).
        "job": "memory-synth",
        # #1334-sweep: script varsayılan DRY_RUN modunda BİLEREK 'partial' basar (pass sadece
        # APPLY=1 veya sentezlenecek küme yokken) — bu bir hata değil, tasarım. _cron_success bunu
        # 'partial'ı da başarı sayarak yansıtsın (aksi halde dashboard %16 gösterip yanlış-alarm verir).
        "partial_is_success": True,
    },
    {
        "key": "memory-triage",
        "name": "Hafıza Triyaj",
        "role": "Bayat kayıt temizliği (LLM)",
        "type": "cron",
        "schedule": "günlük",
        "models": ["claude-haiku CLI"],
        "logpath": "/opt/linux-ai-server/data/hook-logs/triage-llm.log",
        "evsrc": "memory-triage",
        "script": "memory-triage.sh",
    },
    {
        "key": "weekly-audit",
        "name": "Haftalık Denetim",
        "role": "Sistem güvenlik/sağlık denetimi",
        "type": "cron",
        "schedule": "haftalık",
        "models": ["kural-tabanlı"],
        "log": "weekly-audit.log",
        "evsrc": "weekly-audit",
        "script": "weekly-audit.sh",
    },
    {
        "key": "agent-health-report",
        "name": "Ajan Sağlık Raporu",
        "role": "Tüm ajanların çalışırlığı + bulgu sentezi (meta-monitor)",
        "type": "cron",
        "schedule": "haftalık",
        "models": ["claude-haiku-4-5 (CLI)"],
        "log": "agent-health-report.log",
        "evsrc": "agent-health-report",
        "script": "agent-health-report.sh",
    },
    {
        "key": "intent-liveness-audit",
        "name": "Niyet-Canlılık Denetçi",
        "role": "Cron/intent canlılık denetimi",
        "type": "cron",
        "schedule": "haftalık",
        "models": ["kural-tabanlı"],
        "log": "intent-liveness.log",
        "evsrc": "intent-liveness",
        "script": "intent-liveness-audit.sh",
        "job": "intent-liveness",
        # #1334-sweep: script bulgu bulunca BİLEREK 'partial' basar (pass sadece bulgu-yokken) —
        # bu bir hata değil, denetçinin normal işi. Bkz memory-synthesize'daki aynı desen.
        "partial_is_success": True,
    },
    {
        "key": "autonomous-daily-summary",
        "name": "Günlük Özet",
        "role": "LLM günlük operasyon özeti",
        "type": "cron",
        "schedule": "günlük",
        "models": ["qwen2.5:3b"],
        "log": "autonomous-summary.log",
        "evsrc": "autonomous",
        "script": "autonomous-daily-summary.sh",
        "job": "autonomous-summary",
    },
    # ── 2026-07-15 sweep (#1334 dashboard-denetimi): bu 8 ajan crontab'da canlı-çalışıyordu ama
    # manifest-dışıydı (Ajanlar sekmesinde görünmüyordu). Bkz reference_agents_dashboard_sweep.
    {
        "key": "meta-cognition",
        "name": "Meta-Biliş",
        "role": "Düşünce kalitesi / confidence denetimi",
        "type": "cron",
        "schedule": "günlük",
        "models": ["kural-tabanlı (istatistik+heuristic)"],
        "log": "meta-cognition.log",
        # Codex #328-P2: "meta-cognition-agent" (script'in kendi event-source'u) POST /api/v1/events
        # route'u YOK — o çağrı hep 404. Bare job-adı yerine LIKE-substring hem bunu hem her-zaman-
        # yazılan cron:meta-cognition fallback'ini yakalar (bkz _cron_card cron: fallback).
        "evsrc": "meta-cognition",
        "script": "meta_cognition.sh",
    },
    {
        "key": "pattern-recognition",
        "name": "Pattern Tanıma",
        "role": "Bilinç düşüncelerinde tekrarlayan pattern tespiti",
        "type": "cron",
        "schedule": "günlük",
        "models": ["kural-tabanlı (SQL+threshold)"],
        "log": "pattern-recognition.log",
        "disc_like": "Tekrar Eden Pattern%",
        "script": "pattern_recognition.sh",
    },
    {
        "key": "reflection",
        "name": "Yansıma (Reflection)",
        "role": "Playbook başarı-oranı analizi",
        "type": "cron",
        "schedule": "haftalık",
        "models": ["kural-tabanlı (SQL+threshold)"],
        "log": "reflection.log",
        "disc_like": "Playbook Başarı%",
        "script": "reflection.sh",
    },
    {
        "key": "predictive-agent",
        "name": "Öngörü Ajanı",
        "role": "Proaktif eşik/trend tahmini (disk/CPU/RAM)",
        "type": "cron",
        "schedule": "günlük",
        "models": ["kural-tabanlı (linear regression)"],
        "log": "predictive-agent.log",
        "evsrc": "predictive-agent",
        "script": "predictive_agent.sh",
    },
    {
        "key": "self-improvement",
        "name": "Öz-İyileştirme",
        "role": "Kod-değişikliği önerisi üretimi",
        "type": "cron",
        "schedule": "haftalık",
        "models": ["claude-sonnet-4-6"],
        "log": "self-improvement.log",
        # Codex #328-P2: normal-başarı yolu self_improvement_pending'e yazar, event SADECE DB-yazma-
        # hatasında fallback (ve o fallback da POST /api/v1/events'e gider — route yok, hep 404).
        # pending_table birincil kaynak; evsrc yalnız cron:self-improvement fallback'i için tutulur.
        "evsrc": "self-improvement",
        "pending_table": "self_improvement_pending",
        "script": "self_improvement.sh",
    },
    {
        "key": "cross-source-consolidation",
        "name": "Çapraz-Kaynak Birleştirme",
        "role": "Farklı kaynaklardan öğrenmeleri embedding-kümeleme ile birleştir",
        "type": "cron",
        "schedule": "haftalık",
        "models": ["bge-m3 (embedding)"],
        "log": "cross-source-consolidation.log",
        # Codex #328-P2: ne evsrc ne disc_like vardı → hata-detayı hiç görünmüyordu. Script kendi
        # event/discovery yazmıyor (unified-memory'e POST ediyor) — cron:job fallback tek kaynak.
        "evsrc": "cross-source-consolidation",
        "script": "cross_source_consolidation.sh",
    },
    {
        "key": "self-pentest",
        "name": "Öz-Pentest",
        "role": "Sahip olunan domain'lerde haftalık güvenlik taraması",
        "type": "cron",
        "schedule": "haftalık",
        "models": ["kural-tabanlı"],
        "log": "self-pentest.log",
        # Codex #328-P2 r1→r4: disc_like ile gerçek vulnerability-başlıkları ("GUVENLIK: auth-
        # bypass", "self-pentest: eksik security header/TLS/cookie") gösterilmeye çalışıldı, sırayla
        # whitelist'e daraltıldı + status='active' filtrelendi. r5: KÖK-SORUN kaldı — bu detaylar
        # dedicated pentest API'de (app/api/security.py) verify_pentest_key arkasında, ama
        # /agents/runtime yalnız require_auth ister → herhangi-authenticated kullanıcı düşük-
        # ayrıcalıklı bir yoldan vulnerability-detaylarını görebilirdi. Fix: disc_like tamamen
        # kaldırıldı — kart yalnız cron:<job> wrapper-ÖZETİ gösterir (sayı, "X/Y domain tarandı,
        # N bulgu" — detay YOK). Gerçek bulgular için tek yol: dedicated /security/pentest/findings.
        "script": "self-pentest.sh",
        # Codex #328-P2 (2 sorun): (1) tam-tarama 51-path×domain + 30sn ara-bekleme > generic 600s-
        # timeout, elle-tetikle sessizce öldürülür; (2) argümansız-full-scan tasarlanmamış manuel-
        # tek-tık için. Görüntüle-only bırakıldı, script zaten haftalık cron'da çalışıyor.
        "triggerable": False,
    },
    {
        "key": "ci-fix-runall",
        "name": "Otonom CI-Düzeltici",
        "role": "9-repo CI-fail → Claude-fix denemesi (soft-gate #1230 shadow)",
        "type": "cron",
        "schedule": "günlük",
        "models": ["claude CLI (ci_fixer)"],
        "log": "ci-fix-runall.log",
        "evsrc": "ci_fixer",
        "script": "ci-fix-runall.sh",
        # Codex #328-P2 (security, ciddi): generic /runtime/{key}/trigger yalnız require_write ister,
        # ama script'in çağırdığı /api/v1/ci/run-all require_admin-korumalı — write-token bu kartla
        # admin-gate'i bypass edip 9-repo attempt_fix tetikleyebilirdi. Ayrıca 600s generic-timeout,
        # script'in izin verdiği 2700s'ten kısa (elle-tetiklenirse sessizce öldürülür). Görüntüle-only;
        # script zaten günlük cron'da (+admin-gated /ci/run-all üzerinden) çalışıyor.
        "triggerable": False,
    },
]
# Codex #328-P2 (security): triggerable=False SUNUCU-TARAFINDA da uygulanmalı — sadece UI-buton
# gizlemek yetmez, /runtime/{key}/trigger doğrudan çağrılabilir. ci-fix-runall (require_write ile
# require_admin-korumalı /ci/run-all'ı bypass eder + 600s generic-timeout 2700s'e izin veren script'i
# öldürür) ve self-pentest (10dk-timeout > gerçek çoklu-domain tarama süresi) bu yüzden burada dışlanır.
_CRON_SCRIPTS = {a["key"]: a["script"] for a in _AGENT_MANIFEST if a.get("script") and a.get("triggerable", True)}

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

_registry = AgentRegistry()


@router.get("/list", dependencies=[Depends(require_auth)])
async def list_agents():
    return {"agents": _registry.list_agents()}


# ── Runtime (canlı arka-plan ajanları): tek sekmede last-run/iş/bulgu/model/başarı ──


def _sev_from_details(det: str) -> str:
    for s in ("P1", "P2", "P3"):
        if det.startswith(f"[{s}]"):
            return s
    return ""


def _codereview_db() -> dict:
    """code-review discoveries: son bulgular + active/obsolete sayımı (sinyal-oranı). Read-only.

    counts_14d: sinyal-oranı SON-14-GÜN penceresiyle hesaplansın diye ayrı sayım. Yaşam-boyu
    kümülatif oran 14-22 Haziran qwen/fail-open FP-seli havuzunu (306 kayıt, %4) sonsuza dek
    paydada taşıyordu → pipeline %88'e çıkmışken dashboard %16 gösteriyordu (Turgut 07-10).
    30g DEĞİL 14g: bugünün (2026-07-10) tarihinde 30g pencere hâlâ 10-22 Haziran kötü-havuzunu
    kapsıyor (kendiliğinden ancak ~20 Temmuz'da düzelir) — 14g bunu HEMEN dışlar + n=35 yeterli
    (klipper verify, PR#301 re-review)."""
    try:
        con = get_conn(MEMORY_DB, readonly=True)
        try:
            counts: dict[str, int] = {}
            counts_14d: dict[str, int] = {}
            for status, n, n14 in con.execute(
                "SELECT status, COUNT(*), "
                "SUM(CASE WHEN created_at >= datetime('now','-14 days') THEN 1 ELSE 0 END) "
                "FROM discoveries WHERE project='code-review' AND type='bug' GROUP BY status"
            ).fetchall():
                counts[status] = n
                counts_14d[status] = n14 or 0
            rows = con.execute(
                "SELECT created_at, title, COALESCE(details,'') AS details, status, type "
                "FROM discoveries WHERE project='code-review' ORDER BY id DESC LIMIT 8"
            ).fetchall()
            findings = [
                {
                    "time": r["created_at"],
                    "title": r["title"],
                    "severity": _sev_from_details(r["details"]),
                    "status": r["status"],
                    "kind": r["type"],
                }
                for r in rows
            ]
            return {"counts": counts, "counts_14d": counts_14d, "findings": findings}
        finally:
            con.close()
    except Exception:
        return {"counts": {}, "counts_14d": {}, "findings": []}


def _devops_card(dv) -> dict:
    st = dv.status
    log = list(getattr(dv, "_remediation_log", []))
    total = len(log)
    succ = sum(1 for r in log if getattr(r, "success", False))
    remediation_findings = [
        {
            "time": getattr(r, "timestamp", None),
            "title": f"{getattr(r, 'alert_source', '?')} → {getattr(r, 'action', '?')}",
            "severity": "P3" if getattr(r, "success", False) else "P1",
            "status": "pass" if getattr(r, "success", False) else "fail",
            "kind": "remediation",
        }
        for r in log[-8:][::-1]
    ]
    # Kullanıcı (2026-07-18): kart 'izleme·remediation·teşhis' diye etiketleniyor ama findings
    # yalnız _remediation_log'dan geliyordu — DiagnosisMixin._diagnose_and_emit'in ürettiği
    # 'diagnosis:{source}' event'leri (sustained-critical alert'te LLM kök-neden hipotezi)
    # HİÇ görünmüyordu. Tarihsel-doğrulama: 17 gerçek teşhis-event var (06-21→07-13) ama
    # dashboard'da sıfırı hiç yansımamış — 0-aktif-alarm dönemlerinde "0 bulgu" yanıltıcı
    # görünüyordu (aslında "0 remediation", teşhis-geçmişi ayrı-görünmez). _events_for zaten
    # diğer cron-ajan kartlarının kullandığı ortak yardımcı — aynı deseni burada da uygula.
    diag_findings = _events_for("diagnosis:", limit=5)
    findings = sorted((remediation_findings + diag_findings), key=lambda f: f.get("time") or "", reverse=True)[:8]
    active = st.get("active_alerts", 0)
    return {
        "key": "devops",
        "name": "DevOps Ajanı",
        "role": "İzleme · remediation · teşhis",
        "type": "continuous",
        "schedule": "30sn döngü",
        "running": bool(st.get("running")),
        "models": [f"{getattr(dv, '_diag_model', '?')} (teşhis)"],
        "last_run": st.get("last_check"),
        "interval_s": st.get("interval_seconds"),
        "current_task": (f"Remediation: {active} aktif uyarı" if active else "İzleme (cpu/mem/disk/vps/servis/docker)"),
        "stats": {"Kontrol": st.get("check_count", 0), "Aktif uyarı": active, "Remediation": total},
        "success_rate": ({"label": "Remediation başarısı", "value": round(succ / total, 3), "n": total} if total else None),
        "findings": findings,
    }


def _codereview_card(cra, crdb: dict) -> dict:
    st = cra.status()
    counts = crdb["counts"]
    active = counts.get("active", 0)
    # Sinyal-oranı = TRİYAJ-EDİLEN bulguların kaçı GERÇEKTİ: completed (fix'lendi) ÷ (completed+obsolete).
    # active (triaj-bekleyen) sayılmaz — henüz gerçek/FP bilinmez. ESKİ HATA-1: active/(active+obsolete)
    # = açık/toplam (backlog), FP-oranı DEĞİL → her şey triaj-edilip kapatılınca yanıltıcı %0 ("ajan
    # %100 FP" sanılır oysa "backlog temiz" demek). ESKİ HATA-2: yaşam-boyu kümülatif oran — eski
    # FP-seli havuzu paydada kaldıkça bugünkü pipeline'ı yansıtmaz → SON-14-GÜN penceresi ana metrik,
    # tüm-zaman stats'ta ayrı satır. 14g'de hiç triyaj yoksa tüm-zamana düşülür (dürüst etiketle).
    completed = counts.get("completed", 0)
    triaged = completed + counts.get("obsolete", 0)
    c14d = crdb.get("counts_14d", counts)
    completed_14 = c14d.get("completed", 0)
    triaged_14 = completed_14 + c14d.get("obsolete", 0)
    if triaged_14:
        rate = {"label": "Sinyal (gerçek÷triaj, 14g)", "value": round(completed_14 / triaged_14, 3), "n": triaged_14}
    elif triaged:
        rate = {"label": "Sinyal (gerçek÷triaj, tüm-zaman)", "value": round(completed / triaged, 3), "n": triaged}
    else:
        rate = None
    findings = crdb["findings"]
    last_file = findings[0]["title"].split(" ", 1)[0] if findings else None
    stats = {"Tick": st.get("ticks", 0), "Toplam bulgu": st.get("total_findings", 0), "Aktif": active}
    if triaged_14 and triaged:
        stats["Sinyal tüm-zaman"] = f"{round(100 * completed / triaged)}% ({triaged})"
    return {
        "key": "code-review",
        "name": "Kod-Mühendisi Ajanı",
        "role": "Kod incelemesi · öğrenme · web-research (read-only)",
        "type": "continuous",
        "schedule": "5dk döngü",
        "running": bool(st.get("enabled")),
        "models": [f"{st.get('model')} (tarama)", f"{st.get('verify_model', '?')} (kontrol/sentez)"],
        "last_run": st.get("last_run"),
        "interval_s": st.get("interval_s"),
        "current_task": (f"Son inceleme: {last_file}" if last_file else "Kuyruk/sweep bekliyor"),
        "stats": stats,
        "success_rate": rate,
        "findings": findings,
    }


def _cron_last_run(spec: dict) -> str | None:
    """Cron-ajanı son-koşu = log dosyası mtime (en güvenilir). Yoksa None."""
    for p in (spec.get("logpath"), os.path.join(_LOG_DIR, spec["log"]) if spec.get("log") else None):
        try:
            if p and os.path.exists(p):
                return datetime.fromtimestamp(os.path.getmtime(p), tz=UTC).isoformat()
        except Exception:
            pass
    return None


def _events_for(evsrc: str | None, limit: int = 5) -> list[dict]:
    """server.db events'ten kaynak-eşleşen son olaylar (cron-ajan çıktıları). Read-only."""
    if not evsrc:
        return []
    try:
        con = get_conn(server_db_path(), readonly=True)
        try:
            rows = con.execute(
                "SELECT timestamp, title, severity, detail FROM events WHERE source LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{evsrc}%", limit),
            ).fetchall()
            sevmap = {"critical": "P1", "warn": "P2"}
            return [
                {
                    "time": r["timestamp"],
                    # Codex #328-P2 r6: klipper-cron-wrap.sh title'ı jenerik basar ("cron <job>
                    # <result>"), asıl bilgi (rc + OUTCOME-satırı) detail'de — bkz emit-event.sh
                    # <type> <source> <sev> <title> [detail]. detail varsa ekle, aksi halde salt-title.
                    "title": f"{r['title']}: {r['detail']}" if r["detail"] else r["title"],
                    "severity": sevmap.get(r["severity"], ""),
                    "status": r["severity"],
                    "kind": "event",
                }
                for r in rows
            ]
        finally:
            con.close()
    except Exception:
        return []


def _discoveries_for(
    title_like: str | list[str],
    types: tuple[str, ...] = ("learning",),
    limit: int = 5,
) -> list[dict]:
    """claude_memory.db discoveries'ten başlık-eşleşen son bulgular — bazı cron-ajanların
    (ad-advisor/data-analyst/seo-audit/seo-gsc) GERÇEK çıktısı burada, server.db.events'te
    DEĞİL (_events_for onları hep 'Bulgu yok' gösteriyordu). Read-only. project SABİT
    'linux-ai-server' — self-pentest gibi çoklu-domain/hassas-güvenlik-detaylı ajanlar BU
    fonksiyonu kullanmamalı (Codex #328-P2 r1→r5: whitelist-scope + status-filtre denendi,
    KÖK-SORUN kaldı — /agents/runtime yalnız require_auth ister, dedicated pentest API
    verify_pentest_key ister; vulnerability-detayı düşük-ayrıcalıklı yoldan sızardı. self-pentest
    artık disc_like KULLANMIYOR, yalnız cron:<job> wrapper-özeti gösteriyor — bkz _cron_card).

    title_like: tek pattern veya OR'lanacak pattern listesi (birden çok başlık-deseni OR'lamak
    için)."""
    patterns = title_like if isinstance(title_like, list) else [title_like]
    try:
        con = get_conn(MEMORY_DB, readonly=True)
        try:
            placeholders = ",".join("?" for _ in types)
            title_clause = " OR ".join("title LIKE ?" for _ in patterns)
            rows = con.execute(
                f"SELECT created_at, title, type FROM discoveries "
                # Codex #328-P2 r4: status filtresi YOKTU — resolved/obsolete bulgular dashboard'da
                # sonsuza dek 'aktif' görünürdü. status='active' pentest API'nin (app/api/security.py
                # list_findings) kullandığı varsayılanla aynı.
                f"WHERE status='active' AND project='linux-ai-server' AND type IN ({placeholders}) AND ({title_clause}) "
                f"ORDER BY id DESC LIMIT ?",
                (*types, *patterns, limit),
            ).fetchall()
            return [
                {
                    "time": r["created_at"],
                    "title": r["title"],
                    "severity": "P2" if r["type"] == "bug" else "",
                    "status": r["type"],
                    "kind": "discovery",
                }
                for r in rows
            ]
        finally:
            con.close()
    except Exception:
        return []


def _pending_for(table: str, limit: int = 5) -> list[dict[str, Any]]:
    """Onay-bekleyen öneri tablosundan (ör. self_improvement_pending) bulgu listesi. Read-only.
    table: manifest-sabiti (kullanıcı girdisi değil) — f-string SQL-interpolasyonu güvenli."""
    try:
        con = get_conn(server_db_path(), readonly=True)
        try:
            rows = con.execute(
                f"SELECT created_at, title, priority FROM {table} "  # noqa: S608 (table ∈ manifest sabiti)
                f"WHERE status='pending' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            sevmap = {"high": "P1", "medium": "P2"}
            return [
                {
                    "time": r["created_at"],
                    "title": r["title"],
                    "severity": sevmap.get(r["priority"], ""),
                    "status": "pending",
                    "kind": "pending",
                }
                for r in rows
            ]
        finally:
            con.close()
    except Exception:
        return []


def _cron_success(spec: dict) -> tuple:
    """cron_outcomes'tan (job=spec['job'] veya spec['key']) son-koşu zamanı + başarı-oranı +
    en-son-koşu-OK-mu. Read-only, fail-safe. Dashboard 'success_rate: None' hardcode'u script-
    ajanları SÜS gibi gösteriyordu — gerçek pass/fail oranı cron_outcomes'ta var. 3 ajanın
    (memory-synthesize/intent-liveness-audit/autonomous-daily-summary) manifest-key'i gerçek
    job-adıyla uyuşmuyordu (bkz spec['job'] override) — bunlarda success_rate hep None kalıyordu.

    3. dönüş değeri (Codex #328-P2 r6): klipper-cron-wrap.sh yalnız RESULT!=pass'te events
    satırı yazar (satır 95-97) — bir job haftalarca-önce fail edip sonra pass'lamaya başlasa
    bile events'teki en-son satır hâlâ o ESKİ fail'e ait olur (yeni pass hiç event yazmaz).
    _cron_card bu bayrağı cron:<job> event-fallback'ini bastırmak için kullanır."""
    job = spec.get("job") or spec.get("key")
    if not job:
        return None, None, None
    try:
        con = get_conn(server_db_path(), readonly=True)
        try:
            rows = con.execute(
                "SELECT result, timestamp FROM cron_outcomes WHERE job=? ORDER BY timestamp DESC LIMIT 20",
                (job,),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return None, None, None
    if not rows:
        return None, None, None
    # #1334-sweep: bazı scriptler 'partial'ı BİLEREK "ran fine, reportable outcome" için kullanır
    # (bkz spec['partial_is_success'] — memory-synthesize DRY_RUN, intent-liveness-audit bulgu-var).
    # Diğer scriptlerde 'partial' gerçek kısmi-başarısızlık anlamına gelir (bkz meta_cognition/
    # cross_source_consolidation/vb "N öneri, discovery yazılamadı" deseni) — orada saymamak doğru.
    ok_results = {"pass", "partial"} if spec.get("partial_is_success") else {"pass"}
    ok = sum(1 for r in rows if r["result"] in ok_results)
    rate = {"label": "Cron başarısı", "value": round(ok / len(rows), 3), "n": len(rows)}
    return rows[0]["timestamp"], rate, rows[0]["result"] in ok_results


def _cron_card(spec: dict) -> dict:
    job = spec.get("job") or spec["key"]
    if spec.get("pending_table"):
        findings = _pending_for(spec["pending_table"])
    elif spec.get("disc_like"):
        findings = _discoveries_for(spec["disc_like"], types=spec.get("disc_types", ("learning",)))
    else:
        findings = _events_for(spec.get("evsrc"))
    cron_last, success_rate, latest_ok = _cron_success(spec)
    # klipper-cron-wrap.sh RESULT!=pass'te source=cron:<job> title="cron <job> <result>" event'i
    # yazar (satır 95-104 — pass'ta events satırı YOK, yalnız cron_outcomes).
    wrapper_prefix = f"cron {job} "
    if latest_ok and cron_last:
        # Codex #328-P2 r7: r6'nın latest_ok-gate'i yalnız FALLBACK-eklemeyi engelliyordu; ama
        # evsrc bare-job-adına ayarlı kartlarda (cross-source-consolidation — r4/r6'da BİLEREK
        # cron:<job>'ı yakalasın diye) PRİMARY findings zaten substring-eşleşmesiyle o eski wrapper-
        # event'ini içeriyordu, latest_ok=True olsa bile hiç filtrelenmiyordu. SIKI `< cron_last`
        # (eşit DEĞİL) kullan: partial_is_success kartlarında (memory-synthesize/intent-liveness-
        # audit) latest_ok=True İKEN dahi en-son-run'ın KENDİ wrapper-event'i (timestamp==cron_last)
        # hâlâ o run'ın gerçek/güncel özeti — bunu silme, yalnız STRICT-ESKİ olanları at.
        findings = [f for f in findings if not (f["kind"] == "event" and f["title"].startswith(wrapper_prefix) and f["time"] < cron_last)]
    elif latest_ok is False:
        # r1: disc_like/evsrc/pending_table yalnız script'in KENDİ yazdığı çıktıyı yakalar — r3:
        # findings-BOŞKEN değil HER ZAMAN kontrol et ve daha YENİYSE öne al. r6: yalnız latest_ok
        # False iken (en-son run gerçekten fail/partial) göster, aksi halde stale-fail sonsuza dek
        # 'güncel hata' gibi görünürdü.
        cron_events = _events_for(f"cron:{job}", limit=1)
        if cron_events and (not findings or cron_events[0]["time"] > findings[0]["time"]):
            findings = (cron_events + findings)[:5]
    last_run = cron_last or _cron_last_run(spec) or (findings[0]["time"] if findings else None)
    return {
        "key": spec["key"],
        "name": spec["name"],
        "role": spec["role"],
        "type": "cron",
        "schedule": spec["schedule"],
        "running": last_run is not None,
        "models": spec.get("models", ["—"]),
        "last_run": last_run,
        "interval_s": None,
        "current_task": spec["role"],
        "stats": {"Son olay": len(findings)},
        "success_rate": success_rate,
        "findings": findings,
        "triggerable": spec.get("triggerable", True),
    }


def _research_db() -> dict:
    """research ajanı: discoveries 'learning' [araştırma]% — son koşular + bulgular. Read-only."""
    try:
        con = get_conn(MEMORY_DB, readonly=True)
        try:
            rows = con.execute(
                "SELECT created_at, title FROM discoveries WHERE type='learning' AND title LIKE '[araştırma]%' ORDER BY id DESC LIMIT 6"
            ).fetchall()
            findings = [
                {"time": r["created_at"], "title": r["title"], "severity": "", "status": "active", "kind": "research"} for r in rows
            ]
            return {"findings": findings, "n": len(findings)}
        finally:
            con.close()
    except Exception:
        return {"findings": [], "n": 0}


def _research_card(spec: dict, rdb: dict) -> dict:
    findings = rdb["findings"]
    last = findings[0]["title"].replace("[araştırma] ", "") if findings else None
    return {
        "key": "research",
        "name": spec["name"],
        "role": spec["role"],
        "type": "ondemand",
        "schedule": spec["schedule"],
        "running": False,
        "models": spec["models"],
        "last_run": findings[0]["time"] if findings else None,
        "interval_s": None,
        "current_task": (f"Son araştırma: {last}" if last else "İstek bekliyor (/research/run)"),
        "stats": {"Kayıtlı araştırma": rdb["n"]},
        "success_rate": None,
        "findings": findings,
        "triggerable": False,
    }


# ── Multi-agent bus cards ────────────────────────────────────────


def _agent_bus_card(kind: str, agent) -> dict:
    """Critic / MemoryConsolidator / LearningLoop için ortak kart yapısı."""
    st = agent.status
    return {
        "key": st.get("key", kind),
        "name": st.get("name", kind),
        "role": st.get("role", ""),
        "type": "continuous",
        "schedule": f"{st.get('interval_s', '?')}s döngü",
        "running": bool(st.get("running")),
        "models": st.get("models", ["—"]),
        "last_run": st.get("last_run"),
        "interval_s": st.get("interval_s"),
        "current_task": st.get("current_task", ""),
        "stats": st.get("stats", {}),
        "success_rate": st.get("success_rate"),
        "findings": st.get("findings", []),
    }


@router.get("/runtime", dependencies=[Depends(require_auth)])
async def runtime_agents(request: Request) -> dict:
    """TÜM karar-ajanlarını tek yerde topla: sürekli(inmem) + on-demand(research) + cron.
    Her biri: last-run, iş, bulgu, model, başarı oranı, schedule, tetiklenebilir-mi."""
    agents = []
    dv = getattr(request.app.state, "devops_agent", None)
    if dv is not None:
        agents.append(_devops_card(dv))
    cra = getattr(request.app.state, "code_review_agent", None)
    if cra is not None:
        crdb = await asyncio.to_thread(_codereview_db)
        agents.append(_codereview_card(cra, crdb))
    # Multi-agent bus agents
    ca = getattr(request.app.state, "critic_agent", None)
    if ca is not None:
        agents.append(_agent_bus_card("critic", ca))
    ma = getattr(request.app.state, "memory_consolidator", None)
    if ma is not None:
        agents.append(_agent_bus_card("consolidator", ma))
    la = getattr(request.app.state, "learning_loop", None)
    if la is not None:
        agents.append(_agent_bus_card("learning", la))
    for spec in _AGENT_MANIFEST:
        if spec["type"] == "ondemand" and spec.get("src") == "research":
            rdb = await asyncio.to_thread(_research_db)
            agents.append(_research_card(spec, rdb))
        elif spec["type"] == "cron":
            agents.append(await asyncio.to_thread(_cron_card, spec))
    return {"agents": agents}


@router.get("/bus", dependencies=[Depends(require_auth)])
async def bus_status(request: Request) -> dict:
    """Agent bus internal state: subscribers, event log, registered agents."""
    bus = getattr(request.app.state, "agent_bus", None)
    if bus is None:
        from app.core.agent_bus import get_bus

        bus = get_bus()
    return {
        "bus": bus.get_status(),
        "recent_events": bus.recent_events(limit=20),
    }


@router.post("/runtime/{key}/trigger", dependencies=[Depends(require_write)])
async def trigger_agent(key: str, request: Request) -> dict:
    """Bir ajanı ELLE çalıştır (arka-plan task; HTTP hemen döner). Periyodik döngünün
    yaptığı işi şimdi tetikler. require_write (remediation/inceleme aksiyon üretebilir)."""
    if key == "devops":
        dv = getattr(request.app.state, "devops_agent", None)
        if dv is None:
            raise HTTPException(404, "devops agent aktif değil")
        asyncio.create_task(dv._tick())
        return {"triggered": "devops", "task": "izleme + remediation döngüsü"}
    if key == "code-review":
        cra = getattr(request.app.state, "code_review_agent", None)
        if cra is None:
            raise HTTPException(404, "code-review agent aktif değil")

        async def _run_review():
            # idle-gate'i atla: kuyruk + zorla sweep (elle 'şimdi incele'). last_run damgala
            # (sweep _tick dışında çağrıldığı için; dashboard manuel-koşuyu yansıtsın).
            from datetime import UTC, datetime

            cra.last_run = datetime.now(UTC).isoformat()
            await cra._drain_queue()
            await cra._sweep()

        asyncio.create_task(_run_review())
        return {"triggered": "code-review", "task": "kuyruk + sweep incelemesi"}
    if key in _CRON_SCRIPTS:
        # Cron-ajanı: allowlist'li script'i arka-planda çalıştır (manifest dışı key buraya gelmez).
        path = os.path.join(_AUTOMATION, _CRON_SCRIPTS[key])
        if not os.path.exists(path):
            raise HTTPException(404, f"script bulunamadı: {_CRON_SCRIPTS[key]}")

        async def _run_cron():
            try:
                await asyncio.to_thread(subprocess.run, ["bash", path], capture_output=True, text=True, timeout=600)
            except Exception:
                pass

        asyncio.create_task(_run_cron())
        return {"triggered": key, "task": f"cron script: {_CRON_SCRIPTS[key]}"}
    raise HTTPException(404, f"bilinmeyen ajan: {key}")


@router.get("/self-improvement/pending", dependencies=[Depends(require_auth)])
async def list_pending_suggestions(request: Request) -> dict:
    """Self-improvement onay bekleyen önerileri listele."""
    from app.db.data_layer import get_conn, server_db_path

    try:
        con = get_conn(server_db_path(), readonly=True)
        if not con:
            return {"suggestions": []}
        rows = con.execute(
            "SELECT id, title, description, priority, affected_files, created_at "
            "FROM self_improvement_pending WHERE status='pending' "
            "ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        con.close()
        return {"suggestions": [dict(r) for r in rows]}
    except Exception:
        return {"suggestions": []}


@router.post("/self-improvement/approve", dependencies=[Depends(require_write)])
async def approve_suggestion(request: Request) -> dict:
    """Bir self-improvement önerisini onayla → PR oluşturma task'i spawn et."""
    import asyncio

    body = await request.json()
    sid = body.get("id")
    if not sid:
        raise HTTPException(400, "id gerekli")

    from app.db.data_layer import get_conn, server_db_path

    try:
        con = get_conn(server_db_path(), busy_timeout_ms=10000)
        if not con:
            raise HTTPException(500, "DB bağlantı hatası")
        row = con.execute(
            "SELECT id, title, affected_files, suggestion_json FROM self_improvement_pending WHERE id=? AND status='pending'",
            (sid,),
        ).fetchone()
        if not row:
            con.close()
            raise HTTPException(404, "Öneri bulunamadı veya zaten işlenmiş")

        con.execute("UPDATE self_improvement_pending SET status='approved', approved_at=datetime('now') WHERE id=?", (sid,))
        con.commit()
        con.close()

        script = str(Path(__file__).resolve().parents[2] / "automation" / "self-improvement-pr.sh")
        import subprocess

        asyncio.create_task(
            asyncio.to_thread(
                subprocess.run,
                ["bash", script, str(sid), row["title"], row["affected_files"] or ""],
                capture_output=True,
                text=True,
                timeout=120,
            )
        )

        return {"approved": True, "id": sid, "title": row["title"]}
    except Exception as e:
        raise HTTPException(500, f"Onay hatası: {e}")


@router.post("/create", dependencies=[Depends(require_write)])
async def create_agent(body: AgentDefinition):
    agent = AgentDef(
        name=body.name,
        description=body.description,
        trigger=body.trigger,
        schedule=body.schedule,
        tools=body.tools,
        system_prompt=body.system_prompt,
        steps=body.steps,
    )
    _registry.register(agent)
    _registry.save_agent(agent.name)
    return {"created": True, "name": agent.name}


@router.get("/{name}", dependencies=[Depends(require_auth)])
async def get_agent(name: str):
    agent = _registry.get(name)
    return {
        "name": agent.name,
        "description": agent.description,
        "trigger": agent.trigger,
        "tools": agent.tools,
        "status": agent.status,
    }


@router.delete("/{name}", dependencies=[Depends(require_write)])
async def delete_agent(name: str):
    _registry.unregister(name)
    return {"deleted": True}
