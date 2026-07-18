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
from typing import Any

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

# 2026-07-12 TR-eval bulgusu: gemma3:12b-it-qat bazı yanıtlarda şablon-özel token'ı ("<end_of_turn>")
# metne sızdırıyor (Ollama-paket template-sorunu, kaynak-modelde değil). Kök-neden yerine defansif
# temizlik — hangi model/backend'den gelirse gelsin görünür-özel-token asla kullanıcıya/RAG'a gitmesin.
_LEAKED_SPECIAL_TOKENS = ("<end_of_turn>", "</end_of_turn>", "<|im_end|>", "<|endoftext|>")


def _strip_leaked_special_tokens(text: str) -> str:
    for tok in _LEAKED_SPECIAL_TOKENS:
        text = text.replace(tok, "")
    return text.strip()


# Task → (backend, model). Mevcut çağrı-yerlerindeki gerçek modeller (spekülasyon değil).
_TASK_ROUTES: dict[str, tuple[str, str]] = {
    # topic-5 karari (2026-07-19): qwen3-coder:30b SILINDI (0-cagri kanitli, .env override'i
    # kalici — code_reviewer._ask_coder fiilen HER ZAMAN claude-haiku'ya gider). Fallback artik
    # qwen2.5:7b (automation-lane resmi-rol, diskte mevcut) — override kaldirilirsa/bozulursa
    # var-olmayan modele carpmasin diye savunma-derinligi, gercek-yol degil.
    "code-review": ("ollama", "qwen2.5:7b"),
    "diagnosis": ("ollama", "qwen2.5:3b"),  # devops_agent._ask_diagnosis
    "research": ("ollama", "qwen2.5:3b"),  # research._ollama_generate
    "reasoning": ("ollama", "qwen3:30b-a3b-instruct-2507-q4_K_M"),  # MoE, thinking-siz (2026-07-12 TR-eval: 20/20 vs qwen2.5:7b 18/20)
    "classify": (
        "ollama",
        "qwen3.5:9b",
    ),  # classifier.classify_note (DEFAULT_MODEL) — 5/5+5/5 mini-eval, 2507'yi classify-yükünden kurtarır (#100701/#100713)
    "rag": ("ollama", "qwen2.5:3b"),  # rag.ask (model çağrıcıdan; complete_sync)
    "escalate": ("claude", "claude-haiku-4-5-20251001"),  # hızlı/ucuz Claude (Max-abonelik)
    "verify": ("claude", "claude-haiku-4-5-20251001"),  # #4 adversarial-verify: qwen-coder kendi FP'sini çürütemiyor → güçlü model
    "synthesis": ("claude", "claude-sonnet-4-6"),  # derin sentez
    "default": ("ollama", "qwen2.5:3b"),
}

# Turgut kararı (#100701/#100713, 2026-07-12): SER8'de 2507(18GB)+gemma3(8GB) aynı-anda RAM'e
# sığmıyor (~26GB usable) ama 2507+qwen3.5:9b(6.6GB)=~24.6GB rahat sığar (classify artık 9b'ye
# taşındı — 2507'nin "ana-iş" yükü reasoning/rag'e daraldı, ikisi de resident kalabilir). gemma3 =
# TR-hi (research, düşük-frekans) → hâlâ on-demand (kısa keep_alive). Diğer modeller Ollama
# varsayılanını (5dk) kullanır (None → payload'a eklenmez).
_MODEL_KEEP_ALIVE: dict[str, str] = {
    "qwen3:30b-a3b-instruct-2507-q4_K_M": "30m",
    "qwen3.5:9b": "20m",  # classify sık-çağrılır (her not) — sıcak-tutmaya değer, 2507'den kısa (küçük/ucuz-reload)
    "gemma3:12b-it-qat": "10s",
}


def _keep_alive_for(model: str) -> str | None:
    return _MODEL_KEEP_ALIVE.get(model)


def _record_llm_call(task: str, backend: str, model: str, latency_ms: float, ok: bool, tokens: int | None = None) -> None:
    """LLM çağrısını rag_metrics.db/llm_calls'a kaydet — merkezi LLM-gözlemlenebilirlik
    (#100224-audit: 9 çağrı-yeri gözlemsizdi; tek choke-point burada). task/backend/model/
    latency/ok/tokens → 'hangi ajan GPU'yu yakıyor', 'ne sıklıkla Claude'a eskale ediyoruz'
    sorularını yanıtlar. FAIL-SAFE: metrik-yazımı ASLA LLM-çağrısını bozmaz."""
    try:
        from app.db.data_layer import get_conn

        db = read_env_var("RAG_METRICS_DB") or "/opt/linux-ai-server/data/rag_metrics.db"
        conn = get_conn(db, busy_timeout_ms=2000)  # P1-a: timeout=2 parite
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
    def _payload(
        prompt: str, model: str, system: str | None, temperature: float, num_predict: int | None, fmt: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": temperature}
        if num_predict:
            options["num_predict"] = num_predict
        # think:false KOSULSUZ (2026-07-12, Turgut karari #100713 zorunlu-onkosul): hibrit-thinking
        # modeller (qwen3:30b-a3b orijinal + qwen3.5/3.6 ailesi, bu-oturumda 2x tekrar-tuzagi)
        # varsayilan-ACIK gelir, basit-promptlarda bile 60-180s+ surer/500-hata verir. Thinking-
        # DESTEKLEMEYEN modeller (qwen2.5:*, qwen3-coder) parametreyi zararsizca yok-sayar (dogrulandi:
        # qwen2.5:3b'de normal-cevap, hata-yok) - allowlist bakim-yuku yerine KOSULSUZ-guvenli-varsayilan.
        payload: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False, "options": options, "think": False}
        if system:
            payload["system"] = system
        if fmt:
            payload["format"] = fmt  # Ollama structured-output (JSON-schema) → garantili geçerli JSON
        keep_alive = _keep_alive_for(model)
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
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
        fmt: dict[str, Any] | None = None,
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
        except Exception as e:
            if raise_on_error:
                raise
            # topic-5 karari (2026-07-19): DEBUG-seviyesi journalctl'de (INFO-default) hic
            # gorunmuyordu — code-review %15-25 fail'in gercek istisna-tipi (TimeoutExpired mi,
            # baska mi) hicbir zaman canli-loglanmamisti, yalniz latency-kumelenmesinden dolayli
            # cikarim yapilabiliyordu. WARNING+tip-adi ile artik gorunur.
            # Codex-P2 (PR#339): exception str'i (ozellikle subprocess.TimeoutExpired) TAM
            # argv'yi (claude CLI '-p' prompt = kod-icerigi) tasir - %s ile e YAZMA, yalniz TIP.
            logger.warning("LLMCore generate failed (task=%s, backend=%s, exc_type=%s)", task, backend, type(e).__name__)
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
            return _strip_leaked_special_tokens((r.json() or {}).get("response") or "")

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
        fmt: dict[str, Any] | None = None,
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
        except Exception as e:
            if raise_on_error:
                raise
            logger.warning("LLMCore generate_sync failed (task=%s, backend=%s, exc_type=%s)", task, backend, type(e).__name__)
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
            return _strip_leaked_special_tokens(r.json().get("response") or "")

    def complete_sync(
        self,
        prompt: str,
        *,
        task: str = "default",
        model: str | None = None,
        options: dict[str, Any] | None = None,
        timeout: int = 300,
        raise_on_error: bool = False,
    ) -> dict[str, Any]:
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
            payload: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False, "options": options or {}, "think": False}
            keep_alive = _keep_alive_for(model)
            if keep_alive is not None:
                payload["keep_alive"] = keep_alive
            with self._ollama_gate_sync(prio):  # yerel-CPU vanası (thread) + öncelik
                r = requests.post(f"{self._ollama}/api/generate", json=payload, timeout=timeout)
                r.raise_for_status()
                resp = r.json() or {}
            _ok = True
            _tokens = resp.get("eval_count")
            if "response" in resp:
                resp["response"] = _strip_leaked_special_tokens(resp["response"] or "")
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
        messages: list[dict[str, Any]],
        *,
        task: str = "default",
        model: str | None = None,
        timeout: int = 120,
        raise_on_error: bool = False,
        priority: str = "normal",
        fmt: dict[str, Any] | None = None,
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
                payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False, "think": False}
                if fmt:
                    payload["format"] = fmt  # Ollama structured-output (JSON-schema)
                keep_alive = _keep_alive_for(model)
                if keep_alive is not None:
                    payload["keep_alive"] = keep_alive
                async with httpx.AsyncClient(timeout=timeout) as client:
                    r = await client.post(f"{self._ollama}/api/chat", json=payload)
                r.raise_for_status()
                resp = r.json() or {}
            _ok = True
            _tokens = resp.get("eval_count")
            return _strip_leaked_special_tokens((resp.get("message") or {}).get("content", ""))
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
