"""LLMCore — birleşik LLM arayüzü + task-bazlı routing (AIOS 'LLM Cores' uyarlaması).

Dağınık Ollama/Claude çağrılarını TEK arayüzde toplar: ``generate(prompt, task=...)``.
Task → (backend, model) routing tablosu (env-override'lı). Backend:
  - ``ollama``: yerel httpx-async ``/api/generate`` (ücretsiz, default)
  - ``claude``: ``research._anthropic_generate`` reuse (Max-abonelik OAuth/CLI — escalation)

Action/Provider desenini TAMAMLAR (Provider=context, Action=yetenek, LLMCore=model-yönlendirme).
FAIL-SILENT ("" döner) — ajan-döngüsünü asla bozmaz. Framework DEĞİL: tek-sahip server için
gerçek dedup (8 dağınık çağrı-yeri) + tek-yerden model/maliyet kontrolü, over-engineer'sız.

Routing override: ``LLM_ROUTE_<TASK>`` env = ``backend:model`` (ör. LLM_ROUTE_DIAGNOSIS=ollama:qwen2.5:7b).
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.config import read_env_var

logger = logging.getLogger(__name__)

# Task → (backend, model). Mevcut çağrı-yerlerindeki gerçek modeller (spekülasyon değil).
_TASK_ROUTES: dict[str, tuple[str, str]] = {
    "code-review": ("ollama", "qwen2.5-coder:7b"),  # code_reviewer._ask_coder
    "diagnosis": ("ollama", "qwen2.5:3b"),  # devops_agent._ask_diagnosis
    "research": ("ollama", "qwen2.5:3b"),  # research._ollama_generate
    "reasoning": ("ollama", "qwen2.5:7b"),  # daha güçlü yerel akıl-yürütme
    "classify": ("ollama", "qwen2.5:7b"),  # classifier.classify_note (DEFAULT_MODEL)
    "escalate": ("claude", "claude-haiku-4-5-20251001"),  # hızlı/ucuz Claude (Max-abonelik)
    "synthesis": ("claude", "claude-sonnet-4-6"),  # derin sentez
    "default": ("ollama", "qwen2.5:3b"),
}


class LLMCore:
    """Tek arayüz: task → backend+model yönlendirir.

    İki giriş: ``generate`` (async, ajanlar) + ``generate_sync`` (sync, FastAPI threadpool
    çağrıcıları: research/classifier). ``raise_on_error=False`` (default) → fail-silent ("",
    ajanlar bozulmaz); ``raise_on_error=True`` → istisna yükselir (API endpoint'i 502/503'e çevirir).
    """

    def __init__(self, ollama_url: str | None = None) -> None:
        self._ollama = (ollama_url or read_env_var("OLLAMA_URL") or "http://localhost:11434").rstrip("/")

    def route(self, task: str) -> tuple[str, str]:
        """task → (backend, model). Env ``LLM_ROUTE_<TASK>`` öncelikli, sonra tablo, sonra default."""
        env = read_env_var(f"LLM_ROUTE_{task.upper().replace('-', '_')}")
        if env and ":" in env:
            backend, _, model = env.partition(":")
            if backend.strip() and model.strip():
                return backend.strip(), model.strip()
        return _TASK_ROUTES.get(task, _TASK_ROUTES["default"])

    @staticmethod
    def _payload(prompt: str, model: str, system: str | None, temperature: float, num_predict: int | None) -> dict:
        options: dict = {"temperature": temperature}
        if num_predict:
            options["num_predict"] = num_predict
        payload: dict = {"model": model, "prompt": prompt, "stream": False, "options": options}
        if system:
            payload["system"] = system
        return payload

    async def generate(
        self,
        prompt: str,
        *,
        task: str = "default",
        model: str | None = None,
        system: str | None = None,
        temperature: float = 0.2,
        num_predict: int | None = None,
        timeout: int = 60,
        raise_on_error: bool = False,
    ) -> str:
        """Async üretim. Hata → "" (fail-silent) veya raise_on_error ise istisna. model routing'i ezer."""
        backend, route_model = self.route(task)
        model = model or route_model
        try:
            if backend == "claude":
                return await self._claude(system or "", prompt, model)
            return await self._ollama_async(prompt, model, system, temperature, num_predict, timeout)
        except Exception:
            if raise_on_error:
                raise
            logger.debug("LLMCore generate failed (task=%s)", task, exc_info=True)
            return ""

    async def _ollama_async(self, prompt, model, system, temperature, num_predict, timeout) -> str:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{self._ollama}/api/generate",
                json=self._payload(prompt, model, system, temperature, num_predict),
            )
        r.raise_for_status()
        return ((r.json() or {}).get("response") or "").strip()

    def generate_sync(
        self,
        prompt: str,
        *,
        task: str = "default",
        model: str | None = None,
        system: str | None = None,
        temperature: float = 0.2,
        num_predict: int | None = None,
        timeout: int = 60,
        raise_on_error: bool = False,
    ) -> str:
        """Sync üretim (requests) — FastAPI threadpool çağrıcıları için. Aynı routing/raise semantiği."""
        backend, route_model = self.route(task)
        model = model or route_model
        try:
            if backend == "claude":
                from app.api.research import _anthropic_generate

                return (_anthropic_generate(system or "", prompt, model) or "").strip()
            return self._ollama_sync(prompt, model, system, temperature, num_predict, timeout)
        except Exception:
            if raise_on_error:
                raise
            logger.debug("LLMCore generate_sync failed (task=%s)", task, exc_info=True)
            return ""

    def _ollama_sync(self, prompt, model, system, temperature, num_predict, timeout) -> str:
        import requests

        r = requests.post(
            f"{self._ollama}/api/generate",
            json=self._payload(prompt, model, system, temperature, num_predict),
            timeout=timeout,
        )
        r.raise_for_status()
        return (r.json().get("response") or "").strip()

    async def _claude(self, system: str, user: str, model: str) -> str:
        """Max-abonelik CLI yolu reuse (research._anthropic_generate, sync → to_thread)."""
        from app.api.research import _anthropic_generate

        return (await asyncio.to_thread(_anthropic_generate, system, user, model) or "").strip()


# Modül-singleton — ajanlar import edip paylaşır (tek model/maliyet kontrol noktası).
llm_core = LLMCore()
