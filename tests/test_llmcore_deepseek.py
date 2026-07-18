"""DeepSeek backend testleri (topic-5 K2, P-B paketi) — routing, payload, fail-silent, fallback."""

from unittest.mock import AsyncMock, patch

import pytest

from app.core.agents import llmcore as llmcore_mod
from app.core.agents.llmcore import LLMCore, llm_core


def test_research_hi_routes_to_deepseek():
    backend, model = llm_core.route("research-hi")
    assert backend == "deepseek"
    assert model == "deepseek-v4-flash"  # legacy deepseek-chat DEĞİL (2026-07-24 deprecation)


def test_deepseek_payload_shape():
    p = LLMCore._deepseek_payload("soru", "deepseek-v4-flash", "sistem", 0.3, 256)
    assert p["model"] == "deepseek-v4-flash"
    assert p["messages"][0] == {"role": "system", "content": "sistem"}
    assert p["messages"][1] == {"role": "user", "content": "soru"}
    assert p["max_tokens"] == 256
    assert p["stream"] is False


def test_deepseek_headers_require_key(monkeypatch):
    monkeypatch.setattr(llmcore_mod, "read_env_var", lambda name: None)
    with pytest.raises(RuntimeError):
        LLMCore._deepseek_headers()


def test_generate_sync_deepseek_missing_key_fail_silent(monkeypatch):
    # Key yokken istisna yükselmemeli (fail-silent "") — çağrıcı lokal-fallback'ine düşer.
    monkeypatch.setattr(llmcore_mod, "read_env_var", lambda name: None)
    out = llm_core.generate_sync("soru", task="research-hi")
    assert out == ""


class _FakeResp:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def test_generate_sync_deepseek_backend(monkeypatch):
    def fake_env(name):
        return "sk-test" if name == "DEEPSEEK_API_KEY" else None

    monkeypatch.setattr(llmcore_mod, "read_env_var", fake_env)
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["auth"] = headers.get("Authorization", "")
        captured["json"] = json
        return _FakeResp("merhaba<|im_end|>")

    monkeypatch.setattr("requests.post", fake_post)
    out = llm_core.generate_sync("soru", task="research-hi", system="sistem")
    assert out == "merhaba"  # leaked-token temizliği deepseek'te de uygulanır
    assert captured["url"].startswith("https://api.deepseek.com")
    assert captured["auth"] == "Bearer sk-test"
    assert captured["json"]["model"] == "deepseek-v4-flash"


async def test_generate_async_routes_deepseek_branch():
    with patch.object(LLMCore, "_deepseek_async", new_callable=AsyncMock, return_value="ok") as ds:
        out = await llm_core.generate("soru", task="research-hi")
    assert out == "ok"
    assert ds.call_count == 1


def test_hi_generate_prefers_deepseek_then_falls_back(monkeypatch):
    # research._hi_generate: Layer-2 boş dönerse gemma3 lokal-fallback (davranış-koruma).
    from app.api import research

    monkeypatch.setattr(llm_core, "generate_sync", lambda *a, **k: "")
    monkeypatch.setattr(research, "_ollama_generate", lambda prompt, model=None: "gemma-yanit")
    assert research._hi_generate("soru") == "gemma-yanit"

    monkeypatch.setattr(llm_core, "generate_sync", lambda *a, **k: "ds-yanit")

    def _boom(prompt, model=None):
        raise AssertionError("deepseek başarılıyken gemma3 çağrılmamalı")

    monkeypatch.setattr(research, "_ollama_generate", _boom)
    assert research._hi_generate("soru") == "ds-yanit"
