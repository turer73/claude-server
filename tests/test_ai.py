from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.ai_inference import AIInference


@pytest.fixture
def ai():
    return AIInference(base_url="http://localhost:11434")


def test_ai_init(ai):
    assert ai._base_url == "http://localhost:11434"


@pytest.mark.anyio
async def test_chat_mock(ai):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "message": {"content": "Hello! I'm an AI assistant."},
        "model": "linux-ai-agent",
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await ai.chat("Hello", model="linux-ai-agent")
        assert "response" in result
        assert result["model"] == "linux-ai-agent"


@pytest.mark.anyio
async def test_list_models_mock(ai):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"models": [{"name": "linux-ai-agent"}, {"name": "linux-ai-coder"}]}

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        models = await ai.list_models()
        assert len(models) == 2
        assert models[0]["name"] == "linux-ai-agent"


@pytest.mark.anyio
async def test_chat_connection_error(ai):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=Exception("Connection refused")):
        from app.exceptions import ServerError

        with pytest.raises(ServerError):
            await ai.chat("Hello")


@pytest.mark.anyio
async def test_list_models_connection_error_logs_and_returns_empty(ai, caplog):
    """list_models LLM-offline'da SESSİZ-değil: logger.warning + boş-liste (surer #100190; codecov-gap kapat).
    chat() raise-eder ama list_models graceful-degrade — asimetri kasıtlı (model-listesi opsiyonel)."""
    import logging

    with (
        patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=Exception("Connection refused")),
        caplog.at_level(logging.WARNING),
    ):
        models = await ai.list_models()
    assert models == []  # graceful-degrade (raise DEĞİL)
    assert any("list_models failed" in r.message for r in caplog.records)  # SESSİZ-değil, loglandı
