"""LLM status — local Ollama + Vulkan/GPU + Anthropic + RAG usage stats.

Dashboard'in 'LLM' tab'i bu endpoint'i cagirir. Tum bilgi tek round-trip'te
toplanir. require_admin auth (X-API-Key veya Bearer JWT).
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

import requests
from fastapi import APIRouter, Depends

from app.api import rag as rag_module
from app.core.config import read_env_var
from app.middleware.dependencies import require_admin

OLLAMA_URL = "http://127.0.0.1:11434"
ANTHROPIC_KEY = read_env_var("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
GPU_BUSY_PATHS = [
    "/sys/class/drm/card1/device/gpu_busy_percent",
    "/sys/class/drm/card0/device/gpu_busy_percent",
]

router = APIRouter(prefix="/api/v1/llm", tags=["llm"])


def _ollama_models() -> dict[str, Any]:
    """Ollama'da yuklu modeller + boyut + modified + currently loaded."""
    out: dict[str, Any] = {"ok": False, "models": [], "loaded": []}
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        if r.ok:
            out["ok"] = True
            for m in r.json().get("models", []):
                out["models"].append(
                    {
                        "name": m.get("name"),
                        "size_mb": round(m.get("size", 0) / 1024 / 1024, 1),
                        "family": m.get("details", {}).get("family"),
                        "param_size": m.get("details", {}).get("parameter_size"),
                        "quant": m.get("details", {}).get("quantization_level"),
                        "modified": m.get("modified_at"),
                    }
                )
    except Exception as e:
        out["error"] = str(e)[:100]

    try:
        r = requests.get(f"{OLLAMA_URL}/api/ps", timeout=3)
        if r.ok:
            for m in r.json().get("models", []):
                out["loaded"].append(
                    {
                        "name": m.get("name"),
                        "vram_mb": round(m.get("size_vram", 0) / 1024 / 1024, 1),
                        "size_mb": round(m.get("size", 0) / 1024 / 1024, 1),
                        "expires_at": m.get("expires_at"),
                    }
                )
    except Exception:
        pass

    return out


def _gpu_status() -> dict[str, Any]:
    """Vulkan/GPU runtime — systemctl env probe + busy percent + Ollama log probe.

    Ollama process /proc/<pid>/environ permission 0400 ollama:ollama
    klipperos kullanicisi okuyamaz; bunun yerine 'systemctl show' env'i
    okur ve son Ollama startup log'unda 'library=Vulkan' satirini arar.
    """
    import subprocess

    info: dict[str, Any] = {
        "vulkan_enabled": False,
        "backend": "cpu",
        "gpu_name": None,
        "busy_percent": None,
        "shared_vram_gib": None,
    }

    # 1. systemctl show -p Environment ollama
    try:
        out = subprocess.run(
            ["systemctl", "show", "ollama", "-p", "Environment", "--value"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0 and "OLLAMA_VULKAN=1" in out.stdout:
            info["vulkan_enabled"] = True
    except Exception:
        pass

    # 2. journalctl son 'inference compute' satirinda library=Vulkan goz at
    if info["vulkan_enabled"]:
        try:
            out = subprocess.run(
                ["journalctl", "-u", "ollama", "--grep", "inference compute", "-n", "5", "--no-pager"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in out.stdout.splitlines():
                if "library=Vulkan" in line:
                    info["backend"] = "vulkan"
                    # description="AMD Radeon ..." parse et
                    if 'description="' in line:
                        start = line.index('description="') + len('description="')
                        end = line.index('"', start)
                        info["gpu_name"] = line[start:end]
                    # total="17.3 GiB" parse
                    if 'total="' in line:
                        start = line.index('total="') + len('total="')
                        end = line.index('"', start)
                        info["shared_vram_gib"] = line[start:end]
                    break
        except Exception:
            pass

    # 3. GPU busy% sysfs
    for path in GPU_BUSY_PATHS:
        try:
            with open(path) as f:
                info["busy_percent"] = int(f.read().strip())
                break
        except Exception:
            continue

    return info


def _anthropic_status() -> dict[str, Any]:
    return {
        "configured": bool(ANTHROPIC_KEY),
        "model": ANTHROPIC_MODEL,
        "fallback_threshold": "kaynak>=8 -> claude (auto mode, research.py)",
    }


def _usage_stats(hours: int = 24) -> dict[str, Any]:
    """rag_metrics.db'den son N saat istatistik."""
    out: dict[str, Any] = {"ok": False, "period_hours": hours, "total": 0}
    since = int(time.time()) - hours * 3600
    try:
        conn = sqlite3.connect(rag_module.METRICS_DB, timeout=2)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*), AVG(duration_ms), AVG(hit_count), AVG(top_score) FROM rag_queries WHERE ts >= ?",
            (since,),
        )
        total, avg_dur, avg_hits, avg_score = cur.fetchone()
        out.update(
            {
                "ok": True,
                "total": total or 0,
                "avg_duration_ms": round(avg_dur or 0, 1),
                "avg_hit_count": round(avg_hits or 0, 2),
                "avg_top_score": round(avg_score or 0, 3),
            }
        )
        cur.execute(
            "SELECT endpoint, COUNT(*) FROM rag_queries WHERE ts >= ? GROUP BY endpoint",
            (since,),
        )
        out["by_endpoint"] = dict(cur.fetchall())
        conn.close()
    except Exception as e:
        out["error"] = str(e)[:100]
    return out


@router.get("/status", dependencies=[Depends(require_admin)])
def llm_status():
    """Aggregate LLM topology + health + usage. Dashboard kullanir."""
    return {
        "ollama": _ollama_models(),
        "gpu": _gpu_status(),
        "anthropic": _anthropic_status(),
        "usage_last_24h": _usage_stats(24),
    }
