"""LLMCore testleri — task-bazlı routing + backend dispatch + fail-silent (AIOS uyarlaması)."""

import app.core.agents.llmcore as lc
from app.core.agents import llm_core
from app.core.agents.llmcore import LLMCore


def test_route_table_known_tasks():
    core = LLMCore()
    assert core.route("code-review") == ("ollama", "qwen2.5-coder:7b")
    assert core.route("diagnosis") == ("ollama", "qwen2.5:3b")
    assert core.route("escalate")[0] == "claude"
    assert core.route("synthesis") == ("claude", "claude-sonnet-4-6")


def test_route_unknown_falls_back_to_default():
    assert LLMCore().route("bilinmeyen-task") == ("ollama", "qwen2.5:3b")


def test_route_env_override(monkeypatch):
    """LLM_ROUTE_<TASK> env tabloyu ezer (backend:model)."""
    monkeypatch.setattr(lc, "read_env_var", lambda k: "ollama:qwen2.5:7b" if k == "LLM_ROUTE_DIAGNOSIS" else None)
    assert LLMCore().route("diagnosis") == ("ollama", "qwen2.5:7b")


def test_route_env_override_malformed_ignored(monkeypatch):
    """Bozuk env (':' yok / boş taraf) yok sayılır → tabloya düşer."""
    monkeypatch.setattr(lc, "read_env_var", lambda k: "garbage-no-colon" if k.startswith("LLM_ROUTE") else None)
    assert LLMCore().route("code-review") == ("ollama", "qwen2.5-coder:7b")


async def test_generate_ollama_backend(monkeypatch):
    """ollama-route → _ollama_generate çağrılır, ham yanıt döner."""

    async def fake_ollama(self, prompt, model, system, temperature, num_predict, timeout):
        return f"OLLAMA:{model}"

    monkeypatch.setattr(LLMCore, "_ollama_async", fake_ollama)
    assert await LLMCore().generate("p", task="diagnosis") == "OLLAMA:qwen2.5:3b"


async def test_generate_model_override_beats_route(monkeypatch):
    async def fake_ollama(self, prompt, model, *a):
        return model

    monkeypatch.setattr(LLMCore, "_ollama_async", fake_ollama)
    assert await LLMCore().generate("p", task="diagnosis", model="custom:1b") == "custom:1b"


async def test_generate_claude_backend_delegates(monkeypatch):
    """claude-route → _claude (research._anthropic_generate reuse) çağrılır."""

    async def fake_claude(self, system, user, model):
        return f"CLAUDE:{model}"

    monkeypatch.setattr(LLMCore, "_claude", fake_claude)
    assert await LLMCore().generate("p", task="synthesis") == "CLAUDE:claude-sonnet-4-6"


async def test_generate_fail_silent(monkeypatch):
    """Backend hata atarsa generate '' döner (ajan döngüsü asla bozulmaz)."""

    async def boom(self, *a, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(LLMCore, "_ollama_async", boom)
    assert await LLMCore().generate("p", task="diagnosis") == ""


async def test_generate_raise_on_error_propagates(monkeypatch):
    """raise_on_error=True → istisna yükselir (API endpoint 502/503'e çevirir)."""

    async def boom(self, *a, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(LLMCore, "_ollama_async", boom)
    import pytest

    with pytest.raises(RuntimeError):
        await LLMCore().generate("p", task="diagnosis", raise_on_error=True)


def test_generate_sync_ollama(monkeypatch):
    """generate_sync ollama-route → _ollama_sync, ham yanıt döner (research/classifier yolu)."""

    def fake_sync(self, prompt, model, *a):
        return f"SYNC:{model}"

    monkeypatch.setattr(LLMCore, "_ollama_sync", fake_sync)
    assert LLMCore().generate_sync("p", task="research") == "SYNC:qwen2.5:3b"


def test_generate_sync_fail_silent_and_raise(monkeypatch):
    """generate_sync: default fail-silent '', raise_on_error=True → propagate."""

    def boom(self, *a, **k):
        raise RuntimeError("requests fail")

    monkeypatch.setattr(LLMCore, "_ollama_sync", boom)
    assert LLMCore().generate_sync("p", task="research") == ""
    import pytest

    with pytest.raises(RuntimeError):
        LLMCore().generate_sync("p", task="research", raise_on_error=True)


def test_generate_sync_claude_backend(monkeypatch):
    """generate_sync claude-route → research._anthropic_generate (sync) reuse."""
    import app.api.research as research

    monkeypatch.setattr(research, "_anthropic_generate", lambda system, user, model: f"C:{model}")
    assert LLMCore().generate_sync("p", task="synthesis") == "C:claude-sonnet-4-6"


def test_complete_sync_returns_raw_dict(monkeypatch):
    """complete_sync ham ollama dict'i döndürür (response + eval metrikleri) — rag /ask yolu."""
    import app.core.agents.llmcore as lcmod

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "cevap", "eval_count": 5, "eval_duration": 1_000_000_000}

    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["model"] = (json or {}).get("model")
        captured["options"] = (json or {}).get("options")
        return FakeResp()

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    out = lcmod.LLMCore().complete_sync("p", task="rag", model="qwen2.5:7b", options={"num_ctx": 8192})
    assert out["response"] == "cevap"
    assert out["eval_count"] == 5
    assert captured["model"] == "qwen2.5:7b"
    assert captured["options"] == {"num_ctx": 8192}


def test_complete_sync_fail_silent_and_raise(monkeypatch):
    import requests

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(requests, "post", boom)
    assert LLMCore().complete_sync("p", task="rag") == {}
    import pytest

    with pytest.raises(RuntimeError):
        LLMCore().complete_sync("p", task="rag", raise_on_error=True)


async def test_chat_extracts_message_content(monkeypatch):
    """chat() → /api/chat yanıtından message.content çıkarır (rol'lü messages)."""

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "  merhaba  "}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(lc.httpx, "AsyncClient", FakeClient)
    out = await LLMCore().chat([{"role": "user", "content": "selam"}], model="qwen2.5:7b")
    assert out == "merhaba"


async def test_chat_fail_silent_and_raise(monkeypatch):
    """chat(): default fail-silent '', raise_on_error=True → propagate."""

    class BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise RuntimeError("chat down")

    monkeypatch.setattr(lc.httpx, "AsyncClient", BoomClient)
    assert await LLMCore().chat([{"role": "user", "content": "x"}]) == ""
    import pytest

    with pytest.raises(RuntimeError):
        await LLMCore().chat([{"role": "user", "content": "x"}], raise_on_error=True)


def test_singleton_exported():
    assert isinstance(llm_core, LLMCore)
