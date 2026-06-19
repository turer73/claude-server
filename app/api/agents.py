"""Agent management API endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.agent_system import AgentDefinition as AgentDef
from app.core.agent_system import AgentRegistry
from app.db.data_layer import MEMORY_DB, get_conn
from app.middleware.dependencies import require_auth, require_write
from app.models.schemas import AgentDefinition

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
        "running": bool(st.get("enabled")),
        "models": [f"{st.get('model')} (review)", "qwen2.5:3b (research)"],
        "last_run": st.get("last_run"),
        "interval_s": st.get("interval_s"),
        "current_task": (f"Son inceleme: {last_file}" if last_file else "Kuyruk/sweep bekliyor"),
        "stats": {"Tick": st.get("ticks", 0), "Toplam bulgu": st.get("total_findings", 0), "Aktif": active},
        "success_rate": ({"label": "Sinyal (FP-değil)", "value": round(active / total, 3), "n": total} if total else None),
        "findings": findings,
    }


@router.get("/runtime", dependencies=[Depends(require_auth)])
async def runtime_agents(request: Request) -> dict:
    """Tüm canlı arka-plan ajanlarını tek yerde topla: last-run, iş, bulgu, model, başarı oranı."""
    agents = []
    dv = getattr(request.app.state, "devops_agent", None)
    if dv is not None:
        agents.append(_devops_card(dv))
    cra = getattr(request.app.state, "code_review_agent", None)
    if cra is not None:
        crdb = await asyncio.to_thread(_codereview_db)
        agents.append(_codereview_card(cra, crdb))
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
