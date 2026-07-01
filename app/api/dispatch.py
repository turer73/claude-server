from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.memory import verify_key
from app.core.config import get_settings, read_env_var
from app.core.shell_executor import ShellExecutor
from app.exceptions import AuthorizationError, ShellExecutionError

router = APIRouter(prefix="/api/v1/dispatch", tags=["dispatch"])

MODEL = "qwen2.5:7b"  # LLMCore.chat'e model= ile geçer

# GUVENLIK (Codex P1b): whitelist'te olsa da arg'iyla keyfi kod calistiran yorumlayici/
# wrapper komutlar — LLM-uretimi girdide _run_klipper_cmd bunlari reddeder.
_INTERP_DENY = frozenset(
    {
        "bash",
        "sh",
        "dash",
        "zsh",
        "ksh",
        "fish",
        "csh",
        "tcsh",
        "python",
        "python2",
        "python3",
        "perl",
        "ruby",
        "node",
        "php",
        "lua",
        "tclsh",
        "env",
        "xargs",
        "sudo",
        "ssh",
        "eval",
        "exec",
        "source",
        "awk",
        "gawk",
        "mawk",
        "sed",
        "nc",
        "ncat",
        "netcat",
        "socat",
        "telnet",
    }
)

ANALYZER_SYSTEM = (
    "Gorev analizci. JSON formatinda donus yap:\n"
    '{"route": "KLIPPER|SURER|HYBRID", "klipper_cmds": ["cmd1"], '
    '"surer_tasks": [{"dosya": "...", "degisiklik": "..."}], "proje": "...", "ozet": "tek cumle"}'
)

# klipper #100224 structured-output: Ollama'yı geçerli analiz-objesine kısıtla → kırılgan
# regex-ayıklama (re.search(r"\{...\}")) yerine temiz json.loads. route enum-kısıtlı (geçersiz
# yönlendirme imkânsız). Ollama yok-sayarsa / claude-route'ta → regex-fallback korunur.
_ANALYZE_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": ["KLIPPER", "SURER", "HYBRID"]},
        "klipper_cmds": {"type": "array", "items": {"type": "string"}},
        "surer_tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"dosya": {"type": "string"}, "degisiklik": {"type": "string"}},
            },
        },
        "proje": {"type": "string"},
        "ozet": {"type": "string"},
    },
    # Codex #235 P2: klipper_cmds/surer_tasks ZORUNLU (boş dizi serbest). Aksi halde
    # KLIPPER/HYBRID yanıtı klipper_cmds'i tümden atlayabilir → dispatch_task hiçbir şey
    # çalıştırmaz ama "başarılı" döner (routed no-op); SURER detayı da sessizce düşer.
    "required": ["route", "klipper_cmds", "surer_tasks", "ozet"],
}


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
    analysis: dict[str, Any] = {}
    duration_ms: int = 0


async def _ollama_chat(user_msg: str, system: str = "", fmt: dict[str, Any] | None = None) -> str:
    from app.core.agents.llmcore import llm_core

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_msg})
    return await llm_core.chat(messages, model=MODEL, timeout=30, raise_on_error=True, fmt=fmt)


def _quick_route(task: str) -> str:
    """Kural-tabanli hizli yonlendirme — ML oncesi."""
    t = task.lower()
    klipper_kws = [
        "bash ",
        "shell ",
        "servis restart",
        "log bak",
        "docker ps",
        "git log",
        "git status",
        "memory yaz",
        "telegram",
        "cron ",
        "discovery kaydet",
        "ollama",
        "komut calistir",
    ]
    surer_kws = [
        "dosyasini duzenle",
        "pr ac",
        "commit at",
        "feature ekle",
        ".tsx",
        ".ts dosya",
        "react ",
        "typescript",
        "component yaz",
        "kod yaz",
        "bug fix",
        "implement",
    ]
    if any(k in t for k in klipper_kws):
        return "KLIPPER"
    if any(k in t for k in surer_kws):
        return "SURER"
    return ""


async def _analyze_task(task: str, project: str, context: str) -> dict[str, Any]:
    prompt = f"Gorev: {task}\nProje: {project or 'belirtilmedi'}\nEk bilgi: {context or 'yok'}\n\nAnaliz et ve JSON donus yap."
    try:
        raw = await _ollama_chat(prompt, system=ANALYZER_SYSTEM, fmt=_ANALYZE_SCHEMA)
    except Exception:
        # Codex #235 P2: endpoint JSON-schema fmt'i desteklemiyorsa (eski Ollama / reddediyor)
        # _ollama_chat raise_on_error=True raise eder → dispatch tümden 500 olurdu. fmt'siz
        # retry → serbest-metin gelir, aşağıdaki regex-fallback ayıklar (graceful-degrade).
        # İkinci hata (Ollama gerçekten down) → raw="" → default-SURER return.
        try:
            raw = await _ollama_chat(prompt, system=ANALYZER_SYSTEM)
        except Exception:
            raw = ""
    if raw:
        # Structured-output: temiz JSON objesi → doğrudan parse. Ollama yok-sayarsa /
        # claude-route'ta serbest-metin → regex-ayıkla (eski davranış, fail-safe fallback).
        try:
            parsed: dict[str, Any] = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        m = re.search(r"\{[\s\S]+\}", raw)
        if m:
            try:
                parsed = json.loads(m.group(0))
                if isinstance(parsed, dict):
                    return parsed
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
    """Klipper bash komutunu projenin whitelisted ShellExecutor'i ile calistir.

    GUVENLIK: komut qwen2.5:7b (LLM) tarafindan uretiliyor → bespoke denylist YERINE
    audit edilmis ShellExecutor: (1) ilk-komut whitelist (shell_whitelist disindaki
    bilinmeyen komut reddedilir — denylist'in tersi, cok daha guclu), (2) katastrofik
    desen blogu (rm -rf /, chmod -R / vb. regex). RCE-yuzeyini daraltir.

    Codex P1a: ShellExecutor whitelist'i YALNIZ ilk komutu kontrol eder → `df; nmap`
    gibi zincir whitelist'i bypass eder. LLM-uretimi girdide shell-zincirleme/yonlendirme
    meta-karakterlerini REDDET → tek-komut zorla (boylece whitelist tum komutu kapsar).

    Codex P1b: whitelisted yorumlayici/wrapper (bash -c, python -c, env, xargs, sudo,
    awk/perl-system, find -exec) keyfi kod calistirir → whitelist'i deler. Ilk-komut
    bunlardan biriyse REDDET (genis admin-whitelist'inin temel sizintisini kapatir)."""
    if re.search(r"[;&|`<>\n]|\$\(", cmd):
        return f"BLOCKED: komut zincirleme/yonlendirme yasak (tek komut ver): {cmd[:60]}"
    base = cmd.strip().split()[0].rsplit("/", 1)[-1] if cmd.strip() else ""
    if base in _INTERP_DENY:
        return f"BLOCKED: yorumlayici/wrapper komut yasak ({base})"
    if base == "find" and "-exec" in cmd:
        return "BLOCKED: find -exec yasak (keyfi kod)"
    executor = ShellExecutor(whitelist=get_settings().shell_whitelist)
    try:
        result = await executor.execute(cmd, timeout=30)
    except AuthorizationError as e:
        return f"BLOCKED (whitelist/desen): {str(e)[:80]}"
    except ShellExecutionError as e:
        return f"HATA: {str(e)[:80]}"
    out = (result.get("stdout", "") + result.get("stderr", "")).strip()
    return out[:500] if out else "(cikti yok)"


async def _send_to_surer(analysis: dict[str, Any], task: str, project: str, context: str) -> int:
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
        note_id: int = resp.json().get("id", 0)
        return note_id


@router.post("/task", response_model=DispatchResult, dependencies=[Depends(verify_key)])
async def dispatch_task(body: DispatchRequest) -> DispatchResult:
    t0 = time.monotonic()

    # 1. Hizli kural tabanli yonlendirme
    quick = _quick_route(body.task)

    # 2. qwen2.5:7b derin analiz (klipper_cmds + surer_tasks icin her zaman)
    analysis = await _analyze_task(body.task, body.project, body.context)

    # 3. Kural bos ise ML karari kullan
    route = quick or analysis.get("route", "SURER")

    # Codex #238: KLIPPER/HYBRID yönlendirmesi ama klipper_cmds BOŞ → routed-no-op
    # (sahte-başarı, sessiz-drop). Olur: (a) fmt-less analiz schema-kısıtsız
    # {"route":"KLIPPER"} döner (required-arrays uygulanmaz), (b) quick-route KLIPPER der
    # ama analiz (Ollama down) komut üretemez. Çalıştıracak komut yoksa SURER'e düşür.
    klipper_cmds = analysis.get("klipper_cmds") or []
    if route in ("KLIPPER", "HYBRID") and not klipper_cmds:
        route = "SURER"

    klipper_results: list[str] = []
    surer_note_id: int | None = None

    # 4. Klipper komutlarini uygula
    if route in ("KLIPPER", "HYBRID"):
        for cmd in klipper_cmds[:5]:
            out = await _run_klipper_cmd(cmd)
            klipper_results.append(f"$ {cmd[:60]}\n{out[:200]}")

    # 5. Surer'a gorev paketi gonder
    if route in ("SURER", "HYBRID"):
        try:
            surer_note_id = await _send_to_surer(analysis, body.task, body.project, body.context)
        except Exception as e:
            klipper_results.append(f"SURER-SEND-HATA: {e}")

    routed_to = "klipper" if route == "KLIPPER" else ("surer" if route == "SURER" else "both")
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
