"""Tests for Telegram bot webhook + polling-shared core (process_update).

process_update auth-bagimsiz; webhook endpoint secret_token ile korunur.
"""

from unittest.mock import patch

import pytest


@pytest.mark.anyio
async def test_webhook_route_registered(client):
    resp = await client.get("/openapi.json")
    paths = resp.json()["paths"]
    assert "/webhooks/telegram/update" in paths


def _fake_update(text: str, chat_id: int = 123, msg_id: int = 1) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": msg_id,
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def test_process_update_skips_non_research():
    from app.api.telegram_bot import process_update

    with patch("app.api.telegram_bot._send_message") as snd, patch("app.api.telegram_bot.research_ask") as ask:
        out = process_update(_fake_update("merhaba"))
    assert out["ok"]
    assert out["skipped"] == "not /research"
    snd.assert_not_called()
    ask.assert_not_called()


def test_process_update_help_when_empty_question():
    from app.api.telegram_bot import process_update

    with patch("app.api.telegram_bot._send_message") as snd, patch("app.api.telegram_bot.research_ask") as ask:
        out = process_update(_fake_update("/research"))
    assert out["action"] == "help"
    snd.assert_called_once()
    # Help text gonderildi, research_ask cagirilmamali
    ask.assert_not_called()
    args, kwargs = snd.call_args
    assert "Kullanim" in args[1]


def test_process_update_calls_research_and_sends_reply():
    from app.api.telegram_bot import process_update

    fake_result = {
        "answer": "petvet'te HSTS eksik [discovery:323]",
        "engine": "claude",
        "source_count": 1,
        "citations": {"used": ["discovery:323"], "hallucinated": [], "unused": []},
        "duration_ms": {"total": 1234, "retrieval": 5, "synthesis": 1229},
    }
    with (
        patch("app.api.telegram_bot.research_ask", return_value=fake_result) as ask,
        patch("app.api.telegram_bot._send_message") as snd,
        patch("app.api.telegram_bot._send_typing"),
    ):
        out = process_update(_fake_update("/research petvet headers"))
    assert out["action"] == "answered"
    ask.assert_called_once()
    snd.assert_called_once()
    # Yanit metninde answer + citation tag bulunmali
    sent_text = snd.call_args[0][1]
    assert "petvet'te HSTS eksik" in sent_text
    assert "discovery:323" in sent_text
    assert "claude" in sent_text  # engine footer
    assert "1234ms" in sent_text


def test_process_update_handles_bot_mention():
    from app.api.telegram_bot import process_update

    with (
        patch(
            "app.api.telegram_bot.research_ask",
            return_value={
                "answer": "x",
                "engine": "local",
                "source_count": 0,
                "citations": {"used": [], "hallucinated": [], "unused": []},
                "duration_ms": {"total": 1},
            },
        ),
        patch("app.api.telegram_bot._send_message"),
        patch("app.api.telegram_bot._send_typing"),
    ):
        # /research@botname formati
        out = process_update(_fake_update("/research@vps_backup_3dlabx_bot test"))
    assert out["action"] == "answered"


def test_process_update_no_chat_id():
    """Update'te chat_id yoksa skip."""
    from app.api.telegram_bot import process_update

    bad = {"update_id": 1, "message": {"text": "/research test"}}
    out = process_update(bad)
    assert out["skipped"] == "no chat_id"


@pytest.mark.anyio
async def test_webhook_secret_rejected_when_wrong(client, monkeypatch):
    """TELEGRAM_WEBHOOK_SECRET set ise wrong header -> 403."""
    monkeypatch.setattr("app.api.telegram_bot.TELEGRAM_WEBHOOK_SECRET", "expected-secret")
    resp = await client.post(
        "/webhooks/telegram/update",
        json={"update_id": 1, "message": {}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_webhook_secret_accepted_when_correct(client, monkeypatch):
    monkeypatch.setattr("app.api.telegram_bot.TELEGRAM_WEBHOOK_SECRET", "expected-secret")
    resp = await client.post(
        "/webhooks/telegram/update",
        json={"update_id": 1, "message": {}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "expected-secret"},
    )
    assert resp.status_code == 200
