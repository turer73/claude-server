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
# NOT: allowed_updates KISITLANMAZ (default). process_update message + edited_message +
# callback_query işler; kısıtlı bir allowlist edited_message'i düşürür (düzenlenen mesajla
# /research/'/claude' kırılır) → default'ta kal. (Eski 'if False' ölü-kod artığıydı.)


def _startup_gate() -> None:
    """Ağ hazır olana kadar bekle (boot-DNS-lag'e dayanıklı), SONRA webhook temizle.

    Boot'ta network-online.target erişilse de DNS henüz çözmeyebilir; eski kod deleteWebhook/
    getMe'yi tek-atış deniyordu → fail'de webhook ASLA temizlenmiyordu (webhook set ise polling
    kalıcı 409) + ilk getUpdates tam-traceback ('poll loop hatasi') basıyordu. Burada getMe'yi
    bağlantı-probe olarak kullanıp ilk YANIT gelene dek retry ediyoruz (yanıt=ağ hazır), ardından
    webhook'u temizliyoruz. Token/API-seviye hata (ok=False) retry ETMEZ — polling kendi raporlar."""
    while True:
        try:
            me = requests.get(f"{TELEGRAM_API}/getMe", timeout=10).json()
            if me.get("ok"):
                log.info("bot: @%s (id=%s)", me["result"]["username"], me["result"]["id"])
            else:
                log.warning("getMe non-ok (token/API sorunu?): %s", me)
            break  # herhangi bir YANIT geldi = ağ hazır
        except requests.exceptions.RequestException as e:
            log.warning("ağ bekleniyor (getMe fail), %ss sonra retry: %s", SLEEP_ON_ERROR, e)
            time.sleep(SLEEP_ON_ERROR)

    # Ağ hazır → webhook setli ise polling 409 verir; kaldır.
    try:
        r = requests.post(f"{TELEGRAM_API}/deleteWebhook", json={"drop_pending_updates": False}, timeout=10)
        if r.ok:
            log.info("webhook kaldirildi (varsa)")
    except requests.exceptions.RequestException as e:
        log.warning("deleteWebhook fail: %s", e)


def main() -> int:
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN .env'de tanimli degil")
        return 2

    _startup_gate()

    offset = 0
    while True:
        try:
            r = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params={"offset": offset, "timeout": POLL_TIMEOUT},
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
        except requests.exceptions.ConnectionError as e:
            # DNS/ağ transient (boot-lag, geçici kesinti) — tam-traceback GÜRÜLTÜSÜ yok,
            # kısa uyarı + backoff. Loop zaten kendini toparlar.
            log.warning("bağlantı hatası (transient), %ss sonra retry: %s", SLEEP_ON_ERROR, e)
            time.sleep(SLEEP_ON_ERROR)
        except Exception:
            log.exception("poll loop hatasi")  # gerçekten beklenmedik → tam traceback
            time.sleep(SLEEP_ON_ERROR)


if __name__ == "__main__":
    sys.exit(main() or 0)
