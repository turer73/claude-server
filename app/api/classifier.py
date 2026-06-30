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

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.memory import verify_key
from app.core.agents.llmcore import llm_core

router = APIRouter(prefix="/api/v1/classify", tags=["classify"])

DEFAULT_MODEL = "qwen2.5:7b"

CLASSIFIER_PROMPT_TEMPLATE = (
    "SYSTEM: You are a message router. Classify the note into exactly one "
    "category. Output only the category word, nothing else.\n\n"
    "Categories:\n"
    'ACK         - Acknowledgement, confirmation: "done", "ok", "received", '
    '"live", "tamam", "alindi", "tamamlandi", "calisiyor"\n'
    "ACTIONABLE  - Has explicit tasks: commit, fix, test, PR, implement, "
    'deploy, "gorev paketi", "adimlar", "basari kriteri", JSON task structure\n'
    "DISCUSSION  - Needs human decision: strategy, tradeoff, review request, "
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
    'Respond ONLY with JSON: {{"label": "<CATEGORY>"}}'
)

VALID_LABELS = ("URGENT", "ACTIONABLE", "DISCUSSION", "ACK")

# klipper #100224: Ollama structured-output — model'i enum'a kısıtla → garantili-geçerli JSON,
# substring-match FP'si yok ("ACTIONABLE ama DISCUSSION" gibi belirsiz çıktı yanlış-eşleşmez).
LABEL_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": list(VALID_LABELS)}},
    "required": ["label"],
}


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
        raw_text = await llm_core.generate(
            prompt, task="classify", model=model, temperature=0.1, num_predict=30, timeout=20, raise_on_error=True, fmt=LABEL_SCHEMA
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ollama upstream error: {e}") from e

    duration_ms = int((time.monotonic() - started) * 1000)

    # Structured-output: {"label": "..."} parse et; format desteklenmezse (eski Ollama) substring-fallback.
    label = "DISCUSSION"  # default safe fallback
    try:
        cand = str(json.loads(raw_text).get("label", "")).upper()
        if cand in VALID_LABELS:
            label = cand
    except (json.JSONDecodeError, AttributeError, TypeError):
        up = raw_text.upper()
        for candidate in VALID_LABELS:
            if candidate in up:
                label = candidate
                break

    return {
        "label": label,
        "model": model,
        "duration_ms": duration_ms,
        "raw_response": raw_text[:50],
    }
