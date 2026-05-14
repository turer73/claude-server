"""Tests for the Research API.

Endpoints:
  POST /api/v1/research/ask
  GET  /api/v1/research/health

Auth: verify_key (X-Memory-Key). Tests blank MEMORY_API_KEY via monkeypatch
so the dependency short-circuits.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _bypass_memory_auth(monkeypatch):
    monkeypatch.setattr("app.api.memory.MEMORY_API_KEY", "")


@pytest.mark.anyio
async def test_routes_registered(client):
    resp = await client.get("/openapi.json")
    paths = resp.json()["paths"]
    assert "/api/v1/research/ask" in paths
    assert "/api/v1/research/health" in paths


@pytest.mark.anyio
async def test_health_all_deps(client):
    """Mock 3 dependency (ollama + qdrant + memory db)."""
    ol = MagicMock(ok=True)
    ol.json.return_value = {"version": "0.23.2"}
    qd = MagicMock(ok=True)
    qd.json.return_value = {"result": {"points_count": 9945}}

    def fake_get(url, **_):
        return ol if "/api/version" in url else qd

    # memory_db: gercek dosyaya yazmak istemiyoruz; sqlite3.connect'i mock'la
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = (42,)
    with patch("app.api.research.requests.get", side_effect=fake_get), patch("app.api.research.sqlite3.connect", return_value=fake_conn):
        resp = await client.get("/api/v1/research/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ollama"]["ok"] is True
    assert body["qdrant"]["ok"] is True
    assert body["memory_db"]["ok"] is True


@pytest.mark.anyio
async def test_ask_returns_empty_answer_when_no_chunks(client, monkeypatch):
    """Tum kaynaklar bos donerse 'Yetersiz kaynak' don, LLM cagirma."""
    monkeypatch.setattr("app.api.research._qdrant_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research._discovery_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research._memory_chunks", lambda *a, **kw: [])
    # Bu durumda Ollama cagrilmamali — patch et ki cagrilirsa test patlasin
    with patch("app.api.research._ollama_generate") as gen:
        resp = await client.post(
            "/api/v1/research/ask",
            json={"q": "test sorgu", "include_rag": True},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "Yetersiz kaynak" in body["answer"]
    assert body["source_count"] == 0
    gen.assert_not_called()


@pytest.mark.anyio
async def test_ask_full_pipeline_with_citation_validation(client, monkeypatch):
    """Tek discovery dondurup synthesizer'in mock'lanmis cevabini validate et."""
    chunk = {
        "type": "discovery",
        "id": "323",
        "project": "petvet.panola.app",
        "subtype": "bug",
        "title": "self-pentest: eksik security header",
        "status": "active",
        "text": "Eksik header: HSTS CSP",
    }
    monkeypatch.setattr("app.api.research._qdrant_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research._discovery_chunks", lambda *a, **kw: [chunk])
    monkeypatch.setattr("app.api.research._memory_chunks", lambda *a, **kw: [])
    fake_answer = "petvet.panola.app'da HSTS ve CSP header'lari eksik [discovery:323]."
    fake_hallu = "Ayrica [discovery:999] obsolete olarak isaretli."
    with patch("app.api.research._ollama_generate", return_value=f"{fake_answer} {fake_hallu}"):
        resp = await client.post(
            "/api/v1/research/ask",
            json={"q": "petvet security headers", "include_rag": False},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_count"] == 1
    # 323 gercek -> used; 999 sahte -> hallucinated
    assert "discovery:323" in body["citations"]["used"]
    assert "discovery:999" in body["citations"]["hallucinated"]


@pytest.mark.anyio
async def test_ask_caps_chunks_at_max(client, monkeypatch):
    """max_chunks asilirsa kesilir — qwen 7B prompt boyu hassasiyeti."""
    big_list = [{"type": "discovery", "id": str(i), "title": f"d{i}", "text": "x"} for i in range(20)]
    monkeypatch.setattr("app.api.research._qdrant_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research._discovery_chunks", lambda *a, **kw: big_list)
    monkeypatch.setattr("app.api.research._memory_chunks", lambda *a, **kw: [])
    with patch("app.api.research._ollama_generate", return_value="ok"):
        resp = await client.post(
            "/api/v1/research/ask",
            json={"q": "test", "max_chunks": 5, "include_rag": False},
        )
    assert resp.json()["source_count"] == 5


@pytest.mark.anyio
async def test_engine_claude_uses_anthropic_when_requested(client, monkeypatch):
    """engine='claude' -> _anthropic_generate cagrilir, _ollama_generate degil."""
    chunk = {"type": "discovery", "id": "5", "title": "x", "text": "y"}
    monkeypatch.setattr("app.api.research._qdrant_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research._discovery_chunks", lambda *a, **kw: [chunk])
    monkeypatch.setattr("app.api.research._memory_chunks", lambda *a, **kw: [])
    # ANTHROPIC_API_KEY'i mock'la (modul yuklemesinde okunmus olabilir bos)
    monkeypatch.setattr("app.api.research.ANTHROPIC_API_KEY", "sk-test-fake")
    with (
        patch("app.api.research._anthropic_generate", return_value="cevap [discovery:5]") as cl,
        patch("app.api.research._ollama_generate") as ol,
    ):
        resp = await client.post(
            "/api/v1/research/ask",
            json={"q": "test", "engine": "claude", "include_rag": False},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["engine"] == "claude"
    cl.assert_called_once()
    ol.assert_not_called()
    assert "discovery:5" in body["citations"]["used"]


@pytest.mark.anyio
async def test_engine_auto_picks_claude_when_chunks_high(client, monkeypatch):
    """8+ kaynak ve ANTHROPIC_API_KEY varsa auto -> claude."""
    many = [{"type": "discovery", "id": str(i), "title": "x", "text": "y"} for i in range(10)]
    monkeypatch.setattr("app.api.research._qdrant_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research._discovery_chunks", lambda *a, **kw: many)
    monkeypatch.setattr("app.api.research._memory_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research.ANTHROPIC_API_KEY", "sk-test")
    with patch("app.api.research._anthropic_generate", return_value="ok") as cl, patch("app.api.research._ollama_generate") as ol:
        resp = await client.post(
            "/api/v1/research/ask",
            json={"q": "test", "engine": "auto", "include_rag": False, "max_chunks": 10},
        )
    assert resp.json()["engine"] == "claude"
    cl.assert_called_once()
    ol.assert_not_called()


@pytest.mark.anyio
async def test_engine_auto_falls_back_to_local_without_key(client, monkeypatch):
    """ANTHROPIC_API_KEY yoksa auto -> local."""
    many = [{"type": "discovery", "id": str(i), "title": "x", "text": "y"} for i in range(10)]
    monkeypatch.setattr("app.api.research._discovery_chunks", lambda *a, **kw: many)
    monkeypatch.setattr("app.api.research._memory_chunks", lambda *a, **kw: [])
    monkeypatch.setattr("app.api.research.ANTHROPIC_API_KEY", "")  # key yok
    with patch("app.api.research._ollama_generate", return_value="ok") as ol, patch("app.api.research._anthropic_generate") as cl:
        resp = await client.post(
            "/api/v1/research/ask",
            json={"q": "test", "engine": "auto", "include_rag": False, "max_chunks": 10},
        )
    assert resp.json()["engine"] == "local"
    ol.assert_called_once()
    cl.assert_not_called()


@pytest.mark.anyio
async def test_engine_invalid_rejected(client, monkeypatch):
    chunk = {"type": "discovery", "id": "1", "title": "x", "text": "y"}
    monkeypatch.setattr("app.api.research._discovery_chunks", lambda *a, **kw: [chunk])
    monkeypatch.setattr("app.api.research._memory_chunks", lambda *a, **kw: [])
    resp = await client.post(
        "/api/v1/research/ask",
        json={"q": "test", "engine": "gpt5", "include_rag": False},
    )
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_fts_query_hyphens_normalised():
    """'bilge-arena' -> 'bilge OR arena' (FTS5 column-prefix patlamasini engeller)."""
    from app.api.research import _fts_q

    out = _fts_q("bilge-arena security header")
    # Tire kelimeleri ayirir, kucuk kelimeler (<3 char) atilir
    assert "bilge" in out
    assert "arena" in out
    assert "OR" in out
    # FTS5'in column-prefix sentaksini tetikleyecek karakterler yok
    for forbidden in ('"', "*", ":"):
        assert forbidden not in out
