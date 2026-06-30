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
import threading
from contextlib import asynccontextmanager, contextmanager

import httpx

from app.core.config import read_env_var

logger = logging.getLogger(__name__)

# #2 Öncelik: bu task'lar incident/interaktif → rutin-işi geçer (rezerv-permit ile). Çağrıcı
# `priority="high"` ile de zorlayabilir. (Tam-scheduler DEĞİL — vanaya rezerv-lane, bounded.)
_HIGH_PRIORITY_TASKS = {"diagnosis"}

# Kaynak-vanası (AIOS scheduler'ın kernel-DIŞI bounded parçası): aynı anda en çok N YEREL-ollama
# çağrısı (CPU-saturate önler — 14 ajan bağımsız ateşliyor, hafıza: recursive-grep/zombie/cpu-FP).
# Claude CLI MUAF (abonelik, yerel-CPU yemiyor). Async + sync yol AYRI semafor (worst-case 2N;
# pratikte düşük çünkü code-review/research kendi içlerinde sıralı). NOT: doğrudan ollama'ya curl
# atan bash-cron ajanları (ad-advisor vb.) LLMCore'dan geçmez → bu vana onları KAPSAMAZ.
_OLLAMA_CONCURRENCY = max(1, int(read_env_var("LLM_OLLAMA_CONCURRENCY") or "2"))

# Task → (backend, model). Mevcut çağrı-yerlerindeki gerçek modeller (spekülasyon değil).
_TASK_ROUTES: dict[str, tuple[str, str]] = {
    "code-review": ("ollama", "qwen2.5-coder:7b"),  # code_reviewer._ask_coder
    "diagnosis": ("ollama", "qwen2.5:3b"),  # devops_agent._ask_diagnosis
    "research": ("ollama", "qwen2.5:3b"),  # research._ollama_generate
    "reasoning": ("ollama", "qwen2.5:7b"),  # daha güçlü yerel akıl-yürütme
    "classify": ("ollama", "qwen2.5:7b"),  # classifier.classify_note (DEFAULT_MODEL)
    "rag": ("ollama", "qwen2.5:3b"),  # rag.ask (model çağrıcıdan; complete_sync)
    "escalate": ("claude", "claude-haiku-4-5-20251001"),  # hızlı/ucuz Claude (Max-abonelik)
    "verify": ("claude", "claude-haiku-4-5-20251001"),  # #4 adversarial-verify: qwen-coder kendi FP'sini çürütemiyor → güçlü model
    "synthesis": ("claude", "claude-sonnet-4-6"),  # derin sentez
    "default": ("ollama", "qwen2.5:3b"),
}


def _record_llm_call(task: str, backend: str, model: str, latency_ms: float, ok: bool, tokens: int | None = None) -> None:
    """LLM çağrısını rag_metrics.db/llm_calls'a kaydet — merkezi LLM-gözlemlenebilirlik
    (#100224-audit: 9 çağrı-yeri gözlemsizdi; tek choke-point burada). task/backend/model/
    latency/ok/tokens → 'hangi ajan GPU'yu yakıyor', 'ne sıklıkla Claude'a eskale ediyoruz'
    sorularını yanıtlar. FAIL-SAFE: metrik-yazımı ASLA LLM-çağrısını bozmaz."""
    try:
        import sqlite3

        db = read_env_var("RAG_METRICS_DB") or "/opt/linux-ai-server/data/rag_metrics.db"
        conn = sqlite3.connect(db, timeout=2)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS llm_calls ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT (datetime('now')), "
                "task TEXT, backend TEXT, model TEXT, latency_ms INTEGER, tokens INTEGER, ok INTEGER)"
            )
            conn.execute(
                "INSERT INTO llm_calls (task, backend, model, latency_ms, tokens, ok) VALUES (?, ?, ?, ?, ?, ?)",
                (task, backend, model, int(latency_ms), tokens, 1 if ok else 0),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.debug("llm_calls metric write failed", exc_info=True)


class LLMCore:
    """Tek arayüz: task → backend+model yönlendirir.

    İki giriş: ``generate`` (async, ajanlar) + ``generate_sync`` (sync, FastAPI threadpool
    çağrıcıları: research/classifier). ``raise_on_error=False`` (default) → fail-silent ("",
    ajanlar bozulmaz); ``raise_on_error=True`` → istisna yükselir (API endpoint'i 502/503'e çevirir).
    """

    def __init__(self, ollama_url: str | None = None) -> None:
        self._ollama = (ollama_url or read_env_var("OLLAMA_URL") or "http://localhost:11434").rstrip("/")
        # Yerel-ollama eşzamanlılık vanaları (claude muaf). asyncio.Semaphore loop'a BAĞLANIR — modül-
        # singleton import-zamanında loop-suz yaratırsa, çok-loop'lu ortamda (pytest test-başı-loop)
        # "Event loop is closed" verir. Bu yüzden async semaforlar LOOP-BAŞINA lazy yaratılır
        # (_ensure_async_sems); prod tek-loop'ta bir kez. Sync yol thread-semafor (loop-bağımsız).
        self._async_ollama_sem: asyncio.Semaphore | None = None
        self._async_lowprio_sem: asyncio.Semaphore | None = None
        self._async_sem_loop = None
        # #2 Öncelik: düşük-öncelik en çok N-1 permit kullanır → high-priority'ye HER ZAMAN ≥1 permit
        # kalır (rutin-iş doldursa bile incident-teşhisi beklemez). N=1'de rezerv yok (tek-slot=FIFO).
        _lp = max(1, _OLLAMA_CONCURRENCY - 1)
        self._sync_ollama_sem = threading.Semaphore(_OLLAMA_CONCURRENCY)
        self._sync_lowprio_sem = threading.Semaphore(_lp)

    def _ensure_async_sems(self) -> None:
        """Async semaforları ÇALIŞAN loop'a bağlı tut (loop değişince yeniden yarat). Modül-singleton
        + çok-loop güvenliği. Manuel-set'li testler _async_sem_loop'u set ederse ezilmez."""
        loop = asyncio.get_running_loop()
        if self._async_sem_loop is not loop or self._async_ollama_sem is None:
            self._async_ollama_sem = asyncio.Semaphore(_OLLAMA_CONCURRENCY)
            self._async_lowprio_sem = asyncio.Semaphore(max(1, _OLLAMA_CONCURRENCY - 1))
            self._async_sem_loop = loop

    def route(self, task: str) -> tuple[str, str]:
        """task → (backend, model). Env ``LLM_ROUTE_<TASK>`` öncelikli, sonra tablo, sonra default."""
        env = read_env_var(f"LLM_ROUTE_{task.upper().replace('-', '_')}")
        if env and ":" in env:
            backend, _, model = env.partition(":")
            if backend.strip() and model.strip():
                return backend.strip(), model.strip()
        return _TASK_ROUTES.get(task, _TASK_ROUTES["default"])

    @staticmethod
    def _resolve_priority(task: str, priority: str) -> str:
        return "high" if priority == "high" or task in _HIGH_PRIORITY_TASKS else "normal"

    @asynccontextmanager
    async def _ollama_gate_async(self, priority: str):
        """Yerel-CPU vanası + öncelik. high → tam N permit; normal → önce low-prio(N-1) sonra ana."""
        self._ensure_async_sems()  # loop-başına lazy (Event-loop-closed footgun fix)
        if priority == "high":
            async with self._async_ollama_sem:
                yield
        else:
            async with self._async_lowprio_sem, self._async_ollama_sem:
                yield

    @contextmanager
    def _ollama_gate_sync(self, priority: str):
        if priority == "high":
            with self._sync_ollama_sem:
                yield
        else:
            with self._sync_lowprio_sem, self._sync_ollama_sem:
                yield

    @staticmethod
    def _payload(prompt: str, model: str, system: str | None, temperature: float, num_predict: int | None, fmt: dict | None = None) -> dict:
        options: dict = {"temperature": temperature}
        if num_predict:
            options["num_predict"] = num_predict
        payload: dict = {"model": model, "prompt": prompt, "stream": False, "options": options}
        if system:
            payload["system"] = system
        if fmt:
            payload["format"] = fmt  # Ollama structured-output (JSON-schema) → garantili geçerli JSON
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
        priority: str = "normal",
        fmt: dict | None = None,
    ) -> str:
        """Async üretim. Hata → "" (fail-silent) veya raise_on_error ise istisna. model routing'i ezer.
        priority='high' (veya task∈_HIGH_PRIORITY_TASKS) → rutin-işi geçer (rezerv-permit).
        fmt (Ollama JSON-schema) verilirse → garantili-geçerli structured output (claude'da yok-sayılır)."""
        backend, route_model = self.route(task)
        model = model or route_model
        prio = self._resolve_priority(task, priority)
        import time as _t

        _t0 = _t.monotonic()
        _ok = False
        try:
            if backend == "claude":
                out = await self._claude(system or "", prompt, model)
            else:
                out = await self._ollama_async(prompt, model, system, temperature, num_predict, timeout, prio, fmt)
            _ok = True
            return out
        except Exception:
            if raise_on_error:
                raise
            logger.debug("LLMCore generate failed (task=%s)", task, exc_info=True)
            return ""
        finally:
            _record_llm_call(task, backend, model, (_t.monotonic() - _t0) * 1000, _ok)

    async def _ollama_async(self, prompt, model, system, temperature, num_predict, timeout, priority="normal", fmt=None) -> str:
        async with self._ollama_gate_async(priority):  # yerel-CPU vanası + öncelik
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(
                    f"{self._ollama}/api/generate",
                    json=self._payload(prompt, model, system, temperature, num_predict, fmt),
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
        priority: str = "normal",
        fmt: dict | None = None,
    ) -> str:
        """Sync üretim (requests) — FastAPI threadpool çağrıcıları için. Aynı routing/raise/öncelik.
        fmt (Ollama JSON-schema) → garantili structured output (claude'da yok-sayılır)."""
        backend, route_model = self.route(task)
        model = model or route_model
        prio = self._resolve_priority(task, priority)
        import time as _t

        _t0 = _t.monotonic()
        _ok = False
        try:
            if backend == "claude":
                from app.api.research import _anthropic_generate

                out = (_anthropic_generate(system or "", prompt, model) or "").strip()
            else:
                out = self._ollama_sync(prompt, model, system, temperature, num_predict, timeout, prio, fmt)
            _ok = True
            return out
        except Exception:
            if raise_on_error:
                raise
            logger.debug("LLMCore generate_sync failed (task=%s)", task, exc_info=True)
            return ""
        finally:
            _record_llm_call(task, backend, model, (_t.monotonic() - _t0) * 1000, _ok)

    def _ollama_sync(self, prompt, model, system, temperature, num_predict, timeout, priority="normal", fmt=None) -> str:
        import requests

        with self._ollama_gate_sync(priority):  # yerel-CPU vanası (thread) + öncelik
            r = requests.post(
                f"{self._ollama}/api/generate",
                json=self._payload(prompt, model, system, temperature, num_predict, fmt),
                timeout=timeout,
            )
            r.raise_for_status()
            return (r.json().get("response") or "").strip()

    def complete_sync(
        self,
        prompt: str,
        *,
        task: str = "default",
        model: str | None = None,
        options: dict | None = None,
        timeout: int = 300,
        raise_on_error: bool = False,
    ) -> dict:
        """HAM yanıt dict'i (response + eval_count/eval_duration metrikleri) — metrik+özel-options
        isteyen sync çağrıcılar için (rag /ask: num_ctx, tps). generate_sync string döndürür; bu dict.
        Routing/env-override yine geçerli. Hata → {} (veya raise_on_error)."""
        import requests

        backend, route_model = self.route(task)
        model = model or route_model
        prio = self._resolve_priority(task, "normal")
        import time as _t

        _t0 = _t.monotonic()
        _ok = False
        _tokens = None
        try:
            payload = {"model": model, "prompt": prompt, "stream": False, "options": options or {}}
            with self._ollama_gate_sync(prio):  # yerel-CPU vanası (thread) + öncelik
                r = requests.post(f"{self._ollama}/api/generate", json=payload, timeout=timeout)
                r.raise_for_status()
                resp = r.json() or {}
            _ok = True
            _tokens = resp.get("eval_count")
            return resp
        except Exception:
            if raise_on_error:
                raise
            logger.debug("LLMCore complete_sync failed (task=%s)", task, exc_info=True)
            return {}
        finally:
            _record_llm_call(task, backend, model, (_t.monotonic() - _t0) * 1000, _ok, _tokens)

    async def chat(
        self,
        messages: list[dict],
        *,
        task: str = "default",
        model: str | None = None,
        timeout: int = 120,
        raise_on_error: bool = False,
        priority: str = "normal",
        fmt: dict | None = None,
    ) -> str:
        """Mesaj-listesi (/api/chat) → asistan içeriği (str). Yerel ollama chat (generate'in
        sohbet-eşi: rol'lü messages, /no_think çağrıcıda). Fail-silent "" veya raise_on_error.
        fmt (Ollama JSON-schema) verilirse → garantili-geçerli structured output (generate ile simetrik).
        NOT: claude-chat YOK (3 çağrıcı da yerel-model RAG/dispatch/inference); gerekirse eklenir."""
        backend, route_model = self.route(task)
        model = model or route_model
        prio = self._resolve_priority(task, priority)
        import time as _t

        _t0 = _t.monotonic()
        _ok = False
        _tokens = None
        try:
            async with self._ollama_gate_async(prio):  # yerel-CPU vanası + öncelik
                payload: dict = {"model": model, "messages": messages, "stream": False}
                if fmt:
                    payload["format"] = fmt  # Ollama structured-output (JSON-schema)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    r = await client.post(f"{self._ollama}/api/chat", json=payload)
                r.raise_for_status()
                resp = r.json() or {}
            _ok = True
            _tokens = resp.get("eval_count")
            return (resp.get("message") or {}).get("content", "").strip()
        except Exception:
            if raise_on_error:
                raise
            logger.debug("LLMCore chat failed (task=%s)", task, exc_info=True)
            return ""
        finally:
            _record_llm_call(task, backend, model, (_t.monotonic() - _t0) * 1000, _ok, _tokens)

    async def _claude(self, system: str, user: str, model: str) -> str:
        """Max-abonelik CLI yolu reuse (research._anthropic_generate, sync → to_thread)."""
        from app.api.research import _anthropic_generate

        return (await asyncio.to_thread(_anthropic_generate, system, user, model) or "").strip()


# Modül-singleton — ajanlar import edip paylaşır (tek model/maliyet kontrol noktası).
llm_core = LLMCore()
