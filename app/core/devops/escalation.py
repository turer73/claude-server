"""DevOpsAgent escalation mixin — split from monolithic devops_agent.py."""

from __future__ import annotations

import asyncio
import shlex
import time

from app.core.devops._base import _DevOpsAgentBase
from app.core.devops.models import (
    Alert,
)
from app.core.events import emit_event


class EscalationMixin(_DevOpsAgentBase):
    """DevOpsAgent escalation mixin — split from monolithic devops_agent.py."""

    async def _escalate_persistent(self) -> None:
        """Çözülmeyen critical alert'leri _escalation_interval'da yeniden bildir
        (okunmamış/ele-alınmamış critical sessizce unutulmasın). Re-ping = yeni
        escalation event -> notify-cron -> Telegram (aksiyon-önerili). Best-effort."""
        nowm = time.monotonic()
        for source, alert in list(self._active_alerts.items()):
            if alert.severity != "critical":
                continue
            # ilk-görülme: kaynak NEREDE yaratılırsa yaratılsın (_detect / _check_services
            # service:* / _check_vps vps:*) eskalasyon-saatini burada başlat -> interval
            # sonra re-escalate. (Codex P2: yalnız _detect-init metrik-dışı critical'leri
            # kaçırıyordu; tek-nokta uniform-init ile hepsi kapsanır.)
            if source not in self._last_escalation:
                self._last_escalation[source] = nowm
                continue
            elapsed = nowm - self._last_escalation[source]
            if elapsed < self._escalation_interval:
                continue
            # ACK-saygı: kullanıcı Telegram '✅ Gördüm' ile bu kaynağın son alert/
            # escalation event'ini onayladıysa YENİDEN BASMA (nag-etme). Skip ->
            # yeni unacked event yaratılmaz -> latest acked kalır -> sessiz (auto-resolve'a dek).
            if await self._source_acked(source):
                continue
            self._last_escalation[source] = nowm
            mins = int(elapsed / 60)
            try:
                await asyncio.to_thread(
                    emit_event,
                    type="alert",
                    source=f"escalation:{source}",
                    title=f"HÂLÂ AÇIK (~{mins}dk): {alert.message} — çözülmedi, manuel müdahale gerek",
                    severity="critical",
                    detail="Otonom remediation kapalı/yetmedi; bu kaynak hâlâ kritik eşikte.",
                )
            except Exception:
                pass

    async def _source_acked(self, source: str) -> bool:
        """Bu kaynağın EN SON alert/escalation event'i kullanıcı tarafından ACK'lendi mi
        (events.acked). Telegram '✅ Gördüm' butonu poller'da acked=1 yapar -> escalation
        durur. Best-effort (db yok/hata -> False = escalate-devam, fail-loud tercih)."""
        if not self._db:
            return False
        try:
            row = await self._db.fetch_one(
                "SELECT acked FROM events WHERE source IN (?, ?) ORDER BY id DESC LIMIT 1",
                (source, f"escalation:{source}"),
            )
            return bool(row and row.get("acked"))
        except Exception:
            return False

    async def _verify_remediation(self, source: str) -> bool | None:
        """Aksiyon sonrası health re-check. True=düzeldi, False=hâlâ sorunlu,
        None=verify-edilemez (cpu sadece-log / belirsiz). Heuristik: cleanup etkisi
        gecikebilir -> False-fail mümkün (sonucu sadece escalate-notify, yıkıcı değil)."""
        base = source.split(":", 1)[0]
        try:
            if base == "service":
                svc = source.split(":", 1)[1]
                # shlex.quote = savunma-derinliği (refused adlar buraya ulaşamaz ama
                # source-string ileride başka üreticiden gelebilir — Codex P1 simetrisi).
                r = await self._executor.execute(f"systemctl is-active {shlex.quote(svc)}", timeout=10)
                return bool(r.get("stdout", "").strip() == "active")
            if base == "docker":
                cont = source.split(":", 1)[1]
                # Codex P2: Running=true unhealthy'de de doğru -> false-recovery. Health-status'a
                # da bak: healthcheck'li container 'healthy' olmalı; healthcheck'siz (none) ->
                # Running yeter. Çıktı "<running>;<health|none>" (örn "true;healthy"/"true;none").
                r = await self._executor.execute(
                    f"docker inspect -f '{{{{.State.Running}}}};{{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{else}}}}none{{{{end}}}}' {shlex.quote(cont)}",
                    timeout=10,
                )
                parts = r.get("stdout", "").strip().lower().split(";")
                running = len(parts) >= 1 and parts[0] == "true"
                health = parts[1] if len(parts) >= 2 else "none"
                if not running or health == "unhealthy":
                    return False
                if health == "starting":
                    return None  # Codex P2: start_period geçici -> belirsiz, escalate ETME
                return True  # healthy veya healthcheck'siz (none)
            # metrik playbook: yeniden örnekle. cpu_critical SADECE log -> verify yok.
            key = {"memory": "memory_percent", "disk": "disk_percent", "temperature": "temperature"}.get(base)
            if not key:
                return None
            metrics = self._monitor.collect_metrics()
            val = metrics.get(key)
            thr = self._thresholds.get(base)
            if val is None or thr is None:
                return None
            return bool(val < thr)
        except Exception:
            return None  # verify-edilemedi -> belirsiz (escalate etme)

    async def _verify_and_escalate(self, source: str, alert: Alert) -> None:
        """FAZ5-S2: yalnız mode=auto (notify'da exec yok -> verify-edecek şey yok).
        verify -> ledger.verify_status güncelle; fail -> escalate (critical event +
        escalated=1). Rollback: çoğu aksiyon geri-alınamaz -> escalate (manuel müdahale)."""
        if self._remediation_mode != "auto":
            return
        # kısa grace: restart/cleanup etkisinin oturması için (False-fail azalt).
        if self._verify_grace:
            await asyncio.sleep(self._verify_grace)
        ok = await self._verify_remediation(source)
        status = "n/a" if ok is None else ("pass" if ok else "fail")
        escalated = status == "fail"
        # INTERV: verify FAIL + reversible-state yakalı + flapping-değil -> AUTO-ROLLBACK
        # (önceki governor'a dön). Rollback ÇÖZÜM DEĞİL -> yine escalate (manuel müdahale).
        rolled_back = False
        rb_result = ""
        if escalated:
            rolled_back, rb_result = await self._attempt_rollback(source)
        else:
            # surer F1: verify-PASS'te de yakalı state'i TEMİZLE — yoksa bayat governor-state
            # sonraki (olası irreversible) aksiyonun verify-fail'inde yanıltıcı rollback tetikler.
            self._rollback_state.pop(source, None)
        if self._db:
            try:
                await self._db.execute(
                    "UPDATE remediation_log SET verify_status=?, escalated=?, rolled_back=?, rollback_result=? "
                    "WHERE alert_source=? AND verify_status IS NULL",
                    (status, 1 if escalated else 0, 1 if rolled_back else 0, rb_result or None, source),
                )
            except Exception:
                pass
        if escalated:
            # ESCALATE: otonom remediation çalıştı ama sorun sürüyor -> manuel müdahale.
            rb_note = f" [auto-rollback: {rb_result}]" if rolled_back else ""
            try:
                await asyncio.to_thread(
                    emit_event,
                    type="alert",
                    source=f"remediation:{source}",
                    title=f"Otonom remediation BAŞARISIZ: {source} hâlâ kritik — manuel müdahale gerek",
                    severity="critical",
                    detail=f"auto-remediation yürütüldü ama verify başarısız ({alert.message}).{rb_note}",
                )
            except Exception:
                pass

    async def _send_webhook(self, alert: Alert) -> None:
        """Notify via webhook for n8n integration."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    "http://localhost:8420/api/v1/monitor/webhooks/receive",
                    json={
                        "source": "devops-agent",
                        "event": "remediation",
                        "data": {
                            "alert_source": alert.source,
                            "severity": alert.severity,
                            "message": alert.message,
                            "remediation": alert.remediation,
                            "timestamp": alert.timestamp,
                        },
                    },
                )
        except Exception:
            pass
