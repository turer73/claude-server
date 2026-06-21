"""Agent management API endpoints."""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.agent_system import AgentDefinition as AgentDef
from app.core.agent_system import AgentRegistry
from app.db.data_layer import MEMORY_DB, get_conn, server_db_path
from app.middleware.dependencies import require_auth, require_write
from app.models.schemas import AgentDefinition

_LOG_DIR = "/var/log/linux-ai-server"
_AUTOMATION = "/opt/linux-ai-server/automation"

# Karar-ajanları manifesti — sürekli(inmem) dışındaki on-demand + cron ajanları.
# type: ondemand(research) | cron(log mtime + events). script: manuel-tetikleme (allowlist).
_AGENT_MANIFEST = [
    {
        "key": "research",
        "name": "Araştırma Ajanı",
        "role": "İnternet araştırma · grounding · sentez",
        "type": "ondemand",
        "schedule": "istek-üzerine",
        "models": ["qwen2.5:3b / aya:8b", "claude CLI (sentez)"],
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
        "schedule": "günlük",
        "models": ["veri-script"],
        "log": "data-analyst.log",
        "evsrc": "data-analyst",
        "script": "data-analyst.sh",
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
    },
    {
        "key": "seo-gsc",
        "name": "SEO Search Console",
        "role": "GSC sıralama/tıklama takibi",
        "type": "cron",
        "schedule": "günlük",
        "models": ["GSC API"],
        "log": "seo-gsc.log",
        "evsrc": "seo-gsc",
        "script": "seo-gsc.sh",
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
        "schedule": "günlük",
        "models": ["kural-tabanlı"],
        "log": "memory-synth.log",
        "evsrc": "memory-synth",
        "script": "memory-synthesize.sh",
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
        "key": "intent-liveness-audit",
        "name": "Niyet-Canlılık Denetçi",
        "role": "Cron/intent canlılık denetimi",
        "type": "cron",
        "schedule": "günlük",
        "models": ["kural-tabanlı"],
        "log": "intent-liveness.log",
        "evsrc": "intent-liveness",
        "script": "intent-liveness-audit.sh",
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
    },
]
_CRON_SCRIPTS = {a["key"]: a["script"] for a in _AGENT_MANIFEST if a.get("script")}

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
    """code-review discoveries: son bulgular + active/obsolete sayımı (sinyal-oranı). Read-only."""
    try:
        con = get_conn(MEMORY_DB, readonly=True)
        try:
            counts: dict[str, int] = {}
            for status, n in con.execute(
                "SELECT status, COUNT(*) FROM discoveries WHERE project='code-review' AND type='bug' GROUP BY status"
            ).fetchall():
                counts[status] = n
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
            return {"counts": counts, "findings": findings}
        finally:
            con.close()
    except Exception:
        return {"counts": {}, "findings": []}


def _devops_card(dv) -> dict:
    st = dv.status
    log = list(getattr(dv, "_remediation_log", []))
    total = len(log)
    succ = sum(1 for r in log if getattr(r, "success", False))
    findings = [
        {
            "time": getattr(r, "timestamp", None),
            "title": f"{getattr(r, 'alert_source', '?')} → {getattr(r, 'action', '?')}",
            "severity": "P3" if getattr(r, "success", False) else "P1",
            "status": "pass" if getattr(r, "success", False) else "fail",
            "kind": "remediation",
        }
        for r in log[-8:][::-1]
    ]
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
    total = active + counts.get("obsolete", 0)
    findings = crdb["findings"]
    last_file = findings[0]["title"].split(" ", 1)[0] if findings else None
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
        "stats": {"Tick": st.get("ticks", 0), "Toplam bulgu": st.get("total_findings", 0), "Aktif": active},
        "success_rate": ({"label": "Sinyal (FP-değil)", "value": round(active / total, 3), "n": total} if total else None),
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
                "SELECT timestamp, title, severity FROM events WHERE source LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{evsrc}%", limit),
            ).fetchall()
            sevmap = {"critical": "P1", "warn": "P2"}
            return [
                {
                    "time": r["timestamp"],
                    "title": r["title"],
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


def _cron_card(spec: dict) -> dict:
    findings = _events_for(spec.get("evsrc"))
    last_run = _cron_last_run(spec) or (findings[0]["time"] if findings else None)
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
        "success_rate": None,
        "findings": findings,
        "triggerable": True,
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
    for spec in _AGENT_MANIFEST:
        if spec["type"] == "ondemand" and spec.get("src") == "research":
            rdb = await asyncio.to_thread(_research_db)
            agents.append(_research_card(spec, rdb))
        elif spec["type"] == "cron":
            agents.append(await asyncio.to_thread(_cron_card, spec))
    return {"agents": agents}


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
