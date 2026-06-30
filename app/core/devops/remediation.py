"""DevOpsAgent remediation mixin — split from monolithic devops_agent.py."""

from __future__ import annotations

import asyncio
import shlex
import time
from datetime import UTC, datetime

from app.core.devops.models import (
    _GOVERNOR_RE,
    _VALID_GOVERNOR,
    _VALID_UNIT,
    PLAYBOOKS,
    Alert,
    RemediationRecord,
)
from app.core.events import emit_event
from app.core.provenance import provenance_json


class RemediationMixin:
    """DevOpsAgent remediation mixin — split from monolithic devops_agent.py."""

    async def _remediate(self, alert: Alert) -> None:
        """Execute remediation playbook for an alert."""
        # Check cooldown — only blocks if we've actually remediated this source
        # before. The previous default of 0 broke on freshly-booted hosts where
        # time.monotonic() < cooldown_seconds.
        now = time.monotonic()
        last = self._cooldowns.get(alert.source)
        if last is not None and (now - last) < self._cooldown_seconds:
            return

        playbook_key = f"{alert.source}_critical"
        playbook = PLAYBOOKS.get(playbook_key, [])
        if not playbook:
            return

        self._cooldowns[alert.source] = now

        for step in playbook:
            await self._apply_remediation(alert, alert.source, step["desc"], step["cmd"])

        # Send webhook event (n8n) — mode dahil
        await self._send_webhook(alert)
        # FAZ5-S2: verify -> fail ise escalate (yalnız mode=auto)
        await self._verify_and_escalate(alert.source, alert)

    async def _apply_remediation(self, alert: Alert, source: str, action: str, command: str, timeout: int = 30) -> None:
        """Tek-nokta remediation adımı — TÜM yollar (playbook + servis + container)
        bunu kullanır (Codex P1: gate her yerde). mode-gate: 'auto' değilse YÜRÜTME
        YOK, sadece niyet kaydedilir (mevcut alert-notify escalate eder). in-memory
        log + kalıcı ledger + alert.remediation."""
        mode = self._remediation_mode
        executed = False
        success: bool | None = None
        if mode == "auto":
            # OPT-IN: gerçekten yürüt. Playbook'lar güvenlileştirildi (yıkıcı/geri-
            # alınamaz adımlar — prune --volumes / backup-silme — çıkarıldı); kalanlar
            # güvenli-reclaim + restart. FAZ5-S2: aksiyon sonrası _verify_and_escalate
            # verify eder (fail -> escalate). INTERV: reversible-komutta aksiyon-ÖNCESİ
            # geri-alma durumu yakalanır (verify-fail -> auto-rollback).
            await self._capture_rollback(source, command)
            executed = True
            try:
                result = await self._executor.execute(command, timeout=timeout)
                out = result.get("stdout", "")[:500]
                success = result.get("exit_code", 1) == 0
            except Exception as e:
                out = str(e)[:500]
                success = False
        else:
            out = f"skipped: remediation_mode={mode} (otonom yürütme kapalı)"
        self._remediation_log.append(
            RemediationRecord(
                timestamp=datetime.now(UTC).isoformat(),
                alert_source=source,
                action=action,
                command=command,
                result=out,
                success=bool(success),
            )
        )
        # executed -> verify_status NULL (S2 verify-edecek); değilse 'skipped'
        # (notify/dry_run satırları verify-UPDATE'inden ayrışsın). INTERV: her satıra
        # provenance (tetik-kökeni) iliştir — sonradan denetlenebilir.
        await self._persist_remediation_row(
            source,
            alert.severity,
            mode,
            action,
            command,
            executed,
            out,
            success,
            verify_status=None if executed else "skipped",
            provenance=provenance_json(alert, mode, detected_at=getattr(alert, "timestamp", None) or None),
        )
        alert.remediation = f"[{mode}] {action}"

    def _reversible_kind(self, command: str) -> str | None:
        """Komut GERİ-ALINABİLİR mi (DAR set). Şimdilik yalnız cpu-governor değişimi.
        prune/delete/truncate/restart -> None (geri-alınamaz, escalate-only)."""
        if _GOVERNOR_RE.search(command):
            return "governor"
        return None

    async def _capture_rollback(self, source: str, command: str) -> None:
        """Aksiyon-ÖNCESİ geri-alma durumunu yakala (yalnız reversible komut). governor:
        mevcut scaling_governor'ı oku, doğrula, sakla. Yakalanamazsa rollback olmaz (güvenli)."""
        if self._reversible_kind(command) != "governor":
            return
        try:
            cap = await self._executor.execute("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", timeout=10)
            prior = (cap.get("stdout", "") or "").strip().splitlines()
            prior_gov = prior[0].strip() if prior else ""
        except Exception:
            prior_gov = ""
        # GÜVENLİK: yalnız geçerli governor-adı sakla (komut-enjeksiyonu önle); aksi -> rollback yok.
        if prior_gov and _VALID_GOVERNOR.fullmatch(prior_gov):
            self._rollback_state[source] = {"kind": "governor", "state": prior_gov, "command": command}

    def _rollback_is_flapping(self, source: str) -> bool:
        """Anti-flapping: bu kaynak için son rollback _rollback_cooldown içinde mi (tekrar-tekrar
        geri-alma -> flapping). True -> rollback ATLA (doğrudan escalate)."""
        last = self._last_rollback.get(source)
        return last is not None and (time.monotonic() - last) < self._rollback_cooldown

    async def _attempt_rollback(self, source: str) -> tuple[bool, str]:
        """Yakalı reversible-state varsa geri-al. (rolled_back, rollback_result) döndür.
        Anti-flapping cooldown'da -> (False, 'skipped: flapping'). Yalnız mode=auto'dan çağrılır."""
        state = self._rollback_state.pop(source, None)
        if not state:
            return False, ""
        if self._rollback_is_flapping(source):
            return False, "skipped: flapping-cooldown"
        gov = state["state"]
        if not _VALID_GOVERNOR.fullmatch(gov):  # defense-in-depth (saklarken de doğrulandı)
            return False, "skipped: invalid-governor"
        q = shlex.quote(gov)
        cmd = f"cpufreq-set -g {q} 2>/dev/null || echo {q} | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null || true"
        # Codex P2: '|| true' + olası whitelist-eksikliği başarısızlığı maskeler → komut
        # exit_code'una GÜVENME. Rollback'i governor'ı RE-READ ederek DOĞRULA; gerçekten
        # geri dönmediyse rolled_back=False (gerçekleşmeyen rollback'i 'oldu' RAPORLAMA).
        try:
            await self._executor.execute(cmd, timeout=30)
            chk = await self._executor.execute("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", timeout=10)
            lines = (chk.get("stdout", "") or "").strip().splitlines()
            now_gov = lines[0].strip() if lines else ""
            ok = now_gov == gov
            res = f"governor={now_gov or '?'} (hedef {gov})"
        except Exception as e:
            ok = False
            res = f"rollback-error: {str(e)[:200]}"
        if ok:
            self._last_rollback[source] = time.monotonic()  # cooldown YALNIZ doğrulanmış rollback'te
            return True, res
        return False, f"rollback-DOĞRULANAMADI: {res}"

    async def _persist_remediation_row(
        self,
        source: str,
        severity: str,
        mode: str,
        action: str,
        command: str,
        executed: bool,
        result: str,
        success: bool | None,
        verify_status: str | None = None,
        provenance: str | None = None,
    ) -> None:
        """Kalıcı remediation ledger (server.db.remediation_log). Best-effort:
        DB yoksa/yazamazsa sessizce geç (remediation akışını bozma)."""
        if not self._db:
            return
        try:
            await self._db.execute(
                "INSERT INTO remediation_log "
                "(alert_source, severity, mode, action, command, executed, result, success, verify_status, provenance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source,
                    severity,
                    mode,
                    action,
                    command,
                    1 if executed else 0,
                    result,
                    None if success is None else (1 if success else 0),
                    verify_status,
                    provenance,
                ),
            )
        except Exception:
            pass

    def _executable_playbook(self, source: str) -> list[dict] | None:
        """source -> ÇALIŞTIRILABİLİR playbook adımları (template doldurulmuş).
        None = bu kaynak için manuel-tetiklenebilir düzeltme yok (örn cpu = sadece-
        inceleme). escalation:/remediation: önekleri asıl kaynağa indirgenir."""
        for pfx in ("escalation:", "remediation:"):
            if source.startswith(pfx):
                source = source[len(pfx) :]
                break
        base, _, name = source.partition(":")
        # GÜVENLİK (defense-in-depth, RCE-yüzeyi): service/docker adı shell-komutuna
        # gömülüyor. Kaynak iç-yazımlı olsa da, herhangi bir gelecekteki injection
        # yolunu kapat -> yalnız güvenli unit/container-ad karakterleri. Aksi -> aksiyon yok.
        # TEK-KAYNAK: _VALID_UNIT (otonom _remediate_service/_container ile aynı desen;
        # eski _SAFE_UNIT_NAME kopyası birleştirildi — kopya-drift önleme).
        if base in ("service", "docker") and name and not _VALID_UNIT.fullmatch(name):
            return None
        if base == "service" and name:
            return [{"desc": f"Restart {name}", "cmd": f"systemctl restart {name}"}]
        if base == "docker" and name:
            # restart: durmuş container'ı da başlatır, unhealthy'yi de düzeltir (Codex P2;
            # 'docker start' çalışan-unhealthy'de no-op'tu).
            return [{"desc": f"Restart {name}", "cmd": f"docker restart {name}"}]
        if base == "cpu":
            return None  # cpu_critical sadece-log -> çalıştırılacak düzeltme yok
        steps = PLAYBOOKS.get(f"{base}_critical")
        return steps or None

    def has_actionable_playbook(self, source: str) -> bool:
        """notify-cron + endpoint: bu kaynağa [🔧 Uygula] sunulabilir mi."""
        return self._executable_playbook(source) is not None

    async def force_remediate(self, source: str) -> dict:
        """Kullanıcı [🔧 Uygula] ile AÇIK ONAY verdi -> remediation_mode-gate BYPASS
        (insan-in-loop ayrı gate; notify-default'ta bile çalışır çünkü onay manuel).
        Playbook'u yürüt + verify + ledger(mode='manual'). verify-fail -> escalate.
        Owner-auth ÇAĞRAN katmanda (telegram owner-chat / endpoint internal-key)."""
        for pfx in ("escalation:", "remediation:"):
            if source.startswith(pfx):
                source = source[len(pfx) :]
                break
        steps = self._executable_playbook(source)
        if steps is None:
            return {"ok": True, "executed": False, "reason": "no_actionable_playbook", "source": source}

        results = []
        all_ok = True
        for step in steps:
            try:
                r = await self._executor.execute(step["cmd"], timeout=30)
                ok = r.get("exit_code", 1) == 0
                out = r.get("stdout", "")[:300]
            except Exception as e:
                ok = False
                out = str(e)[:300]
            all_ok = all_ok and ok
            results.append({"action": step["desc"], "success": ok})
            await self._persist_remediation_row(
                source,
                "critical",
                "manual",
                step["desc"],
                step["cmd"],
                executed=True,
                result=out,
                success=ok,
                verify_status=None,
            )

        # verify (kısa grace -> cleanup/restart etkisi otursun)
        if self._verify_grace:
            await asyncio.sleep(self._verify_grace)
        verified = await self._verify_remediation(source)
        status = "n/a" if verified is None else ("pass" if verified else "fail")
        if self._db:
            try:
                await self._db.execute(
                    "UPDATE remediation_log SET verify_status=? WHERE alert_source=? AND mode='manual' AND verify_status IS NULL",
                    (status, source),
                )
            except Exception:
                pass
        if status == "fail":
            # düzeltme çalıştı ama sorun sürüyor -> yeni unacked critical -> escalate.
            try:
                await asyncio.to_thread(
                    emit_event,
                    type="alert",
                    source=f"remediation:{source}",
                    title=f"Manuel remediation BAŞARISIZ: {source} hâlâ kritik — elle müdahale gerek",
                    severity="critical",
                    detail="Kullanıcı [🔧 Uygula] ile çalıştırdı ama verify başarısız.",
                )
            except Exception:
                pass
        return {
            "ok": True,
            "executed": True,
            "source": source,
            "steps": results,
            "all_success": all_ok,
            "verify": status,
        }

    async def _remediate_service(self, service: str, alert: Alert) -> None:
        now = time.monotonic()
        source = f"service:{service}"
        # GÜVENLİK: ad-doğrulama remediation'dan ÖNCE (f-string → tam-shell yolu;
        # _refuse_invalid_unit ledger'a görünür 'refused' satırı yazar, alarm akar).
        if not _VALID_UNIT.fullmatch(service):
            await self._refuse_invalid_unit(source, alert, "service", service)
            return
        # taze-boot bug fix (Codex-CI): get(source,0)+monotonic<cooldown erken-return
        # yapardi -> None-check (devops _remediate ile ayni).
        last = self._cooldowns.get(source)
        if last is not None and (now - last) < self._cooldown_seconds:
            return
        self._cooldowns[source] = now

        # mode-gate (Codex P1): notify/dry_run'da systemctl restart YÜRÜTÜLMEZ.
        # shlex.quote = savunma-derinliği (doğrulama geçse bile meta-karakter etkisiz).
        await self._apply_remediation(alert, source, f"Restart {service}", f"systemctl restart {shlex.quote(service)}", timeout=15)
        await self._send_webhook(alert)
        await self._verify_and_escalate(source, alert)

    async def _refuse_invalid_unit(self, source: str, alert: Alert, kind: str, name: str) -> None:
        """Geçersiz servis/konteyner adı → remediation REFUSED (yürütme yok), ama
        SESSİZ DEĞİL: ledger'a refused satırı + webhook (görünürlük korunur).
        Ad config'ten gelir; geçersiz ad = config bozuk/oynanmış → incelenmeli."""
        msg = f"refused: gecersiz {kind} adi ({name[:60]!r}) — komut-enjeksiyonu riski, yurutme yok"
        self._remediation_log.append(
            RemediationRecord(
                timestamp=datetime.now(UTC).isoformat(),
                alert_source=source,
                action=f"Restart {kind} REFUSED",
                command="(yurutulmedi)",
                result=msg,
                success=False,
            )
        )
        await self._persist_remediation_row(
            source,
            alert.severity,
            self._remediation_mode,
            f"Restart {kind} REFUSED",
            "(yurutulmedi)",
            False,
            msg,
            False,
            verify_status="refused",
            provenance=provenance_json(alert, self._remediation_mode, detected_at=getattr(alert, "timestamp", None) or None),
        )
        alert.remediation = f"[refused] {msg}"
        await self._send_webhook(alert)

    async def _remediate_container(self, container: str, alert: Alert) -> None:
        now = time.monotonic()
        source = f"docker:{container}"
        # GÜVENLİK: ad-doğrulama (servis yoluyla simetrik — f-string → tam-shell).
        if not _VALID_UNIT.fullmatch(container):
            await self._refuse_invalid_unit(source, alert, "container", container)
            return
        # taze-boot bug fix (Codex-CI): get(source,0)+monotonic<cooldown erken-return
        # yapardi -> None-check (devops _remediate ile ayni).
        last = self._cooldowns.get(source)
        if last is not None and (now - last) < self._cooldown_seconds:
            return
        self._cooldowns[source] = now

        # mode-gate (Codex P1): notify/dry_run'da YÜRÜTÜLMEZ. restart: durmuş+unhealthy
        # ikisini de kapsar (Codex P2). shlex.quote = savunma-derinliği.
        await self._apply_remediation(alert, source, f"Restart {container}", f"docker restart {shlex.quote(container)}", timeout=15)
        await self._send_webhook(alert)
        await self._verify_and_escalate(source, alert)

    @property
    def remediation_history(self) -> list[dict]:
        return [
            {
                "timestamp": r.timestamp,
                "alert_source": r.alert_source,
                "action": r.action,
                "command": r.command,
                "result": r.result,
                "success": r.success,
            }
            for r in reversed(self._remediation_log)
        ]

    @property
    def playbooks(self) -> dict:
        return {k: [s["desc"] for s in v] for k, v in PLAYBOOKS.items()}
