"""Tests for RAG API — index, query, stats."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.anyio
async def test_rag_stats(client, auth_headers):
    mock_stats = {"collection": "documents", "document_count": 42}
    with patch("app.api.rag._engine.stats", new_callable=AsyncMock, return_value=mock_stats):
        resp = await client.get("/api/v1/rag/stats", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["document_count"] == 42


@pytest.mark.anyio
async def test_rag_index_text(client, auth_headers):
    mock_result = {"indexed": 3, "source": "test"}
    with patch("app.api.rag._engine.index_text", new_callable=AsyncMock, return_value=mock_result):
        resp = await client.post(
            "/api/v1/rag/index/text",
            json={
                "text": "Test document content for indexing",
                "source": "test",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["indexed"] == 3


@pytest.mark.anyio
async def test_rag_index_text_requires_write(client, read_headers):
    resp = await client.post(
        "/api/v1/rag/index/text",
        json={
            "text": "Test",
            "source": "test",
        },
        headers=read_headers,
    )
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_rag_query_with_generation(client, auth_headers):
    mock_result = {
        "question": "What is Linux?",
        "answer": "Linux is an operating system.",
        "sources": [{"text": "Linux is...", "source": "doc.md", "distance": 0.15}],
    }
    with patch("app.api.rag._engine.query", new_callable=AsyncMock, return_value=mock_result):
        resp = await client.post(
            "/api/v1/rag/query",
            json={
                "question": "What is Linux?",
                "n_results": 3,
                "generate": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "answer" in data
        assert len(data["sources"]) == 1


@pytest.mark.anyio
async def test_rag_query_without_generation(client, auth_headers):
    mock_result = {
        "question": "What is Linux?",
        "results": [{"text": "Linux is...", "source": "doc.md", "distance": 0.15}],
    }
    with patch("app.api.rag._engine.query", new_callable=AsyncMock, return_value=mock_result):
        resp = await client.post(
            "/api/v1/rag/query",
            json={
                "question": "What is Linux?",
                "generate": False,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert "results" in resp.json()


@pytest.mark.anyio
async def test_rag_index_file(client, auth_headers):
    mock_result = {"indexed": 5, "source": "readme.md"}
    with patch("app.api.rag._engine.index_file", new_callable=AsyncMock, return_value=mock_result):
        resp = await client.post("/api/v1/rag/index/file", json={"path": "/tmp/readme.md"}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["indexed"] == 5


@pytest.mark.anyio
async def test_rag_index_directory(client, auth_headers):
    mock_result = {"files": 10, "chunks": 50, "directory": "/docs", "pattern": "*.md"}
    with patch("app.api.rag._engine.index_directory", new_callable=AsyncMock, return_value=mock_result):
        resp = await client.post(
            "/api/v1/rag/index/directory",
            json={
                "directory": "/docs",
                "pattern": "*.md",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["files"] == 10


@pytest.mark.anyio
async def test_rag_delete_collection(client, auth_headers):
    mock_result = {"deleted": "documents", "status": 200}
    with patch("app.api.rag._engine.delete_collection", new_callable=AsyncMock, return_value=mock_result):
        resp = await client.delete("/api/v1/rag/collection", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "documents"
