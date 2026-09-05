"""EventSpineBridge — AgentBus olaylarını server.db.events spine'a yönlendirir.

Bu bridge, AgentBus üzerinden geçen her event'i (subscribe_to_all("*")) alır
ve emit_event() ile server.db.events tablosuna yazar. Böylece in-memory
AgentBus'taki olaylar kalıcı hale gelir ve Dashboard/digest/alert sistemleri
tarafından görülebilir.

Loop guard: source'u "bridge:" ile başlayan event'leri (events.py's bridge'inden
gelen) yeniden emit_event() yapmaz — böylece sonsuz döngü önlenir.

Tasarım:
- Tek yön: AgentBus → events spine (gerekli değil çünkü events.py zaten
  emit_event() her çağrıldığında bus'a publish eder → Yön 1 hazır)
- Lazy: bus başlatılmamışsa sessizce skip (test uyumlu)
- Fail-safe: tek bir event'te hata → diğerleri etkilenmez
"""

from __future__ import annotations

import logging

from app.core.agent_bus import Event

logger = logging.getLogger(__name__)

BUS_TO_SPINE_MAP = {
    "thought:new": "agentbus:thought",
    "thought:deep": "agentbus:thought",
    "critic:score": "agentbus:critic",
    "memory:pattern_detected": "agentbus:memory",
    "learning:threshold_adjusted": "agentbus:learning",
}

BUS_SEVERITY_MAP = {
    "thought:new": "info",
    "thought:deep": "info",
    "critic:score": "info",
    "memory:pattern_detected": "info",
    # 2026-09-03: warn -> info. learning_loop'un rutin oz-ayari (esik guncelleme);
    # haritadaki diger tum bus olaylari zaten info. warn oldugu icin notify-cron
    # bunu Telegram'a tasiyordu (24 saatte 21 mesaj) — olay bir ariza degil,
    # sistemin normal calismasi. events tablosunda kayitli kalir, sadece
    # bildirim esigi altina iner.
    "learning:threshold_adjusted": "info",
}


def _bus_event_to_spine(event: Event) -> None:
    if event.source.startswith("bridge:"):
        return  # loop guard: events.py's bridge'ten gelen event'i geri gönderme
    if getattr(event, "from_db", False):
        return  # loop guard: dispatcher'dan gelen DB event'lerini spine'a geri yazma
    from app.core.events import emit_event

    spine_type = BUS_TO_SPINE_MAP.get(event.type, f"agentbus:{event.type}")
    severity = BUS_SEVERITY_MAP.get(event.type, "info")
    title = event.payload.get("title") or f"[AgentBus] {event.type}"
    detail_parts = []
    for k, v in event.payload.items():
        if isinstance(v, str) and len(v) > 100:
            v = v[:100] + "..."
        detail_parts.append(f"{k}={v}")
    detail = " | ".join(detail_parts)
    emit_event(
        type=spine_type,
        source="bridge:agentbus",
        title=title[:200],
        severity=severity,
        detail=detail[:500],
        payload=event.payload,
    )


async def bridge_handler(event: Event) -> None:
    """AgentBus subscribe_to_all handler'ı. Her event'i spine'a yaz."""
    _bus_event_to_spine(event)
