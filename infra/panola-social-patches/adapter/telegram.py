"""
Telegram Bot adapter — panola-social Faz 2

Konfigurasyon (VPS env):
  TELEGRAM_BOT_TOKEN=...            # @BotFather token
  TELEGRAM_PARSE_MODE=MarkdownV2    # opsiyonel, default HTML

DB (social.db channel_configs):
  product='kuafor', channel='telegram', enabled=1,
  config_json='{"chat_id": "-1001234567890"}'

Bot Setup:
  1. @BotFather -> /newbot -> token al
  2. Channel olustur, bot'u admin yap (post yetkisi)
  3. chat_id al: bot'a /start, https://api.telegram.org/bot<token>/getUpdates
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from .base import ChannelAdapter, PostContent, PublishResult, register_adapter

logger = logging.getLogger(__name__)


TELEGRAM_API = "https://api.telegram.org"
REQUEST_TIMEOUT = 30  # saniye
MAX_TEXT_LEN = 1024   # caption limit (photo ile birlikte); plain message 4096


class TelegramAdapter:
    """Telegram Bot API adapter (sendMessage, sendPhoto, sendMediaGroup)."""

    name = "telegram"

    def __init__(self) -> None:
        self._token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self._parse_mode = os.environ.get("TELEGRAM_PARSE_MODE", "HTML")
        self.enabled = bool(self._token)
        self._db_path = os.environ.get("PANOLA_SOCIAL_DB", "/opt/panola-social/data/social.db")

    # ---- ChannelAdapter protocol ----

    def is_configured(self) -> bool:
        return self.enabled

    def publish(self, product: str, content: PostContent) -> PublishResult:
        if not self.is_configured():
            return PublishResult(
                channel=self.name,
                success=False,
                error="TELEGRAM_BOT_TOKEN tanimli degil",
            )

        chat_id = self._chat_id_for(product)
        if not chat_id:
            return PublishResult(
                channel=self.name,
                success=False,
                error=f"channel_configs kaydi yok: product={product} channel=telegram",
            )

        try:
            text = self._build_text(content)
            images = content.image_urls or []

            if not images:
                resp = self._call("sendMessage", {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": self._parse_mode,
                    "disable_web_page_preview": False,
                })
            elif len(images) == 1:
                resp = self._call("sendPhoto", {
                    "chat_id": chat_id,
                    "photo": images[0],
                    "caption": text[:MAX_TEXT_LEN],
                    "parse_mode": self._parse_mode,
                })
            else:
                media = [
                    {"type": "photo", "media": url,
                     **({"caption": text[:MAX_TEXT_LEN], "parse_mode": self._parse_mode} if i == 0 else {})}
                    for i, url in enumerate(images[:10])  # Telegram max 10
                ]
                resp = self._call("sendMediaGroup", {
                    "chat_id": chat_id,
                    "media": json.dumps(media),
                })

            if not resp.get("ok"):
                return PublishResult(
                    channel=self.name,
                    success=False,
                    error=f"telegram: {resp.get('description', 'unknown')}",
                    raw_response=resp,
                )

            result = resp["result"]
            if isinstance(result, list):
                msg = result[0]
            else:
                msg = result

            return PublishResult(
                channel=self.name,
                success=True,
                external_id=str(msg.get("message_id", "")),
                external_url=self._public_url(chat_id, msg.get("message_id")),
                posted_at=datetime.now(timezone.utc),
                raw_response=resp,
            )
        except Exception as e:
            logger.exception("Telegram publish hata: product=%s", product)
            return PublishResult(
                channel=self.name,
                success=False,
                error=f"exception: {type(e).__name__}: {e}",
            )

    def health_check(self) -> dict[str, Any]:
        if not self.is_configured():
            return {"status": "fail", "reason": "no_token"}
        try:
            resp = self._call("getMe", {})
            if resp.get("ok"):
                bot = resp["result"]
                return {
                    "status": "ok",
                    "bot_username": bot.get("username"),
                    "bot_id": bot.get("id"),
                }
            return {"status": "degraded", "reason": resp.get("description", "?")}
        except Exception as e:
            return {"status": "fail", "reason": f"{type(e).__name__}: {e}"}

    # ---- internal ----

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{TELEGRAM_API}/bot{self._token}/{method}"
        r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "description": f"http_{r.status_code}: non_json_response"}

    def _chat_id_for(self, product: str) -> Optional[str]:
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT config_json FROM channel_configs "
                    "WHERE product=? AND channel='telegram' AND enabled=1",
                    (product,),
                ).fetchone()
                if not row:
                    return None
                cfg = json.loads(row[0] or "{}")
                return str(cfg.get("chat_id") or "") or None
        except Exception:
            return None

    def _build_text(self, content: PostContent) -> str:
        """text + hashtags + link birlestir."""
        parts = [content.text]
        if content.link_url:
            parts.append(f"\n{content.link_url}")
        if content.hashtags:
            tags = " ".join(f"#{h.strip('#')}" for h in content.hashtags)
            parts.append(f"\n{tags}")
        return "\n".join(parts)

    def _public_url(self, chat_id: str, message_id: Optional[int]) -> Optional[str]:
        """Public channel ise t.me/<username>/<message_id> URL'i."""
        if not message_id:
            return None
        # chat_id "@username" formatinda olabilir veya numeric
        cid = str(chat_id)
        if cid.startswith("@"):
            return f"https://t.me/{cid[1:]}/{message_id}"
        # Private/numeric channel icin URL yok
        return None


# Modul yuklenirken kayit
register_adapter(TelegramAdapter())
