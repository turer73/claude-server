"""Note classifier proxy — local Ollama (qwen2.5:7b) over HTTP.

Klipper-side autonomous mode için bu API local kullaniliyordu. Sürer
gibi remote agent'lar da Tailscale üzerinden klipper:8420 üzerinden
classify isteyebilir.

POST /api/v1/classify/note
  Body: {"title": "...", "content": "..."}
  Returns: {"label": "ACK|ACTIONABLE|DISCUSSION|URGENT", "model": "...", "duration_ms": N}
  Auth: X-Memory-Key header

Ollama localhost'a bind, Tailscale'e değil. Bu proxy klipper-internal
network'ten qwen'i remote agent'lara açar.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.memory import verify_key

router = APIRouter(prefix="/api/v1/classify", tags=["classify"])

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:7b"

CLASSIFIER_PROMPT_TEMPLATE = (
    "SYSTEM: You are a message router. Classify the note into exactly one "
    "category. Output only the category word, nothing else.\n\n"
    "Categories:\n"
    'ACK         - Acknowledgement, confirmation: "done", "ok", "received", '
    '"live", "tamam", "alindi", "tamamlandi", "calisiyor"\n'
    "ACTIONABLE  - Has explicit tasks: commit, fix, test, PR, implement, "
    'deploy, "gorev paketi", "adimlar", "basari kriteri", JSON task structure\n'
    'DISCUSSION  - Needs human decision: strategy, tradeoff, review request, '
    '"karar", "oneri", "strateji", "ne dusunuyorsun"\n'
    'URGENT      - Security/legal/incident: breach, KVKK, CVE, "saldiri", '
    '"acil", "kritik", data leak, "madde 9"\n\n'
    "Rules:\n"
    '- If title starts with "ACK" -> ACK (regardless of body)\n'
    '- If body contains JSON with "gorev_paketi" key -> ACTIONABLE\n'
    '- If title contains "URGENT" or "ACIL" -> URGENT\n'
    "- When ambiguous between ACTIONABLE and DISCUSSION -> DISCUSSION (human decides)\n"
    "- When ambiguous between ACK and anything else -> ACK\n\n"
    "Examples:\n"
    'Title: "ACK #155 - refactor live"                              -> ACK\n'
    'Title: "Gorev Paketi: bilge-arena fix" + JSON body             -> ACTIONABLE\n'
    'Title: "Hangi mimari secmeliyiz?"                              -> DISCUSSION\n'
    'Title: "KVKK breach tespit edildi"                             -> URGENT\n'
    'Title: "Phase 2 kapandi - handoff"                             -> ACK\n'
    'Title: "PR #154 review lazim"                                  -> DISCUSSION\n'
    'Title: "fix(security): CSRF bypass" + commit steps             -> ACTIONABLE\n\n'
    "--- NOTE TITLE ---\n{title}\n\n"
    "--- NOTE CONTENT (first 300 chars) ---\n{content}\n\n"
    "Category:"
)

VALID_LABELS = ("URGENT", "ACTIONABLE", "DISCUSSION", "ACK")


class ClassifyRequest(BaseModel):
    title: str
    content: str
    model: str | None = None


@router.post("/note")
async def classify_note(
    req: ClassifyRequest,
    _: None = Depends(verify_key),
) -> dict[str, Any]:
    """Sınıflandır not. Returns label + telemetry."""
    model = req.model or DEFAULT_MODEL
    content_short = req.content[:800]
    prompt = CLASSIFIER_PROMPT_TEMPLATE.format(
        title=req.title[:200],
        content=content_short,
    )

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 10},
                },
            )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"ollama upstream error: {e}") from e

    duration_ms = int((time.monotonic() - started) * 1000)
    raw_text = resp.json().get("response", "").strip().upper()

    label = "DISCUSSION"  # default safe fallback
    for candidate in VALID_LABELS:
        if candidate in raw_text:
            label = candidate
            break

    return {
        "label": label,
        "model": model,
        "duration_ms": duration_ms,
        "raw_response": raw_text[:50],
    }
