"""Agent Presence Manager — kimlik + heartbeat + lease.

Her sürekli ajan start()'ta kendini `agent_instances` tablosuna kaydeder;
döngü 15s'de bir heartbeat ile lease yeniler. lease_until geçince ajan offline
sayılır. Böylece "kim hayatta?" sorusu tek tablodan cevaplanır.

DB erişimi kanonik katman üzerinden (get_conn + server_db_path) — busy_timeout
ve WAL garantili. Yalnız leader-worker buraya yazmalı (ajanlar orada yaşar);
standby worker'ın eski kayıtları lease-expire ile offline'a düşer.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.db.data_layer import get_conn, server_db_path

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 15
LEASE_TTL = 45

# ============================================================================
# KAPALI — 2026-09-03, kontrollu deney (server.db bozulmasi #10).
#
# Bu modul 2026-08-27'de commit EDILMEDEN production'a girdi ve o tarihten beri
# server.db 15 saniyede bir (her iki worker'dan) yaziliyor. Bozulma sikligi ayni
# tarihte ikiye katlandi: oncesi ~haftada bir (07-15, 07-21, 07-28, 08-09,
# 08-15), sonrasi 08-28 / 08-31 / 09-03 — 7 gunde 3.
#
# Son iki bozulmanin TEK ortak paydasi bu kod: 08-31'de bozulma backup'tan SONRA
# ve restart YOKKEN, 09-03'te backup'tan ONCE ve restart'tan 11 sn sonra olustu
# — yani ne backup ne restart ortak. Kesif #1645 de ayni yeri isaret ediyor
# ("startup WAL dalgasi ... presence").
#
# NEDENSELLIK KANITLANMADI. Bu bir deneydir: yazma yolu kapatilir, bir hafta
# bozulma olmazsa sebep buradadir. Okuma yollari (list_instances/list_alive)
# ACIK birakildi — yalnizca YAZMA durduruldu.
#
# Geri acmak icin: PRESENCE_WRITES_ENABLED = True.
# ============================================================================
PRESENCE_WRITES_ENABLED = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT UNIQUE,
    instance_id TEXT,
    agent_type TEXT,
    host TEXT,
    device TEXT,
    capabilities TEXT,
    status TEXT DEFAULT 'offline',
    current_task TEXT,
    current_project TEXT,
    started_at REAL,
    last_heartbeat REAL,
    lease_until REAL,
    last_event_id INTEGER DEFAULT 0,
    model TEXT,
    version TEXT,
    pid INTEGER,
    leader_epoch INTEGER,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_instances_lease ON agent_instances(lease_until);
"""

_schema_ready = False


def ensure_schema() -> None:
    global _schema_ready
    if not PRESENCE_WRITES_ENABLED:
        return
    if _schema_ready:
        return
    try:
        conn = get_conn(server_db_path())
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _schema_ready = True
    except Exception as e:
        logger.warning("agent_instances schema ensure failed: %s", e)


def _load_json(raw: Any) -> Any:
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw or {}


class AgentPresenceManager:
    def upsert(
        self,
        agent_id: str,
        instance_id: str,
        agent_type: str,
        host: str,
        device: str,
        capabilities: dict[str, Any],
        model: str = "",
        version: str = "",
        pid: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Ajanı kaydet (restart = yeniden kayıt → status idle'a döner)."""
        if not PRESENCE_WRITES_ENABLED:
            return
        ensure_schema()
        now = time.time()
        try:
            conn = get_conn(server_db_path())
            try:
                conn.execute(
                    """INSERT INTO agent_instances
                       (agent_id, instance_id, agent_type, host, device, capabilities,
                        status, started_at, last_heartbeat, lease_until, model, version, pid, metadata)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(agent_id) DO UPDATE SET
                         instance_id=excluded.instance_id,
                         agent_type=excluded.agent_type,
                         host=excluded.host,
                         device=excluded.device,
                         capabilities=excluded.capabilities,
                         status='idle',
                         last_heartbeat=excluded.last_heartbeat,
                         lease_until=excluded.lease_until,
                         model=excluded.model,
                         version=excluded.version,
                         pid=excluded.pid,
                         metadata=excluded.metadata""",
                    (
                        agent_id,
                        instance_id,
                        agent_type,
                        host,
                        device,
                        json.dumps(capabilities, default=str),
                        "idle",
                        now,
                        now,
                        now + LEASE_TTL,
                        model,
                        version,
                        pid,
                        json.dumps(metadata or {}, default=str),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("presence upsert failed (%s): %s", agent_id, e)

    def heartbeat(
        self,
        agent_id: str,
        status: str | None = None,
        current_task: str | None = None,
        current_project: str | None = None,
        last_event_id: int | None = None,
    ) -> None:
        """Lease yenile. None alanlar mevcut değeri korur (durum gerilemesi yok)."""
        if not PRESENCE_WRITES_ENABLED:
            return
        ensure_schema()
        now = time.time()
        try:
            conn = get_conn(server_db_path())
            try:
                conn.execute(
                    """UPDATE agent_instances
                       SET last_heartbeat=?, lease_until=?,
                           status=CASE WHEN ? IS NOT NULL THEN ? WHEN status='offline' THEN 'idle' ELSE status END,
                           current_task=COALESCE(?, current_task),
                           current_project=COALESCE(?, current_project),
                           last_event_id=COALESCE(?, last_event_id)
                       WHERE agent_id=?""",
                    (now, now + LEASE_TTL, status, status, current_task, current_project, last_event_id, agent_id),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("presence heartbeat failed (%s): %s", agent_id, e)

    def expire_leases(self) -> None:
        if not PRESENCE_WRITES_ENABLED:
            return
        ensure_schema()
        now = time.time()
        try:
            conn = get_conn(server_db_path())
            try:
                conn.execute(
                    "UPDATE agent_instances SET status='offline' WHERE lease_until < ? AND status NOT IN ('offline','stopping')",
                    (now,),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("presence expire failed: %s", e)

    def mark_stopping(self, agent_id: str) -> None:
        self.heartbeat(agent_id, status="stopping")

    def list_instances(self) -> list[dict[str, Any]]:
        ensure_schema()
        try:
            conn = get_conn(server_db_path(), readonly=True)
            try:
                rows = conn.execute("SELECT * FROM agent_instances ORDER BY agent_type").fetchall()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("presence list failed: %s", e)
            return []
        now = time.time()
        out = []
        for r in rows:
            d = dict(r)
            d["capabilities"] = _load_json(d.get("capabilities"))
            d["metadata"] = _load_json(d.get("metadata"))
            d["alive"] = bool(d.get("lease_until")) and d["lease_until"] >= now
            out.append(d)
        return out

    def list_alive(self) -> list[dict[str, Any]]:
        return [d for d in self.list_instances() if d["alive"]]

    def who_is_working_on(self, project: str) -> list[dict[str, Any]]:
        return [d for d in self.list_alive() if d.get("current_project") == project and d.get("status") in ("working", "idle")]


presence = AgentPresenceManager()
