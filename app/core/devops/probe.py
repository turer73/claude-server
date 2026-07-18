"""DevOpsAgent probe mixin — split from monolithic devops_agent.py."""

from __future__ import annotations

import asyncio
import logging
import shlex
from datetime import UTC, datetime
from typing import Any

from app.core.devops._base import _DevOpsAgentBase
from app.core.devops.models import (
    _VALID_UNIT,
    VPS_PROBE_B64,
    Alert,
    parse_vps_probe,
)

log = logging.getLogger("devops_agent")


class ProbeMixin(_DevOpsAgentBase):
    """DevOpsAgent probe mixin — split from monolithic devops_agent.py."""

    async def _check_services(self) -> None:
        """Check critical systemd services and Docker containers."""
        now = datetime.now(UTC).isoformat()

        # Systemd services
        for svc in self._critical_services:
            try:
                # GÜVENLİK (Codex P1): ad-doğrulama PROBE'dan önce — probe da f-string ile
                # tam-shell'e gider, remediate'teki guard tek başına yetmez (enjeksiyonlu
                # ad probe'da çalışırdı). Geçersiz ad shell'e HİÇ gömülmez; sessiz değil:
                # alarm yolu akar, _remediate_service refused-satırı+webhook yazar.
                if not _VALID_UNIT.fullmatch(svc):
                    problem: str | None = f"Service adi gecersiz ({svc[:60]!r}) — izlenemiyor (enjeksiyon riski)"
                else:
                    result = await self._executor.execute(f"systemctl is-active {shlex.quote(svc)}", timeout=5)
                    problem = None if result.get("stdout", "").strip() == "active" else f"Service {svc} is not active"
                if problem:
                    source = f"service:{svc}"
                    if source not in self._active_alerts:
                        alert = Alert(
                            id=f"{source}-{self._check_count}",
                            severity="critical",
                            source=source,
                            message=problem,
                            value=0,
                            threshold=1,
                            timestamp=now,
                        )
                        self._active_alerts[source] = alert
                        # disc#1353a: ilk-an kalıcılık+bildirim — service alert'leri alerts-tablosuna/
                        # events-köprüsüne HİÇ yazılmıyordu; ilk "X down" anı ne alerts/history'de ne
                        # Telegram-hattında görünüyordu (yalnız 30dk-sonraki escalation). Metrik-yolu
                        # (metrics.py _detect:132) ile simetrik.
                        asyncio.create_task(self._store_alert(alert))
                        await self._remediate_service(svc, alert)
            except Exception as e:
                # disc#1353b: sessiz-yutma bug-maskeler (07-18 DB-lock 6-saat-körlük dersi).
                # Yalnız tip-adı loglanır (PR#339 Codex-dersi: exception-str komut-metni taşıyabilir).
                log.warning("service-probe %s failed: %s", svc, type(e).__name__)

        # Docker containers
        for container in self._critical_containers:
            try:
                # GÜVENLİK (Codex P1): servis yoluyla simetrik — doğrulama probe'dan önce.
                if not _VALID_UNIT.fullmatch(container):
                    down, unhealthy, status = True, False, ""
                    invalid_msg = f"Container adi gecersiz ({container[:60]!r}) — izlenemiyor (enjeksiyon riski)"
                else:
                    invalid_msg = None
                    result = await self._executor.execute(
                        f"docker ps --filter name={shlex.quote(container)} --format '{{{{.Status}}}}'", timeout=5
                    )
                    status = result.get("stdout", "").strip()
                    # Codex P2: 'Up (unhealthy)' de 'Up' içerir -> çalışıyor-ama-unhealthy kaçardı.
                    # Healthcheck'li container (n8n/qdrant) unhealthy = kritik outage -> yakala.
                    down = not status or "Up" not in status
                    unhealthy = "unhealthy" in status.lower()
                if down or unhealthy:
                    source = f"docker:{container}"
                    if source not in self._active_alerts:
                        msg = invalid_msg or (
                            f"Container {container} is not running" if down else f"Container {container} UNHEALTHY ({status})"
                        )
                        alert = Alert(
                            id=f"{source}-{self._check_count}",
                            severity="critical",
                            source=source,
                            message=msg,
                            value=0,
                            threshold=1,
                            timestamp=now,
                        )
                        self._active_alerts[source] = alert
                        # disc#1353a: service-yoluyla simetrik ilk-an kalıcılık.
                        asyncio.create_task(self._store_alert(alert))
                        await self._remediate_container(container, alert)
            except Exception as e:
                # disc#1353b: sessiz-yutma yerine tip-adı logu (payload'sız).
                log.warning("container-probe %s failed: %s", container, type(e).__name__)

    async def _vps_ssh_probe(self) -> dict[str, Any] | None:
        """Run the fixed VPS metric probe over SSH via an isolated subprocess.

        Bypasses the user-facing ShellExecutor on purpose: this is a fixed,
        internal command with no user input, and `ssh` is deliberately absent
        from shell_whitelist (routing it through the executor raises
        AuthorizationError). Returns the parsed sample, or None if the VPS is
        unreachable / the output is unusable.
        """
        if not self._vps_host:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "BatchMode=yes",
                self._vps_host,
                f"echo {VPS_PROBE_B64} | base64 -d | bash",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except (TimeoutError, OSError):
            return None
        if proc.returncode != 0:
            return None
        parsed = parse_vps_probe(out.decode(errors="replace"))
        if parsed["cpu"] is None:  # partial/unparseable output → treat as failure
            return None
        return parsed

    async def _store_vps_metrics(self, sample: dict[str, Any], online: bool) -> None:
        if not self._db:
            return
        try:
            await self._db.execute(
                """INSERT INTO vps_metrics_history
                   (timestamp, online, cpu_usage, memory_usage, disk_usage, containers_total, containers_up)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(UTC).isoformat(),
                    1 if online else 0,
                    sample.get("cpu"),
                    sample.get("mem"),
                    sample.get("disk"),
                    sample.get("containers_total"),
                    sample.get("containers_up"),
                ),
            )
        except Exception:
            # #1334: bu satır önceden sessizdi -> meta-monitor "dead" alarmı atarken
            # kök-neden görünmezdi (85dk-lik 07-14 kesintisi log-izsiz kaldı). Artık log'lu.
            log.warning("vps_metrics_history insert failed", exc_info=True)

    async def _local_internet_up(self) -> bool:
        """Whether klipper itself has outbound internet right now.

        Used to disambiguate a failed VPS SSH probe: if our own WAN is down, the
        failure is local, not a VPS outage. Tries a short TCP connect to public
        anycast resolvers; any success → internet up. ICMP is avoided (often
        filtered, needs the ping binary); a raw TCP connect needs no privileges.
        """
        for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
            try:
                fut = asyncio.open_connection(host, port)
                _, writer = await asyncio.wait_for(fut, timeout=3)
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                return True
            except (TimeoutError, OSError):
                continue
        return False

    async def _check_vps(self) -> None:
        """Collect VPS host metrics + container state via the SSH probe, persist, alert."""
        now = datetime.now(UTC).isoformat()
        probe = await self._vps_ssh_probe()

        if probe is None:
            self._vps_probe_fails += 1
            await self._store_vps_metrics({}, online=False)
            self._latest_vps = {"online": False, "timestamp": now}
            # SUSTAINED-GATE: tek geçici probe-fail (SSH timeout / anlık blip / VPS-busy)
            # ANINDA alert üretmesin — N ardışık-fail = gerçek kesinti. Tek-blip → bekle,
            # sıradaki tick'te retry. (metrik-alarmlarındaki _sustained_high simetrisi.)
            if self._vps_probe_fails < self._vps_fail_threshold:
                return
            # Disambiguate before blaming the VPS: an SSH-probe failure during a
            # local internet outage means *klipper's own WAN* dropped, not that the
            # VPS is down. Without this, every klipper ISP/DNS hiccup produced a
            # false "vps:offline" alert storm (2026-06-17 ~1h WAN blip incident).
            if not await self._local_internet_up():
                source = "klipper:wan-down"
                if source not in self._active_alerts:
                    alert = Alert(
                        id=f"{source}-{self._check_count}",
                        severity="critical",
                        source=source,
                        message="klipper has no outbound internet — VPS reachability unknown",
                        value=0,
                        threshold=1,
                        timestamp=now,
                    )
                    self._active_alerts[source] = alert
                    asyncio.create_task(self._store_alert(alert))  # disc#1353a
                return
            source = "vps:offline"
            if source not in self._active_alerts:
                alert = Alert(
                    id=f"{source}-{self._check_count}",
                    severity="critical",
                    source=source,
                    message="VPS is unreachable",
                    value=0,
                    threshold=1,
                    timestamp=now,
                )
                self._active_alerts[source] = alert
                asyncio.create_task(self._store_alert(alert))  # disc#1353a
            return

        self._vps_probe_fails = 0  # başarılı probe → ardışık-fail sayacı sıfır
        await self._store_vps_metrics(probe, online=True)
        self._latest_vps = {**probe, "online": True, "timestamp": now}

        # Auto-resolve VPS offline / WAN-down alerts: a successful probe proves both
        # the VPS *and* our own internet are up.
        # Codex-P2 (PR#340 follow-up): DB-resolve KOŞULSUZ, in-memory varlığına bağlı DEĞİL —
        # alert DB'ye yazıldıktan sonra healthy-probe'dan önce restart olursa _active_alerts
        # boşalır; in-memory-bağlı resolve DB satırını sonsuza dek açık bırakırdı (stuck-open).
        for resolved in ("vps:offline", "klipper:wan-down"):
            self._active_alerts.pop(resolved, None)
            asyncio.create_task(self._resolve_alert_db_by_source(resolved, now))

        # Per-container down/up alerts (exact name match against running set)
        running = set(probe.get("names", []))
        for container in self._vps_containers:
            source = f"vps:{container}"
            if container not in running:
                if source not in self._active_alerts:
                    alert = Alert(
                        id=f"{source}-{self._check_count}",
                        severity="warning",
                        source=source,
                        message=f"VPS container {container} not running",
                        value=0,
                        threshold=1,
                        timestamp=now,
                    )
                    self._active_alerts[source] = alert
                    asyncio.create_task(self._store_alert(alert))  # disc#1353a
            else:
                # Codex-P2 (PR#340 follow-up): koşulsuz kaynak-bazlı DB-resolve (idempotent no-op
                # açık-satır-yoksa) — restart-orphan stuck-open sınıfını kapatır.
                self._active_alerts.pop(source, None)
                asyncio.create_task(self._resolve_alert_db_by_source(source, now))

    @property
    def latest_vps(self) -> dict[str, Any]:
        return self._latest_vps

    async def get_vps_metrics_history(self, minutes: int = 60) -> list[dict[str, Any]]:
        if not self._db:
            return []
        rows = await self._db.fetch_all(
            # Format-agnostik + expression index idx_vps_metrics_dt — bkz get_metrics_history.
            """SELECT * FROM vps_metrics_history
               WHERE datetime(timestamp) > datetime('now', ?)
               ORDER BY datetime(timestamp) DESC LIMIT 500""",
            (f"-{minutes} minutes",),
        )
        return [dict(r) for r in rows]
