from __future__ import annotations

import json
import os
import re
import subprocess
import time

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.config import read_env_var

try:
    from app.api.memory import verify_key
except ImportError:
    async def verify_key():
        pass

router = APIRouter(prefix="/api/v1/dispatch", tags=["dispatch"])

OLLAMA_URL = "http://127.0.0.1:11434"
MODEL = "qwen2.5:7b"

ANALYZER_SYSTEM = (
    "Gorev analizci. JSON formatinda donus yap:\n"
    '{"route": "KLIPPER|SURER|HYBRID", "klipper_cmds": ["cmd1"], '
    '"surer_tasks": [{"dosya": "...", "degisiklik": "..."}], "proje": "...", "ozet": "tek cumle"}'
)


class DispatchRequest(BaseModel):
    task: str
    project: str = ""
    source: str = "user"
    context: str = ""


class DispatchResult(BaseModel):
    routed_to: str
    project: str
    summary: str
    klipper_results: list[str] = []
    surer_note_id: int | None = None
    analysis: dict = {}
    duration_ms: int = 0


async def _ollama_chat(user_msg: str, system: str = "") -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_msg})
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json={"model": MODEL, "stream": False, "messages": messages},
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()


def _quick_route(task: str) -> str:
    """Kural-tabanli hizli yonlendirme — ML oncesi."""
    t = task.lower()
    klipper_kws = [
        "bash ", "shell ", "servis restart", "log bak", "docker ps",
        "git log", "git status", "memory yaz", "telegram", "cron ",
        "discovery kaydet", "ollama", "komut calistir",
    ]
    surer_kws = [
        "dosyasini duzenle", "pr ac", "commit at", "feature ekle",
        ".tsx", ".ts dosya", "react ", "typescript", "component yaz",
        "kod yaz", "bug fix", "implement",
    ]
    if any(k in t for k in klipper_kws):
        return "KLIPPER"
    if any(k in t for k in surer_kws):
        return "SURER"
    return ""


async def _analyze_task(task: str, project: str, context: str) -> dict:
    prompt = (
        f"Gorev: {task}\n"
        f"Proje: {project or 'belirtilmedi'}\n"
        f"Ek bilgi: {context or 'yok'}\n\n"
        "Analiz et ve JSON donus yap."
    )
    raw = await _ollama_chat(prompt, system=ANALYZER_SYSTEM)
    m = re.search(r"\{[\s\S]+\}", raw)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {
        "route": "SURER",
        "klipper_cmds": [],
        "surer_tasks": [],
        "proje": project,
        "ozet": task[:100],
    }


async def _run_klipper_cmd(cmd: str) -> str:
    """Klipper bash komutlarini guvenli subset ile calistir."""
    BLOCKED = ["rm -rf", "dd ", "mkfs", "chmod 777", "> /dev/", "shutdown", "reboot"]
    for b in BLOCKED:
        if b in cmd:
            return f"BLOCKED: guvenli degil: {cmd[:50]}"
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        out = (result.stdout + result.stderr).strip()
        return out[:500] if out else "(cikti yok)"
    except subprocess.TimeoutExpired:
        return "TIMEOUT: 30sn asimdi"
    except Exception as e:
        return f"HATA: {e}"


async def _send_to_surer(
    analysis: dict, task: str, project: str, context: str
) -> int:
    """Surer-sonnet'e yapilandirilmis gorev paketi gonder."""
    mem_key = read_env_var("MEMORY_API_KEY")
    mem_url = "http://127.0.0.1:8420/api/v1/memory/notes"
    proje = analysis.get("proje", project) or "genel"
    ozet = analysis.get("ozet", task[:100])
    content = json.dumps(
        {
            "tip": "gorev_paketi",
            "gonderen": "klipper-dispatcher",
            "alici": "surer-sonnet",
            "proje": proje,
            "ozet": ozet,
            "gorev": task[:500],
            "degisiklikler": analysis.get("surer_tasks", []),
            "basari_kriteri": f"{proje} gorevi tamamlandi, testler gecti",
            "context": context[:200] if context else "",
        },
        ensure_ascii=False,
    )
    title = f"Gorev Paketi: {proje} — {ozet[:60]}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            mem_url,
            headers={"X-Memory-Key": mem_key, "Content-Type": "application/json"},
            json={"from_device": "klipper", "title": title, "content": content},
        )
        resp.raise_for_status()
        return resp.json().get("id", 0)


@router.post("/task", response_model=DispatchResult, dependencies=[Depends(verify_key)])
async def dispatch_task(body: DispatchRequest) -> DispatchResult:
    t0 = time.monotonic()

    # 1. Hizli kural tabanli yonlendirme
    quick = _quick_route(body.task)

    # 2. qwen2.5:7b derin analiz (klipper_cmds + surer_tasks icin her zaman)
    analysis = await _analyze_task(body.task, body.project, body.context)

    # 3. Kural bos ise ML karari kullan
    route = quick or analysis.get("route", "SURER")

    klipper_results: list[str] = []
    surer_note_id: int | None = None

    # 4. Klipper komutlarini uygula
    if route in ("KLIPPER", "HYBRID"):
        for cmd in analysis.get("klipper_cmds", [])[:5]:
            out = await _run_klipper_cmd(cmd)
            klipper_results.append(f"$ {cmd[:60]}\n{out[:200]}")

    # 5. Surer'a gorev paketi gonder
    if route in ("SURER", "HYBRID"):
        try:
            surer_note_id = await _send_to_surer(
                analysis, body.task, body.project, body.context
            )
        except Exception as e:
            klipper_results.append(f"SURER-SEND-HATA: {e}")

    routed_to = (
        "klipper" if route == "KLIPPER" else ("surer" if route == "SURER" else "both")
    )
    ms = int((time.monotonic() - t0) * 1000)

    return DispatchResult(
        routed_to=routed_to,
        project=analysis.get("proje", body.project),
        summary=analysis.get("ozet", body.task[:80]),
        klipper_results=klipper_results,
        surer_note_id=surer_note_id,
        analysis=analysis,
        duration_ms=ms,
    )
