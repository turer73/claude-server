"""Action/Provider deseni — ElizaOS'tan uyarlama (TS-framework değil, DESEN).

Amaç: ajanlardaki ad-hoc context-toplama + dağınık yetenekleri standartlaştır.
  - Provider: read-only CONTEXT sağlar (son-değişiklik, metrik, git). Ajanlar
    context'i provider'lardan COMPOSE eder → duplikasyon biter (devops-teşhis +
    code-research aynı 'son-değişiklik' context'ini topluyordu).
  - Action: bir ajan-YETENEĞİ (review/learn/research/diagnose). İsimle çağrılır,
    uniform sonuç döner → test-edilebilir, eklenebilir (yeni-mod = yeni Action).

Minimal + Python-idiomatik; full framework DEĞİL (tek-sahip server için over-engineer'a
kaçmadan, gerçek dedup + extensibility).
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Provider(Protocol):
    """Read-only context sağlayıcı. provide() → LLM/karar için context string."""

    name: str

    async def provide(self, **kwargs) -> str: ...


@runtime_checkable
class Action(Protocol):
    """Bir ajan-yeteneği. run() → sonuç dict. validate() opsiyonel ön-koşul."""

    name: str
    description: str

    async def run(self, **kwargs) -> dict: ...


class ActionRegistry:
    """Action'ları isimle kaydet/çağır. Ajan döngüsü buradan dispatch eder."""

    def __init__(self) -> None:
        self._actions: dict[str, Action] = {}

    def register(self, action: Action) -> None:
        self._actions[action.name] = action

    def get(self, name: str) -> Action | None:
        return self._actions.get(name)

    def names(self) -> list[str]:
        return list(self._actions)

    async def run(self, name: str, **kwargs) -> dict | None:
        """Action'ı çalıştır. Yoksa/hata → None (fail-silent, ajan döngüsünü bozmaz)."""
        action = self._actions.get(name)
        if action is None:
            return None
        try:
            return await action.run(**kwargs)
        except Exception:
            logger.exception("action %r failed", name)
            return None


async def compose(providers: list[Provider], **kwargs) -> str:
    """Provider'ların context'ini tek string'e birleştir (ajan girdisi)."""
    parts = []
    for p in providers:
        try:
            text = await p.provide(**kwargs)
            if text:
                parts.append(f"## {p.name}\n{text}")
        except Exception:
            continue
    return "\n\n".join(parts)
