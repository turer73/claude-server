"""AI inference API endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.ai_inference import AIInference
from app.models.schemas import AIChatRequest, AIChatResponse

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])

_ai = AIInference()


@router.post("/chat", response_model=AIChatResponse)
async def ai_chat(body: AIChatRequest):
    result = await _ai.chat(
        message=body.message,
        model=body.model,
        context=[dict(m) for m in body.context] if body.context else None,
    )
    return AIChatResponse(**result)


@router.get("/models")
async def list_models():
    models = await _ai.list_models()
    return {"models": models}
