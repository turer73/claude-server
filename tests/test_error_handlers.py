"""Tutarlı hata zarfı testleri (#4) — HTTPException + validation + unhandled
hepsi ServerError ile aynı {error, message, detail} şeklini döner.
"""

from tests.test_memory_api import memory_db  # noqa: F401 — paylaşılan fixture


async def test_http_exception_envelope(client, memory_db):
    """Raw HTTPException (404) → {error, message, detail}; detail KORUNUR (geri-uyum)."""
    r = await client.get("/api/v1/memory/memories/999999")
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "HTTPException"
    assert "detail" in body  # geri-uyum: mevcut detail-okuyanlar bozulmaz
    assert body["message"] == body["detail"]
    assert "not found" in str(body["detail"]).lower()


async def test_validation_error_envelope(client, memory_db):
    """Pydantic 422 → {error: ValidationError, message, detail:[...]}."""
    r = await client.post("/api/v1/memory/memories", json={"bad": "data"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "ValidationError"
    assert body["message"] == "Request validation failed"
    assert isinstance(body["detail"], list)  # FastAPI errors listesi korunur


async def test_unhandled_exception_envelope(app, memory_db, monkeypatch):
    """Beklenmeyen exception → tutarlı 500 {error: InternalError} (raw stack sızmaz).

    NOT: dedicated client raise_app_exceptions=False — Starlette 500-response üretir
    AMA exception'ı re-raise eder (prod=uvicorn loglar+response gönderir; default test
    client re-raise'i görür). Bu flag ile response'u test ederiz.
    """
    from httpx import ASGITransport, AsyncClient

    from app.api.memory import dashboard as dash
    from tests.conftest import TEST_MEMORY_KEY

    def boom():
        raise RuntimeError("beklenmeyen")

    monkeypatch.setattr(dash, "_dashboard_query", boom)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"X-Memory-Key": TEST_MEMORY_KEY}) as c:
        r = await c.get("/api/v1/memory/dashboard")
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "InternalError"
    assert body["message"] == "Internal server error"
    assert "beklenmeyen" not in str(body)  # iç hata-detayı/stack istemciye SIZMAZ
