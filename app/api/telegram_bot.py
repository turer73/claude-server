"""Telegram bot webhook — /research command routing.

Bot: vps_backup_3dlabx_bot (token .env -> TELEGRAM_BOT_TOKEN).
Webhook secret: TELEGRAM_WEBHOOK_SECRET (.env'de tanimli olmali).
Telegram setWebhook ile {PUBLIC_URL}/webhooks/telegram/update'e baglanir;
Telegram her update'i bu endpoint'e POST eder.

Akis: /research <soru> mesaji -> research_ask -> Markdown yanit + sendMessage.
Auth: secret_token header (Telegram'in setWebhook ile bekledigi mekanizma);
verify_key gerek yok (bu router public-mountlu).
"""

from __future__ import annotations

import re

import requests
from fastapi import APIRouter, Header, HTTPException

from app.api.research import AskRequest, research_ask
from app.core.config import read_env_var

TELEGRAM_BOT_TOKEN = read_env_var("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = read_env_var("TELEGRAM_WEBHOOK_SECRET")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

router = APIRouter(prefix="/webhooks/telegram", tags=["telegram-webhook"])


def _send_message(chat_id: int, text: str, reply_to: int | None = None) -> None:
    """Markdown sendMessage helper. Best-effort, sessizce fail eder."""
    if not TELEGRAM_BOT_TOKEN:
        return
    payload: dict = {
        "chat_id": chat_id,
        "text": text[:4000],  # Telegram limit 4096
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
    except Exception:
        pass


def _send_typing(chat_id: int) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"{TELEGRAM_API}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=3,
        )
    except Exception:
        pass


def _md_escape(text: str) -> str:
    """Markdown v1 — sadece backtick/underscore/star kacir."""
    return re.sub(r"([`_*\[\]])", r"\\\1", text)


def _format_reply(result: dict, q: str) -> str:
    """Research API yanitini Telegram Markdown'a cevir.

    Yapı:
      *Q:* soru
      <answer (max 2500 char)>
      _Kaynaklar:_ tag1, tag2 (max 6)
      `engine` · N kaynak · Ms · (⚠ hallu varsa)
    """
    answer = result.get("answer", "") or "(bos cevap)"
    engine = result.get("engine", "?")
    src_count = result.get("source_count", 0)
    cites = result.get("citations", {})
    used = cites.get("used", [])
    halu = cites.get("hallucinated", [])
    dur = result.get("duration_ms", {}).get("total", 0)

    parts = [f"*Q:* {_md_escape(q[:200])}", "", answer[:2500]]
    if used:
        cite_str = ", ".join(used[:6]) + (f" +{len(used) - 6}" if len(used) > 6 else "")
        parts.extend(["", f"_Kaynaklar:_ {cite_str}"])
    footer = f"`{engine}` · {src_count} kaynak · {dur}ms"
    if halu:
        footer += f" · ⚠️ {len(halu)} hallu"
    parts.extend(["", footer])
    return "\n".join(parts)


def process_update(update: dict) -> dict:
    """Webhook + polling tarafindan paylasilan core handler — auth-bagimsiz."""
    msg = update.get("message") or update.get("edited_message") or {}
    text = msg.get("text", "") or ""
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")

    if not chat_id:
        return {"ok": True, "skipped": "no chat_id"}

    # /research veya /research@bot_username
    if not re.match(r"^/research(@\w+)?(\s|$)", text):
        return {"ok": True, "skipped": "not /research"}

    question = re.sub(r"^/research(@\w+)?\s*", "", text).strip()
    if not question:
        _send_message(
            chat_id,
            "*Kullanim:*\n`/research <soru>`\n\n*Ornek:*\n`/research bilge-arena security header eksiklikleri`",
            reply_to=msg_id,
        )
        return {"ok": True, "action": "help"}

    _send_typing(chat_id)

    try:
        req = AskRequest(q=question, engine="auto")
        result = research_ask(req)
        reply = _format_reply(result, question)
    except Exception as e:
        reply = f"❌ *Research hatasi:*\n`{str(e)[:300]}`"

    _send_message(chat_id, reply, reply_to=msg_id)
    return {"ok": True, "action": "answered"}


@router.post("/update")
def telegram_update(
    update: dict,
    x_telegram_bot_api_secret_token: str | None = Header(None),
):
    """Telegram webhook receiver. Secret_token ile auth."""
    if TELEGRAM_WEBHOOK_SECRET:
        if x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
            raise HTTPException(403, "invalid webhook secret")
    return process_update(update)
