"""AI inference -- Ollama API client."""

from __future__ import annotations

import time

import httpx

from app.exceptions import ServerError


class AIInference:
    """Client for Ollama REST API."""

    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        self._base_url = base_url

    async def chat(
        self,
        message: str,
        model: str = "linux-ai-agent",
        context: list[dict[str, str]] | None = None,
    ) -> dict:
        messages = context or []
        messages.append({"role": "user", "content": message})

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{self._base_url}/api/chat",
                    json={"model": model, "messages": messages, "stream": False},
                )
            elapsed = (time.monotonic() - start) * 1000
            data = resp.json()
            return {
                "response": data.get("message", {}).get("content", ""),
                "model": data.get("model", model),
                "elapsed_ms": round(elapsed, 1),
            }
        except Exception as e:
            raise ServerError(f"AI inference failed: {e}")

    async def list_models(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
            return resp.json().get("models", [])
        except Exception:
            return []
