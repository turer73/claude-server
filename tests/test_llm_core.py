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

    monkeypatch.setattr(LLMCore, "_ollama_generate", fake_ollama)
    assert await LLMCore().generate("p", task="diagnosis") == "OLLAMA:qwen2.5:3b"


async def test_generate_model_override_beats_route(monkeypatch):
    async def fake_ollama(self, prompt, model, *a):
        return model

    monkeypatch.setattr(LLMCore, "_ollama_generate", fake_ollama)
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

    monkeypatch.setattr(LLMCore, "_ollama_generate", boom)
    assert await LLMCore().generate("p", task="diagnosis") == ""


def test_singleton_exported():
    assert isinstance(llm_core, LLMCore)
