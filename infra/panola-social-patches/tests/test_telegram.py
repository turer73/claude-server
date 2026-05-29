"""
Telegram adapter pytest.

Calistirmadan once /opt/panola-social/data/social.db migration 001 uygulanmali.
TELEGRAM_BOT_TOKEN env set ise live test; degil ise mock.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# Path setup - adapter modulu /opt/panola-social/adapter altinda olacak
sys.path.insert(0, str(Path("/opt/panola-social").resolve()))


@pytest.fixture
def temp_db():
    """Gecici SQLite DB ile channel_configs tablosu."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(open(
        Path(__file__).parent.parent / "sql" / "001_channel_configs.sql"
    ).read())
    # Test kaydi: kuafor telegram chat_id ile
    conn.execute(
        "UPDATE channel_configs SET enabled=1, config_json=? "
        "WHERE product='kuafor' AND channel='telegram'",
        (json.dumps({"chat_id": "@test_kuafor_channel"}),),
    )
    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


@pytest.fixture
def adapter(temp_db):
    """TelegramAdapter mock token ile."""
    with patch.dict(os.environ, {
        "TELEGRAM_BOT_TOKEN": "MOCK_TOKEN_12345",
        "PANOLA_SOCIAL_DB": temp_db,
        "TELEGRAM_PARSE_MODE": "HTML",
    }):
        from adapter.telegram import TelegramAdapter
        yield TelegramAdapter()


def test_disabled_without_token(temp_db):
    """Token yoksa adapter disabled."""
    with patch.dict(os.environ, {"PANOLA_SOCIAL_DB": temp_db}, clear=False):
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        from adapter.telegram import TelegramAdapter
        a = TelegramAdapter()
        assert not a.is_configured()
        assert not a.enabled


def test_publish_text_only(adapter):
    from adapter.base import PostContent

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "result": {"message_id": 42, "chat": {"id": -100123}, "text": "test"},
    }

    with patch("adapter.telegram.requests.post", return_value=mock_response) as mocked:
        result = adapter.publish("kuafor", PostContent(text="Test mesaj"))

    assert result.success
    assert result.channel == "telegram"
    assert result.external_id == "42"
    assert result.external_url == "https://t.me/test_kuafor_channel/42"
    # API URL kontrolu
    call_args = mocked.call_args
    assert "/sendMessage" in call_args[0][0]
    assert call_args[1]["data"]["chat_id"] == "@test_kuafor_channel"


def test_publish_single_photo(adapter):
    from adapter.base import PostContent

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "result": {"message_id": 43, "chat": {"id": -100123}},
    }

    with patch("adapter.telegram.requests.post", return_value=mock_response) as mocked:
        result = adapter.publish(
            "kuafor",
            PostContent(text="Foto", image_urls=["https://example.com/a.jpg"]),
        )

    assert result.success
    assert "/sendPhoto" in mocked.call_args[0][0]


def test_publish_media_group(adapter):
    from adapter.base import PostContent

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "result": [{"message_id": 50}, {"message_id": 51}],
    }

    with patch("adapter.telegram.requests.post", return_value=mock_response) as mocked:
        result = adapter.publish(
            "kuafor",
            PostContent(text="Carousel", image_urls=[
                "https://example.com/a.jpg",
                "https://example.com/b.jpg",
            ]),
        )

    assert result.success
    assert result.external_id == "50"  # ilk message_id
    assert "/sendMediaGroup" in mocked.call_args[0][0]


def test_publish_no_channel_config(adapter):
    """channel_configs'ta kayit yoksa fail."""
    from adapter.base import PostContent

    result = adapter.publish("nonexistent_product", PostContent(text="x"))
    assert not result.success
    assert "channel_configs" in result.error


def test_publish_telegram_error(adapter):
    """Telegram API ok=false ise fail rapor et."""
    from adapter.base import PostContent

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": False,
        "description": "Bad Request: chat not found",
    }

    with patch("adapter.telegram.requests.post", return_value=mock_response):
        result = adapter.publish("kuafor", PostContent(text="x"))

    assert not result.success
    assert "chat not found" in result.error


def test_build_text_with_hashtags_link(adapter):
    from adapter.base import PostContent
    text = adapter._build_text(PostContent(
        text="Ana metin",
        link_url="https://kuafor.panola.app",
        hashtags=["kuafor", "salon"],
    ))
    assert "Ana metin" in text
    assert "https://kuafor.panola.app" in text
    assert "#kuafor" in text
    assert "#salon" in text


def test_health_check_unconfigured(temp_db):
    with patch.dict(os.environ, {"PANOLA_SOCIAL_DB": temp_db}, clear=False):
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        from adapter.telegram import TelegramAdapter
        h = TelegramAdapter().health_check()
        assert h["status"] == "fail"


def test_whatsapp_skeleton_dormant():
    """WhatsApp adapter skeleton — is_configured False, publish error doner."""
    from adapter.whatsapp import WhatsAppAdapter
    from adapter.base import PostContent

    a = WhatsAppAdapter()
    assert not a.is_configured()
    h = a.health_check()
    assert h["status"] == "skeleton"
    assert h["implemented"] is False

    result = a.publish("kuafor", PostContent(text="test"))
    assert not result.success
    assert "not_implemented" in result.error


def test_adapter_registry_contains_both():
    """Module import sonrasi her iki adapter registry'de."""
    from adapter import ADAPTER_REGISTRY
    assert "telegram" in ADAPTER_REGISTRY
    assert "whatsapp" in ADAPTER_REGISTRY
