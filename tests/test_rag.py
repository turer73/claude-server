"""Tests for the RAG API (Qdrant + Ollama bge-m3 + qwen2.5 surface).

The old RAGEngine module attr surface was replaced by direct sync requests
to Qdrant/Ollama in commit 0913c15. Routes covered:
  GET  /api/v1/rag/health
  POST /api/v1/rag/search
  POST /api/v1/rag/ask
  GET  /api/v1/rag/projects
  GET  /api/v1/rag/metrics

verify_key reads MEMORY_API_KEY from .env at module load; tests blank it
via monkeypatch so request-level auth is bypassed in isolation.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _set_memory_auth(monkeypatch):
    """MEMORY_API_KEY'i test-key'e set et (fail-closed güvenlik fix; client
    X-Memory-Key gönderir). Eski 'blank=short-circuit' fail-open'ı test ediyordu."""
    from tests.conftest import TEST_MEMORY_KEY

    monkeypatch.setattr("app.api.memory.MEMORY_API_KEY", TEST_MEMORY_KEY)


@pytest.mark.anyio
async def test_rag_routes_registered(client):
    """All 5 RAG routes must be wired into the OpenAPI surface."""
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/api/v1/rag/health" in paths
    assert "/api/v1/rag/search" in paths
    assert "/api/v1/rag/ask" in paths
    assert "/api/v1/rag/projects" in paths
    assert "/api/v1/rag/metrics" in paths


@pytest.mark.anyio
async def test_rag_health_returns_503_when_qdrant_down(client):
    """If Qdrant is unreachable, /health surfaces 503 — not 500."""
    with patch("app.api.rag.requests.get") as m_get:
        m_get.side_effect = ConnectionError("qdrant offline")
        resp = await client.get("/api/v1/rag/health")
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_rag_health_ok_path(client):
    """Qdrant + Ollama both respond → 200 with documented shape."""
    q_resp = MagicMock(ok=True)
    q_resp.json.return_value = {"result": {"points_count": 42}}
    o_resp = MagicMock(ok=True)
    o_resp.json.return_value = {"version": "0.23.2"}

    def fake_get(url, **_):
        if "collections" in url:
            return q_resp
        return o_resp

    with patch("app.api.rag.requests.get", side_effect=fake_get):
        resp = await client.get("/api/v1/rag/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["qdrant"]["points"] == 42
    assert body["ollama"]["version"] == "0.23.2"
    assert body["embed_model"] == "bge-m3"


@pytest.mark.anyio
async def test_rag_projects_503_when_qdrant_unavailable(client):
    """Qdrant non-2xx → 503."""
    bad = MagicMock(ok=False, status_code=500)
    with patch("app.api.rag.requests.post", return_value=bad):
        resp = await client.get("/api/v1/rag/projects")
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_rag_projects_aggregates_by_payload(client):
    """Counts payload.project across scrolled points; missing key → 'general'."""
    points = [
        {"payload": {"project": "panola"}},
        {"payload": {"project": "panola"}},
        {"payload": {"project": "bilge-arena"}},
        {"payload": {}},
    ]
    resp_mock = MagicMock(ok=True)
    resp_mock.json.return_value = {"result": {"points": points}}
    with patch("app.api.rag.requests.post", return_value=resp_mock):
        resp = await client.get("/api/v1/rag/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4
    by_project = {row["project"]: row["count"] for row in body["projects"]}
    assert by_project == {"panola": 2, "bilge-arena": 1, "general": 1}


@pytest.mark.anyio
async def test_rag_metrics_shape(client):
    """/metrics?days=1 returns a JSON object with the documented keys.

    Reads the real METRICS_DB; on a fresh server total may be 0. Asserts
    structural keys only.
    """
    resp = await client.get("/api/v1/rag/metrics?days=1")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("period_days", "total_queries", "avg_duration_ms", "avg_hit_count", "avg_top_score", "by_endpoint", "by_project"):
        assert key in body, f"missing key: {key}"


@pytest.mark.anyio
async def test_rag_metrics_validates_days_range(client):
    """days must be 1..365 per Query(..., ge=1, le=365)."""
    resp = await client.get("/api/v1/rag/metrics?days=0")
    assert resp.status_code == 422
    resp = await client.get("/api/v1/rag/metrics?days=400")
    assert resp.status_code == 422


def _make_ask_mocks():
    """Return (embed, search, scroll, ollama) mocks for /ask tests.
    Qdrant'ın iki endpoint'i farklı şekil döndürür: /points/search → result=LİSTE;
    /points/scroll → result=DICT {points, next_page_offset}. _hybrid_search ikisini de
    çağırır (dense + keyword), o yüzden iki ayrı mock şart."""
    embed_resp = MagicMock(ok=True)
    embed_resp.json.return_value = {"embedding": [0.1] * 10}

    search_resp = MagicMock(ok=True)
    search_resp.json.return_value = {"result": []}

    scroll_resp = MagicMock(ok=True)
    scroll_resp.json.return_value = {"result": {"points": [], "next_page_offset": None}}

    ollama_resp = MagicMock(ok=True)
    ollama_resp.json.return_value = {"response": "test answer", "eval_count": 5, "eval_duration": 1_000_000_000}

    return embed_resp, search_resp, scroll_resp, ollama_resp


@pytest.mark.anyio
async def test_rag_ask_default_model(client):
    """/ask without model param uses qwen2.5:3b by default; response includes model field."""
    embed_resp, search_resp, scroll_resp, ollama_resp = _make_ask_mocks()

    def fake_post(url, **_):
        if "embeddings" in url:
            return embed_resp
        if "scroll" in url:  # scroll'u 6333'ten ÖNCE kontrol et (ikisi de 6333 içerir)
            return scroll_resp
        if "search" in url or "6333" in url:
            return search_resp
        return ollama_resp

    with patch("app.api.rag.requests.post", side_effect=fake_post):
        resp = await client.post("/api/v1/rag/ask", json={"q": "test question"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "qwen2.5:3b"
    assert body["answer"] == "test answer"


@pytest.mark.anyio
async def test_rag_ask_custom_model(client):
    """/ask with model=qwen2.5-coder:7b passes that model to Ollama."""
    embed_resp, search_resp, scroll_resp, ollama_resp = _make_ask_mocks()
    captured = {}

    def fake_post(url, json=None, **_):
        if "embeddings" in url:
            return embed_resp
        if "scroll" in url:  # scroll'u 6333'ten ÖNCE kontrol et
            return scroll_resp
        if "search" in url or "6333" in url:
            return search_resp
        captured["model"] = (json or {}).get("model")
        return ollama_resp

    with patch("app.api.rag.requests.post", side_effect=fake_post):
        resp = await client.post("/api/v1/rag/ask", json={"q": "code review", "model": "qwen2.5-coder:7b"})
    assert resp.status_code == 200
    assert resp.json()["model"] == "qwen2.5-coder:7b"
    assert captured.get("model") == "qwen2.5-coder:7b"


@pytest.mark.anyio
async def test_rag_ask_invalid_model_rejected(client):
    """/ask with an unknown model returns 422 without calling Ollama."""
    resp = await client.post("/api/v1/rag/ask", json={"q": "test", "model": "gpt-4o"})
    assert resp.status_code == 422
