#!/usr/bin/env python3
"""Telegram long-polling worker — /research komutunu isler.

Public URL gerektirmeden bot trafiği yakalar; getUpdates'i 30sn'lik
long-poll ile sorgular, dönenleri telegram_bot.process_update'e geçirir.

systemd unit: /etc/systemd/system/klipper-telegram-poller.service
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from app.api.telegram_bot import (  # noqa: E402
    TELEGRAM_API,
    TELEGRAM_BOT_TOKEN,
    process_update,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("tg-poller")

POLL_TIMEOUT = 30  # long-poll timeout
SLEEP_ON_ERROR = 5
ALLOWED_UPDATES = ["message"]  # callback_query gerektiginde genislet


def main() -> int:
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN .env'de tanimli degil")
        return 2

    # Webhook setli ise polling 409 verir; once kaldir.
    try:
        r = requests.post(f"{TELEGRAM_API}/deleteWebhook", json={"drop_pending_updates": False}, timeout=10)
        if r.ok:
            log.info("webhook kaldirildi (varsa)")
    except Exception as e:
        log.warning("deleteWebhook fail: %s", e)

    # getMe ile bot kimligi
    try:
        me = requests.get(f"{TELEGRAM_API}/getMe", timeout=10).json()
        if me.get("ok"):
            log.info("bot: @%s (id=%s)", me["result"]["username"], me["result"]["id"])
    except Exception as e:
        log.warning("getMe fail: %s", e)

    offset = 0
    while True:
        try:
            r = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": POLL_TIMEOUT,
                    "allowed_updates": ",".join(ALLOWED_UPDATES) if False else None,
                },
                timeout=POLL_TIMEOUT + 10,
            )
            data = r.json()
            if not data.get("ok"):
                log.warning("getUpdates non-ok: %s", data)
                time.sleep(SLEEP_ON_ERROR)
                continue
            for upd in data.get("result", []):
                offset = max(offset, upd["update_id"] + 1)
                try:
                    result = process_update(upd)
                    if result.get("action"):
                        log.info("update %s -> %s", upd["update_id"], result["action"])
                except Exception:
                    log.exception("process_update fail (update_id=%s)", upd.get("update_id"))
        except requests.exceptions.ReadTimeout:
            # Long-poll timeout normal; donguye devam et
            continue
        except Exception:
            log.exception("poll loop hatasi")
            time.sleep(SLEEP_ON_ERROR)


if __name__ == "__main__":
    sys.exit(main() or 0)
