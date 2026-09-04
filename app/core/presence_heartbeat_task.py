"""Presence bakim dongusu — lease expire + kayitli ajan heartbeats.

Her HEARTBEAT_INTERVAL (15s):
1. expire_leases() — lease_until gecen ajanlari offline yapar
2. bus'ta kayitli ajanlar + sabit liste icin heartbeat atar (lease yenile)

Yazmalar asyncio.to_thread ile worker-thread'de calisir — busy_timeout beklemeleri
event loop'u BLOKLAMAZ (API latency korunur; startup WAL dalgasinda 5s'lik bekleme
olsa bile HTTP worker'lari etkilenmez).

Ajanlarin kendi loop'larina dokunmadan tek noktadan lease taze tutulur.
presence_manager hatalari kendi icinde logger.warning basar — bu dongu
asla patlamaz (fail-safe).
"""

from __future__ import annotations

import asyncio
import logging

from app.core.presence_manager import HEARTBEAT_INTERVAL, presence

logger = logging.getLogger(__name__)

_ALWAYS = ("consciousness", "code_review", "devops")


async def run_presence_heartbeat_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(presence.expire_leases)
            names = set(_ALWAYS)
            try:
                from app.core.agent_bus import get_bus

                bus = get_bus()
                agent_names = getattr(bus, "agent_names", None)
                if callable(agent_names):
                    names.update(agent_names())
            except Exception as e:
                logger.debug("bus agent listesi alinamadi, sabit liste devam: %s", e)
            for name in sorted(names):
                await asyncio.to_thread(presence.heartbeat, name)
        except Exception as e:
            logger.warning("presence bakim dongusu hatasi: %s", e)
        await asyncio.sleep(HEARTBEAT_INTERVAL)
