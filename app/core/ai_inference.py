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
        model: str = "qwen3:1.7b",
        context: list[dict[str, str]] | None = None,
        think: bool = False,
    ) -> dict:
        from app.core.agents.llmcore import llm_core

        messages = context or []
        # Disable thinking mode by default for speed on low-end hardware
        if not think and message and not message.startswith("/think"):
            message = f"/no_think {message}"
        messages.append({"role": "user", "content": message})

        # Transport LLMCore.chat üzerinden (tek choke-point); base_url list_models'a uygulanır.
        start = time.monotonic()
        try:
            content = await llm_core.chat(messages, model=model, timeout=300, raise_on_error=True)
        except Exception as e:
            raise ServerError(f"AI inference failed: {e}")
        return {
            "response": content,
            "model": model,
            "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
        }

    async def list_models(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
            return resp.json().get("models", [])
        except Exception:
            return []
