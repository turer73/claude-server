"""DevOpsAgent diagnosis mixin — split from monolithic devops_agent.py."""

from __future__ import annotations

import asyncio

from app.core.devops.models import (
    Alert,
)
from app.core.events import emit_event


class DiagnosisMixin:
    """DevOpsAgent diagnosis mixin — split from monolithic devops_agent.py."""

    def _maybe_diagnose(self, alert: Alert) -> None:
        """Sustained-critical alert için read-only LLM teşhis hipotezi spawn et (once/incident).
        KOMUT ÇALIŞTIRMAZ. Fail-silent — alert akışını asla bozmaz. asyncio.create_task ile
        tick'i bloklamaz (Ollama ~saniyeler sürebilir)."""
        if not self._diagnostic_enabled or alert.source in self._diagnosed:
            return
        self._diagnosed.add(alert.source)
        try:
            asyncio.create_task(self._diagnose_and_emit(alert))
        except RuntimeError:
            # event-loop yok (senkron test bağlamı) — sessizce atla
            self._diagnosed.discard(alert.source)

    async def _diagnose_and_emit(self, alert: Alert) -> None:
        """Read-only context topla → Ollama'ya kök-neden sor → diagnosis event'i emit et."""
        try:
            context = await asyncio.to_thread(self._gather_diag_context)
            hypothesis = await self._ask_diagnosis(alert, context)
            if not hypothesis:
                return
            await asyncio.to_thread(
                emit_event,
                type="alert",
                source=f"diagnosis:{alert.source}",
                title=f"🔍 Teşhis ({alert.source}): {hypothesis[:160]}",
                severity="warning",
                detail=(
                    f"Read-only LLM hipotezi ({self._diag_model}). "
                    f"Alert: {alert.message} (={alert.value}, eşik {alert.threshold}). "
                    f"KOMUT ÇALIŞTIRILMADI — doğrula.\n\n{hypothesis}"
                ),
            )
        except Exception:
            pass

    def _gather_diag_context(self) -> str:
        """Son 7 günde memory'ye kaydedilen değişiklikler (alert-korelasyonu).
        Logic paylaşılan RecentChangesProvider'a çıkarıldı (code-research de aynı
        kaynağı kullanır — duplikasyon-önleme); güncel _diag_memory_db ile delege."""
        from app.core.agents import RecentChangesProvider

        return RecentChangesProvider(self._diag_memory_db)._query()

    async def _ask_diagnosis(self, alert: Alert, context: str) -> str | None:
        """LLMCore ile kök-neden hipotezi sordur (timeout'lu, fail→None). Salt-okuma."""
        from app.core.agents.llmcore import llm_core

        prompt = (
            f"Sistem uyarısı: {alert.source} = {alert.value} (eşik {alert.threshold}). {alert.message}\n\n"
            f"Son 7 günde sistemde kaydedilen değişiklikler:\n{context}\n\n"
            "Bu uyarının MUHTEMEL kök nedenini 2-3 cümlede Türkçe tahmin et. Yukarıdaki "
            "değişikliklerden biriyle korelasyon görüyorsan açıkça belirt. Komut/aksiyon "
            "ÖNERME, sadece hipotez ver. Emin değilsen 'belirsiz' yaz."
        )
        out = await llm_core.generate(prompt, task="diagnosis", timeout=self._diag_timeout)
        return (out.strip()[:600] or None) if out else None
