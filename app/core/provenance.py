"""Remediation provenance — müdahale kökeni/izi (LIVESYS-INTERV).

Her otonom remediation için "neyi, neden, kimin tetiklediği" iz kaydı üretir.
remediation_log.provenance kolonuna JSON olarak yazılır → sonradan denetlenebilir
(hangi alert → hangi aksiyon → rollback oldu mu zincirini açıkça gösterir).

SAF + yan-etkisiz (datetime hariç) → kolay test edilebilir.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def build_provenance(alert: Any, mode: str, detected_at: str | None = None) -> dict[str, Any]:
    """Bir alert+mode'dan provenance sözlüğü üret. detected_at verilmezse şimdi (UTC ISO).

    Auditor-dostu sabit anahtarlar: trigger_source, severity, reason, agent, mode, detected_at.
    """
    return {
        "trigger_source": getattr(alert, "source", "unknown") or "unknown",
        "severity": getattr(alert, "severity", "unknown") or "unknown",
        "reason": (getattr(alert, "message", "") or "")[:200],
        "agent": "devops_agent",
        "mode": mode,
        "detected_at": detected_at or datetime.now(UTC).isoformat(),
    }


def provenance_json(alert: Any, mode: str, detected_at: str | None = None) -> str:
    """build_provenance → kompakt JSON string (remediation_log.provenance için)."""
    return json.dumps(build_provenance(alert, mode, detected_at), ensure_ascii=False)
