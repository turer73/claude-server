"""Somut Provider'lar (read-only context). devops-teşhis + code-research paylaşır."""

from __future__ import annotations

import asyncio
import sqlite3

import psutil

MEMORY_DB = "/opt/linux-ai-server/data/claude_memory.db"


class RecentChangesProvider:
    """Son N günde memory'ye kaydedilen değişiklikler (fix/architecture/workaround +
    task). Alert-korelasyonu (devops teşhis) ve research-context için ortak. Salt SELECT.

    NOT: devops_agent._gather_diag_context'in çıkarılmış hâli — iki ajan tek kaynaktan
    besleniyor (duplikasyon biter; Action/Provider deseninin asıl kazancı).
    """

    name = "recent_changes"

    def __init__(self, db_path: str = MEMORY_DB, days: int = 7) -> None:
        self._db = db_path
        self._days = days

    def _query(self) -> str:
        try:
            conn = sqlite3.connect(self._db)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=3000")
            disc = conn.execute(
                "SELECT project, type, title, date(created_at) d FROM discoveries "
                "WHERE type IN ('fix','architecture','workaround') AND created_at > datetime('now', ?) "
                "ORDER BY created_at DESC LIMIT 10",
                (f"-{self._days} days",),
            ).fetchall()
            tasks = conn.execute(
                "SELECT project, task, date(created_at) d FROM tasks_log "
                "WHERE created_at > datetime('now', ?) ORDER BY created_at DESC LIMIT 10",
                (f"-{self._days} days",),
            ).fetchall()
            conn.close()
            lines = [f"- [{r['d']}] {r['project']}/{r['type']}: {r['title']}" for r in disc]
            lines += [f"- [{r['d']}] {r['project']} task: {r['task']}" for r in tasks]
            return "\n".join(lines) if lines else f"Son {self._days} günde kayıtlı değişiklik yok."
        except Exception:
            return "(context okunamadı)"

    async def provide(self, **kwargs) -> str:
        return await asyncio.to_thread(self._query)


class MetricsProvider:
    """Anlık sistem metriği (CPU/RAM/disk) — read-only context."""

    name = "system_metrics"

    def _query(self) -> str:
        try:
            cpu = psutil.cpu_percent(interval=0.5)
            mem = psutil.virtual_memory().percent
            disk = psutil.disk_usage("/").percent
            return f"CPU %{cpu:.0f} | RAM %{mem:.0f} | Disk %{disk:.0f}"
        except Exception:
            return "(metrik okunamadı)"

    async def provide(self, **kwargs) -> str:
        return await asyncio.to_thread(self._query)
