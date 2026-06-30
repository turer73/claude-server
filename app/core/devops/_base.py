"""Type-checking base for the DevOpsAgent mixins.

The mixins share instance state assigned in ``DevOpsAgent.__init__`` and call one
another's methods across modules. This base declares that shared contract so
``mypy --strict`` can check each mixin file in isolation. It carries **no runtime
behavior** — the real attributes come from ``DevOpsAgent.__init__`` and the real
methods win via the mixin MRO (each concrete mixin precedes this base)."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from app.core.devops.models import Alert, RemediationRecord


class _DevOpsAgentBase:
    # ── Shared state (set in DevOpsAgent.__init__) ──────────────────────
    _db: Any
    _interval: int
    _monitor: Any
    _executor: Any
    _running: bool
    _task: asyncio.Task[None] | None
    _started_at: str | None
    _last_check: str | None
    _check_count: int
    _thresholds: dict[str, Any]
    _critical_services: list[str]
    _critical_containers: list[str]
    _vps_containers: list[str]
    _vps_host: str
    _latest_vps: dict[str, Any]
    _history: deque[dict[str, Any]]
    _active_alerts: dict[str, Alert]
    _remediation_mode: str
    _verify_grace: int
    _remediation_log: deque[RemediationRecord]
    _cooldowns: dict[str, float]
    _cooldown_seconds: int
    _rollback_state: dict[str, dict[str, Any]]
    _last_rollback: dict[str, float]
    _rollback_cooldown: int
    _last_escalation: dict[str, float]
    _escalation_interval: int
    _diagnostic_enabled: bool
    _diag_model: str
    _diag_timeout: int
    _diag_memory_db: str
    _diagnosed: set[str]
    _vps_probe_fails: int
    _vps_fail_threshold: int

    # ── Cross-mixin method contracts (real impls live in sibling mixins) ─
    async def _attempt_rollback(self, source: str) -> tuple[bool, str]:
        raise NotImplementedError

    async def _verify_remediation(self, source: str) -> bool | None:
        raise NotImplementedError

    async def _verify_and_escalate(self, source: str, alert: Alert) -> None:
        raise NotImplementedError

    async def _send_webhook(self, alert: Alert) -> None:
        raise NotImplementedError

    async def _remediate_service(self, service: str, alert: Alert) -> None:
        raise NotImplementedError

    async def _remediate_container(self, container: str, alert: Alert) -> None:
        raise NotImplementedError
