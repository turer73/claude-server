"""classifier.classify_note testleri — LLMCore migrasyonu sonrası label-parse + 502 kontratı."""

import pytest
from fastapi import HTTPException

import app.api.classifier as clf
from app.api.classifier import ClassifyRequest, classify_note


def _stub(monkeypatch, raw):
    async def fake(prompt, **kw):
        return raw

    monkeypatch.setattr(clf.llm_core, "generate", fake)


async def test_label_parsed_from_llm(monkeypatch):
    _stub(monkeypatch, "URGENT")
    out = await classify_note(ClassifyRequest(title="t", content="c"), _=None)
    assert out["label"] == "URGENT"
    assert out["model"] == clf.DEFAULT_MODEL


async def test_empty_response_falls_back_to_discussion(monkeypatch):
    """200-ama-boş yanıt → güvenli default DISCUSSION (kontrat korunur, 502 değil)."""
    _stub(monkeypatch, "")
    out = await classify_note(ClassifyRequest(title="t", content="c"), _=None)
    assert out["label"] == "DISCUSSION"


async def test_upstream_error_becomes_502(monkeypatch):
    """LLMCore raise_on_error=True → istisna → 502 (API kontratı korunur)."""

    async def boom(prompt, **kw):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(clf.llm_core, "generate", boom)
    with pytest.raises(HTTPException) as ei:
        await classify_note(ClassifyRequest(title="t", content="c"), _=None)
    assert ei.value.status_code == 502


async def test_label_parsed_from_json_structured(monkeypatch):
    # klipper #100224 structured-output: {"label": "..."} JSON parse edilir (substring değil).
    _stub(monkeypatch, '{"label": "ACTIONABLE"}')
    out = await classify_note(ClassifyRequest(title="t", content="c"), _=None)
    assert out["label"] == "ACTIONABLE"


async def test_invalid_json_label_falls_back(monkeypatch):
    # JSON ama enum-dışı label → güvenli DISCUSSION (geçersiz kabul edilmez).
    _stub(monkeypatch, '{"label": "GARBAGE"}')
    out = await classify_note(ClassifyRequest(title="t", content="c"), _=None)
    assert out["label"] == "DISCUSSION"


async def test_fmt_schema_passed_to_llm(monkeypatch):
    # generate'e fmt=LABEL_SCHEMA (Ollama enum-kısıtı) geçirilir.
    captured = {}

    async def fake(prompt, **kw):
        captured.update(kw)
        return '{"label": "ACK"}'

    monkeypatch.setattr(clf.llm_core, "generate", fake)
    out = await classify_note(ClassifyRequest(title="t", content="c"), _=None)
    assert out["label"] == "ACK"
    assert captured.get("fmt") == clf.LABEL_SCHEMA
