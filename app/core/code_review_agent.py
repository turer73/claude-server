"""CodeReviewAgent — sürekli read-only kod-inceleme worker'ı (lifespan background).

İki tetik: (a) commit-kuyruğu drenajı (event, git post-commit hook doldurur),
(b) idle-sweep (CPU düşükken rotating, tüm codebase'i zamanla kapsar). Periyodik
'learning' sentezi. P1 bulgu → emit_event (notify-cron Telegram). KOD DEĞİŞTİRMEZ.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Literal

import psutil

from app.core import code_reviewer as cr
from app.core.config import read_env_var
from app.core.events import emit_event

logger = logging.getLogger(__name__)


class CodeReviewAgent:
    def __init__(self, interval: int = 300, *, state_dir: str | Path | None = None) -> None:
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self._manual_task: asyncio.Task[None] | None = None
        self._run_lock = asyncio.Lock()
        self._k = int(read_env_var("CODE_REVIEW_SWEEP_K") or "3")
        self._idle_cpu = float(read_env_var("CODE_REVIEW_IDLE_CPU") or "40")
        self._queue = cr.ROOT / "data" / "code-review-queue.txt"
        manual_state_dir = Path(state_dir or read_env_var("CODE_REVIEW_STATE_DIR") or "/var/lib/linux-ai-server")
        self._manual_request = manual_state_dir / "code-review-manual.request"
        self._manual_running_request = manual_state_dir / "code-review-manual.running"
        self._manual_poll_interval = 1.0
        self._manual_running = False
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
        if not cr._ENABLED:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())
        if self._manual_task is None or self._manual_task.done():
            self._manual_task = asyncio.create_task(self._manual_request_loop())

    async def stop(self) -> None:
        tasks = [task for task in (self._task, self._manual_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._manual_task = None

    def status(self) -> dict[str, Any]:
        # Display GERÇEK route'u yansıtsın (cr._MODEL sabiti DEĞİL) — LLM_ROUTE_* override'ları
        # tarama=Haiku / kontrol+sentez=Sonnet'i gösterir.
        from app.core.agents.llmcore import llm_core

        running = self._task is not None and not self._task.done()
        manual_worker_running = self._manual_task is not None and not self._manual_task.done()
        return {
            "enabled": cr._ENABLED,
            "running": running and manual_worker_running,
            "manual_review_pending": (self._manual_running or self._manual_request.exists() or self._manual_running_request.exists()),
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
        async with self._run_lock:
            await self._run_tick()

    async def _run_tick(self) -> None:
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

    async def run_now(self) -> None:
        """Run a manual queue drain + sweep without overlapping the periodic tick."""
        if not cr._ENABLED:
            return
        async with self._run_lock:
            from datetime import UTC, datetime

            self.last_run = datetime.now(UTC).isoformat()
            await self._drain_queue()
            await self._sweep()

    def request_manual_run(self) -> Literal["queued", "already_queued", "disabled", "error"]:
        """Persist one cross-worker manual request; duplicate requests coalesce."""
        if not cr._ENABLED:
            return "disabled"
        try:
            self._manual_request.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self._manual_request, flags, 0o600)
        except FileExistsError:
            return "already_queued"
        except OSError:
            logger.exception("manual code-review request could not be persisted")
            return "error"

        # O_EXCL creation is the durable publication point. Marker content is
        # deliberately empty: the leader may rename it immediately, and cleanup
        # by pathname after that point could delete a newer producer's request.
        try:
            try:
                os.set_inheritable(fd, False)
            except OSError:
                logger.warning("manual code-review request fd could not be marked non-inheritable", exc_info=True)
        finally:
            try:
                os.close(fd)
            except OSError:
                logger.warning("manual code-review request fd close failed", exc_info=True)
        return "queued"

    async def _manual_request_loop(self) -> None:
        """Leader-only consumer for the shared, crash-recoverable request file."""
        while True:
            try:
                if not await asyncio.to_thread(self._claim_manual_request):
                    await asyncio.sleep(self._manual_poll_interval)
                    continue
                self._manual_running = True
                try:
                    await self.run_now()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("manual code-review failed; durable request retained")
                    await asyncio.sleep(max(1.0, self._manual_poll_interval))
                    continue
                await asyncio.to_thread(self._complete_manual_request)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("manual code-review request loop failed")
                await asyncio.sleep(max(1.0, self._manual_poll_interval))
            finally:
                self._manual_running = False

    def _claim_manual_request(self) -> bool:
        """Atomically move queued work to a crash-recoverable running marker."""
        if self._manual_running_request.exists():
            return True
        try:
            os.replace(self._manual_request, self._manual_running_request)
            return True
        except FileNotFoundError:
            return False

    def _complete_manual_request(self) -> None:
        """Acknowledge only the claimed run; a newer queued request stays intact."""
        self._manual_running_request.unlink(missing_ok=True)

    async def _is_idle(self) -> bool:
        try:
            return (await asyncio.to_thread(psutil.cpu_percent, 1.0)) < self._idle_cpu
        except Exception:
            return False

    async def _drain_queue(self) -> None:
        """commit-hook'un yazdığı değişen-dosyaları incele, kuyruğu temizle."""
        # discovery #1132: eski read_text()→write_text("") arası TOCTOU — bu pencerede
        # commit-hook'un (AYRI process) append'i truncate ile KAYBOLURDU. Atomic os.replace
        # ile pencere yapısal olarak kapanır: rename'den ÖNCEKİ append .draining'e düşer
        # (işlenir), SONRAKİ yeni queue dosyası oluşturur (sonraki tick'te yakalanır).
        # POSIX rename atomic → kayıp yok. .draining leftover (önceki drain çöktüyse) bu
        # turda işlenir (crash-recovery).
        drain_path = self._queue.with_suffix(".draining")
        try:
            if self._queue.exists():
                os.replace(self._queue, drain_path)  # atomic: queue → draining
            if not drain_path.exists():
                return
            lines = [ln.strip() for ln in drain_path.read_text().splitlines() if ln.strip()]
            drain_path.unlink()
        except Exception:
            return
        files = [rel for rel in dict.fromkeys(lines) if (cr.ROOT / rel).is_file()]  # uniq + sıra-koru
        if not files:
            return
        before = self.total_findings
        for rel in files:
            # discovery #1128 (Haiku-self-review P1): tek dosyanın _review_one hatası (emit_event/
            # status fırlarsa) for-loop'u KIRMAMALI → kalan dosyalar + heartbeat atlanır. Per-dosya izole.
            try:
                await self._review_one(cr.ROOT / rel, "commit")
            except Exception:
                logger.exception("review_one failed for %s (drain devam ediyor)", rel)
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
            # discovery #1130: tek dosyanın _review_one hatası (emit_event/registry fırlarsa)
            # sweep loop'unu KIRMAMALI → kalan dosyalar atlanır. Per-dosya izole
            # (_drain_queue'daki #1128 fix'iyle aynı simetri).
            try:
                await self._review_one(p, "sweep")
            except Exception:
                logger.exception("review_one failed for %s (sweep devam ediyor)", p)

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
            try:
                model = self.status().get("model", "Haiku")  # route-lookup emit-path'ini KIRMAMALI
            except Exception:
                model = "Haiku"
            # P1 → emit_event (teşhis-asistanı deseni; notify-cron Telegram'a çevirir)
            await asyncio.to_thread(
                emit_event,
                type="alert",
                source=f"code-review:{rel}",
                title=f"🔬 Kod-review P1 ({source}): {res['p1_titles'][0][:120]}",
                severity="warning",
                detail=(f"Read-only kod-review ajanı ({model}) bulgusu — discoveries'e yazıldı, DOĞRULA.\n" + "\n".join(res["p1_titles"])),
            )
