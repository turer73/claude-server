"""Tests for /api/v1/llm/status — dashboard LLM tab backend.

Read-only aggregate: Ollama models, GPU (Vulkan), Anthropic configured,
RAG usage stats. require_admin auth (X-API-Key veya JWT).
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.anyio
async def test_route_registered(client):
    resp = await client.get("/openapi.json")
    paths = resp.json()["paths"]
    assert "/api/v1/llm/status" in paths


@pytest.mark.anyio
async def test_unauthenticated_rejected(client):
    """require_admin -> X-API-Key veya Bearer JWT olmadan 401."""
    resp = await client.get("/api/v1/llm/status")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_happy_path_with_mocked_deps(client, auth_headers):
    """Tum 4 alt-fonksiyonu mock'la, JSON shape kontrolu."""
    fake_ollama_models = {
        "models": [
            {
                "name": "qwen2.5:7b",
                "size": 4466 * 1024 * 1024,
                "details": {"family": "qwen2", "parameter_size": "7.6B", "quantization_level": "Q4_K_M"},
                "modified_at": "2026-05-12T23:26:22Z",
            }
        ]
    }
    fake_loaded = {"models": []}

    def fake_get(url, **_):
        m = MagicMock(ok=True)
        if "/api/tags" in url:
            m.json.return_value = fake_ollama_models
        elif "/api/ps" in url:
            m.json.return_value = fake_loaded
        return m

    with (
        patch("app.api.llm.requests.get", side_effect=fake_get),
        patch("app.api.llm._gpu_status", return_value={"vulkan_enabled": False, "backend": "cpu", "busy_percent": 0}),
        patch("app.api.llm._usage_stats", return_value={"ok": True, "total": 0}),
    ):
        resp = await client.get("/api/v1/llm/status", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ollama"]["ok"] is True
    assert len(body["ollama"]["models"]) == 1
    assert body["ollama"]["models"][0]["name"] == "qwen2.5:7b"
    assert body["gpu"]["backend"] == "cpu"
    assert body["anthropic"]["configured"] in (True, False)
    assert "usage_last_24h" in body


@pytest.mark.anyio
async def test_ollama_down_does_not_crash(client, auth_headers):
    """Ollama unreachable -> ollama.ok=false, endpoint hala 200."""
    with (
        patch("app.api.llm.requests.get", side_effect=ConnectionError("ollama offline")),
        patch("app.api.llm._gpu_status", return_value={"vulkan_enabled": False, "backend": "cpu"}),
        patch("app.api.llm._usage_stats", return_value={"ok": False, "total": 0}),
    ):
        resp = await client.get("/api/v1/llm/status", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ollama"]["ok"] is False
    assert "error" in body["ollama"]


def test_usage_stats_reads_metrics_db(monkeypatch, tmp_path):
    """P1-a Faz-2b: _usage_stats get_conn(busy_timeout_ms=2000) — gercek tmp-DB'den okur.
    Row-factory ile fetchone-unpack + dict(fetchall) paritesi kilitlenir."""
    import sqlite3
    import time as _time

    from app.api.llm import _usage_stats

    db = tmp_path / "rag_metrics.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE rag_queries (ts INT, endpoint TEXT, duration_ms REAL, hit_count INT, top_score REAL)")
    now = int(_time.time())
    con.execute("INSERT INTO rag_queries VALUES (?, 'search', 120.0, 5, 0.9)", (now,))
    con.execute("INSERT INTO rag_queries VALUES (?, 'ask', 80.0, 3, 0.7)", (now,))
    con.commit()
    con.close()
    monkeypatch.setattr("app.api.rag.METRICS_DB", str(db))
    out = _usage_stats(hours=1)
    assert out["ok"] is True
    assert out["total"] == 2
    assert out["avg_duration_ms"] == 100.0
    assert out["by_endpoint"] == {"search": 1, "ask": 1}  # dict(fetchall) Row-parite


def test_usage_stats_db_error_returns_error_field(monkeypatch, tmp_path):
    """Tablo yok -> ok=False + error alani (fail-safe yol)."""
    from app.api.llm import _usage_stats

    monkeypatch.setattr("app.api.rag.METRICS_DB", str(tmp_path / "bos.db"))
    out = _usage_stats(hours=1)
    assert out["ok"] is False
    assert "error" in out


def test_usage_stats_closes_conn_on_query_error(monkeypatch, tmp_path):
    """#1239 repro — sorgu exception'ında (tablo-yok) bağlantı KAPANMALI.
    Base'de conn.close() yalnız happy-path'te → burada kapanmaz → FAIL."""
    from app.api.llm import _usage_stats
    from app.db.data_layer import get_conn

    closed = []

    class _Tracking:
        def __init__(self, real):
            self._real = real

        def cursor(self):
            return self._real.cursor()

        def close(self):
            closed.append(True)
            self._real.close()

    real = get_conn(str(tmp_path / "empty.db"))  # rag_queries tablosu YOK → execute raise
    monkeypatch.setattr("app.api.llm.get_conn", lambda *a, **k: _Tracking(real))
    out = _usage_stats(hours=1)
    assert out["ok"] is False
    assert "error" in out
    assert closed  # exception-yolunda da close çağrıldı (leak yok)
