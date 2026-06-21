"""CodeReviewAgent — sürekli read-only kod-inceleme worker'ı (lifespan background).

İki tetik: (a) commit-kuyruğu drenajı (event, git post-commit hook doldurur),
(b) idle-sweep (CPU düşükken rotating, tüm codebase'i zamanla kapsar). Periyodik
'learning' sentezi. P1 bulgu → emit_event (notify-cron Telegram). KOD DEĞİŞTİRMEZ.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import psutil

from app.core import code_reviewer as cr
from app.core.config import read_env_var
from app.core.events import emit_event

logger = logging.getLogger(__name__)


class CodeReviewAgent:
    def __init__(self, interval: int = 300) -> None:
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._k = int(read_env_var("CODE_REVIEW_SWEEP_K") or "3")
        self._idle_cpu = float(read_env_var("CODE_REVIEW_IDLE_CPU") or "40")
        self._queue = cr.ROOT / "data" / "code-review-queue.txt"
        self._sweep_files: list[Path] = []
        self._pos = 0
        self._research_pos = 0
        self._ticks = 0
        self.last_run: str | None = None
        self.total_findings = 0
        # Action/Provider deseni: yetenekler (review/learn/research) registry'den dispatch.
        from app.core.agents.code_actions import build_code_review_registry

        self._registry = build_code_review_registry()

    def start(self) -> None:
        if cr._ENABLED:
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def status(self) -> dict:
        # Display GERÇEK route'u yansıtsın (cr._MODEL sabiti DEĞİL) — LLM_ROUTE_* override'ları
        # tarama=Haiku / kontrol+sentez=Sonnet'i gösterir.
        from app.core.agents.llmcore import llm_core

        return {
            "enabled": cr._ENABLED,
            "model": llm_core.route("code-review")[1],  # tarama (LLM_ROUTE_CODE_REVIEW)
            "verify_model": llm_core.route("verify")[1],  # bulgu-kontrol (LLM_ROUTE_VERIFY)
            "synthesis_model": llm_core.route("synthesis")[1],  # research sentezi
            "interval_s": self._interval,
            "sweep_k": self._k,
            "idle_cpu_threshold": self._idle_cpu,
            "ticks": self._ticks,
            "last_run": self.last_run,
            "total_findings": self.total_findings,
        }

    async def _run_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._tick()
            except Exception:
                logger.exception("code-review tick failed")

    async def _tick(self) -> None:
        if not cr._ENABLED:
            return
        from datetime import UTC, datetime

        self.last_run = datetime.now(UTC).isoformat()
        await self._drain_queue()  # event: commit-trigger (her zaman)
        if await self._is_idle():  # idle-trigger (sadece boştayken)
            await self._sweep()
        self._ticks += 1
        if self._ticks % 12 == 0:  # ~her saat (12×5dk) ders sentezle (Action)
            await self._registry.run("learn")
        # Faz 3: internet/yeni-yapı — ~her 4h (48×5dk) sıradaki stack-topic'i araştır
        # (rotating, bounded; tüm topic'ler ~1.3 günde kapsanır). Yalnız idle'da.
        if cr._RESEARCH_ENABLED and self._ticks % 48 == 0 and await self._is_idle():
            topic = cr.STACK_TOPICS[self._research_pos % len(cr.STACK_TOPICS)]
            self._research_pos += 1
            await self._registry.run("research", topic=topic)

    async def _is_idle(self) -> bool:
        try:
            return (await asyncio.to_thread(psutil.cpu_percent, 1.0)) < self._idle_cpu
        except Exception:
            return False

    async def _drain_queue(self) -> None:
        """commit-hook'un yazdığı değişen-dosyaları incele, kuyruğu temizle."""
        try:
            if not self._queue.exists():
                return
            lines = [ln.strip() for ln in self._queue.read_text().splitlines() if ln.strip()]
            self._queue.write_text("")
        except Exception:
            return
        files = [rel for rel in dict.fromkeys(lines) if (cr.ROOT / rel).is_file()]  # uniq + sıra-koru
        if not files:
            return
        before = self.total_findings
        for rel in files:
            await self._review_one(cr.ROOT / rel, "commit")
        # Heartbeat (ajan-feed): GERÇEK bir inceleme oldu → temiz mi bulgu mu, ne zaman.
        # "sorun yok dedi haberim olmalı" — temiz-verdict'in TEK kalıcı izi (early-return iz bırakmaz).
        self._write_heartbeat("commit", len(files), self.total_findings - before)

    def _write_heartbeat(self, trigger: str, files: int, findings: int) -> None:
        """data/hook-state/last-code-review.json — ajan-feed'in Haiku-canlılık + verdict kaynağı.
        FAIL-SAFE: yazım hatası incelemeyi bozmaz."""
        try:
            import json
            from datetime import UTC, datetime

            try:
                model = self.status().get("model")  # route hatası heartbeat'i KAYBETMEMELİ
            except Exception:
                model = None
            hb = cr.ROOT / "data" / "hook-state" / "last-code-review.json"
            hb.parent.mkdir(parents=True, exist_ok=True)
            hb.write_text(
                json.dumps(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "trigger": trigger,
                        "files": files,
                        "findings": findings,
                        "clean": findings == 0,
                        "model": model,
                    }
                )
            )
        except Exception:
            logger.debug("heartbeat write failed", exc_info=True)

    async def _sweep(self) -> None:
        if not self._sweep_files:
            self._sweep_files = self._collect_files()
            self._pos = 0
        if not self._sweep_files:
            return
        for _ in range(self._k):
            p = self._sweep_files[self._pos % len(self._sweep_files)]
            self._pos += 1
            await self._review_one(p, "sweep")

    def _collect_files(self) -> list[Path]:
        out: list[Path] = []
        for d in ("app", "automation", "scripts"):
            base = cr.ROOT / d
            if base.is_dir():
                out += sorted(base.rglob("*.py"))
                out += sorted(base.rglob("*.sh"))
        return [p for p in out if "__pycache__" not in str(p) and "/venv/" not in str(p)]

    async def _review_one(self, abs_path: Path, source: str) -> None:
        # 'review' Action: incele + bulguları dedup'lı kaydet (registry dispatch).
        res = await self._registry.run("review", path=abs_path)
        if not res or not res.get("new"):
            return
        rel = res.get("rel", abs_path.name)
        self.total_findings += res["new"]
        if res["p1_titles"]:
            # P1 → emit_event (teşhis-asistanı deseni; notify-cron Telegram'a çevirir)
            await asyncio.to_thread(
                emit_event,
                type="alert",
                source=f"code-review:{rel}",
                title=f"🔬 Kod-review P1 ({source}): {res['p1_titles'][0][:120]}",
                severity="warning",
                detail=(
                    f"Read-only kod-review ajanı ({self.status().get('model', 'Haiku')}) bulgusu — "
                    "discoveries'e yazıldı, DOĞRULA.\n" + "\n".join(res["p1_titles"])
                ),
            )
