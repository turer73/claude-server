"""FastAPI application factory and server entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBearer
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

from app import __version__
from app.api.admin import router as admin_router
from app.api.agents import router as agents_router
from app.api.ai import router as ai_router
from app.api.auth import router as auth_router
from app.api.backup import router as backup_router
from app.api.ci import router as ci_router
from app.api.classifier import router as classifier_router
from app.api.claude_code import router as claude_code_router
from app.api.csp import router as csp_router
from app.api.deploy import router as deploy_router
from app.api.dev import router as dev_router
from app.api.devops import router as devops_router
from app.api.digest import router as digest_router
from app.api.dispatch import router as dispatch_router
from app.api.files import router as files_router
from app.api.kernel import router as kernel_router
from app.api.llm import router as llm_router
from app.api.logs import router as logs_router
from app.api.memory import public_router as memory_public_router
from app.api.memory import router as memory_router
from app.api.monitoring import router as monitoring_router
from app.api.n8n import router as n8n_router
from app.api.network import router as network_router
from app.api.projects import router as projects_router
from app.api.prometheus import router as prometheus_router
from app.api.rag import router as rag_router
from app.api.research import router as research_router
from app.api.security import router as security_router
from app.api.shell import router as shell_router
from app.api.social import router as social_router
from app.api.ssh import router as ssh_router
from app.api.system import router as system_router
from app.api.telegram_bot import router as telegram_bot_router
from app.api.validation import router as validation_router
from app.api.vps import router as vps_router
from app.api.webops import router as webops_router
from app.api.ws_status import router as ws_status_router
from app.exceptions import ServerError
from app.middleware.audit_log import AuditMiddleware
from app.middleware.rate_limit import GlobalRateLimitMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.ws.logs import router as ws_logs_router
from app.ws.monitor import router as ws_monitor_router
from app.ws.terminal import router as ws_terminal_router

security_scheme = HTTPBearer()


# ── Deploy-SHA görünürlüğü (P0-a, surer): merged≠deployed + deployed≠running körlüğünü kapat ──
# _DEPLOYED_SHA = import-anında SABİT = ÇALIŞAN kodun SHA'sı. _current_disk_sha = disk-HEAD
# (pull sonrası değişir). İkisi farklıysa servis ESKİ kod çalıştırıyor (restart gerekli) =
# 'deployed≠running' drift (bu oturumda cosession-drift olarak yaşandı). 30sn cache.
def _read_deployed_sha() -> str:
    import os
    import subprocess

    try:
        return (
            subprocess.check_output(
                ["git", "-C", str(Path(__file__).parent.parent), "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            .decode()
            .strip()[:12]
        )
    except Exception:
        # Codex P2: installer-kurulumda (.git YOK) git patlar. Build/deploy-zamanı SHA
        # env-var'ı (DEPLOYED_SHA) fallback → installer-install'da da sinyal verilebilir.
        return (os.environ.get("DEPLOYED_SHA") or "").strip()[:12]


_DEPLOYED_SHA: str = _read_deployed_sha()
_disk_sha_cache: dict = {"sha": "", "ts": 0.0}


def _current_disk_sha() -> str:
    import time as _t

    now = _t.monotonic()
    if _disk_sha_cache["sha"] and now - _disk_sha_cache["ts"] < 30:
        return _disk_sha_cache["sha"]
    _disk_sha_cache["sha"] = _read_deployed_sha()
    _disk_sha_cache["ts"] = now
    return _disk_sha_cache["sha"]


# Boot dead-gate discovery emit'leri fire-and-forget (klipper #100091): up-ama-yavas
# servis edge'inde await-emit boot'u bloklayabilir. Task-ref'leri GC'den koru.
_boot_emit_tasks: set[asyncio.Task[None]] = set()


async def _emit_dead_gate_discovery(name: str, reader: str) -> None:
    """Dead-gate -> discovery (Q3, type=bug, dedup'li). Best-effort; hata yutulur."""
    try:
        from app.api.memory import DiscoveryCreate
        from app.api.memory.discoveries import create_discovery

        await create_discovery(
            DiscoveryCreate(
                project="claude-server",
                type="bug",
                title=f"[DEAD-GATE] {name} serviste no-op (.env okunmuyor)",
                details=(
                    f"{name} `.env`'de tanimli ama systemd process-env'e gecirmiyor; "
                    f"reader {reader} os.environ.get kullaniyor -> gate serviste sessizce "
                    f"olu. Fix: read_env_var('{name}'). #3 silent-fail-verify boot-config-log."
                ),
                rationale="boot-config-log runtime dead-gate detection",
            )
        )
    except Exception:
        logger.exception("[DEAD-GATE] discovery emit basarisiz (warn dustu)")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    import os

    from app.auth.api_key import generate_api_key, hash_api_key
    from app.db.database import DEFAULT_DB_PATH, Database

    db_path = os.environ.get("DB_PATH", DEFAULT_DB_PATH)
    db = Database(db_path)
    await db.initialize()

    # Create default admin key if no keys exist
    existing = await db.fetch_all("SELECT id FROM api_keys LIMIT 1")
    if not existing:
        default_key = os.environ.get("DEFAULT_API_KEY", generate_api_key())
        await db.execute(
            "INSERT INTO api_keys (key_hash, name, permissions) VALUES (?, ?, ?)",
            (hash_api_key(default_key), "admin", "admin"),
        )

    app.state.db = db

    # Start DevOps Agent daemon
    from app.core.devops_agent import DevOpsAgent

    devops = DevOpsAgent(db=db, interval=30)
    app.state.devops_agent = devops
    devops.start()

    # Read-only kod-mühendisi ajanı (qwen2.5-coder): commit-diff + idle-sweep ile
    # sürekli inceleme → discoveries (dedup'lı) + P1 Telegram. KOD DEĞİŞTİRMEZ.
    # CODE_REVIEW_ENABLED=0 ile kapatılır. start() yalnız enabled ise task açar.
    from app.core.code_review_agent import CodeReviewAgent

    code_reviewer = CodeReviewAgent(interval=300)
    app.state.code_review_agent = code_reviewer
    code_reviewer.start()

    # Boot-config-log (#3 silent-fail verify): runtime aktif-olu gate tespiti.
    # T1 static-lint PR-zamani yakalar; bu runtime backstop T1'i kacirani yakalar
    # (savunma-derinligi). Fail-safe: audit/discovery ASLA startup'i bozmaz.
    try:
        from app.core.config import DEFAULT_ENV_FILE
        from app.core.dead_gate import audit_runtime_dead_gates

        _repo_root = Path(__file__).resolve().parent.parent
        dead_gates = audit_runtime_dead_gates(DEFAULT_ENV_FILE, [_repo_root / "app", _repo_root / "automation"])
        for dg in dead_gates:
            logger.warning(
                "[DEAD-GATE] %s serviste no-op — .env'de tanimli, process-env'de yok, "
                "reader %s os.environ.get kullaniyor. read_env_var'a gec "
                "(bkz app/core/dead_gate.py).",
                dg.name,
                dg.reader,
            )
            # Q3 emit fire-and-forget (klipper #100091): up-ama-yavas servis edge'inde
            # await-emit boot'u 20-80s bloklayabilir. create_task -> boot ASLA bloklanmaz;
            # WARN-log (asil sinyal) zaten senkron dustu. Task-ref GC'den korunur.
            _t = asyncio.create_task(_emit_dead_gate_discovery(dg.name, dg.reader))
            _boot_emit_tasks.add(_t)
            _t.add_done_callback(_boot_emit_tasks.discard)
    except Exception:
        logger.exception("boot-config-log dead-gate audit basarisiz (startup etkilenmedi)")

    # Klipper telemetry: app fully initialized, fire-and-forget event POST.
    # CLAUDE.md zorunlu kayit kurali -- service-start event'i tasks_log'a dusmeli.
    # subprocess.Popen non-blocking; start_new_session=True ile parent kapanirsa
    # script ayakta kalir; her exception yutulur (telemetry asla startup'i bozamaz).
    try:
        import subprocess

        subprocess.Popen(
            ["/opt/linux-ai-server/scripts/klipper-event.sh", "service-start", "fastapi-ready"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except Exception:
        pass

    yield

    # Klipper telemetry: graceful shutdown event.
    # API kapanirken kendi /tasks endpoint'ine POST atilir -- script retry loop
    # 10s boyunca dener; sonuc cogu zaman GIVEUP olur ama log dosyasinda kanit kalir.
    try:
        import subprocess

        subprocess.Popen(
            ["/opt/linux-ai-server/scripts/klipper-event.sh", "service-stop", "graceful"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except Exception:
        pass

    # Graceful shutdown
    await devops.stop()
    await code_reviewer.stop()
    await db.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Linux-AI Server",
        description="Full kernel-level Linux control via REST API and MCP",
        version=__version__,
        lifespan=lifespan,
        swagger_ui_parameters={"persistAuthorization": True},
    )

    # Middleware order matters: outermost first
    # 1. Request ID — adds x-request-id to every request
    app.add_middleware(RequestIdMiddleware)

    # 2. CORS — browser cross-origin support
    from app.core.config import get_settings

    _settings = get_settings()
    # GUVENLIK: jwt_secret env-only ve placeholder/bos olamaz. Aksi halde JWT'ler
    # public-default ile imzalanir -> herkes gecerli admin-token forge eder. Bind
    # oncesi fail-fast (runtime-generate YANLIS: 2 worker farkli secret + restart'ta
    # token invalidasyonu). Test/prod env'i JWT_SECRET'i set eder; conftest de.
    from app.core.config import INSECURE_JWT_SECRETS

    if _settings.jwt_secret in INSECURE_JWT_SECRETS:
        raise RuntimeError(
            "JWT_SECRET zorunlu ve placeholder/bos olamaz. Guvenli deger uretip "
            "PROCESS ENV'ine gecirin: `openssl rand -hex 32` -> systemd unit "
            "`Environment=JWT_SECRET=...` ya da `EnvironmentFile=<yol>`. "
            "DIKKAT: Settings env_file OKUMAZ; ciplak .env DOSYASI tek basina "
            "yuklenmez (EnvironmentFile ile baglamadan ise yaramaz). server.yml "
            "world-readable -> secret ICIN KULLANMAYIN."
        )
    _cors_origins = [
        "http://localhost:8420",
        "http://localhost:3000",
        f"http://{_settings.lan_ip}:8420",
        f"http://{_settings.tailscale_ip}:8420",
        "https://panola.app",
        "https://petvet.panola.app",
        "https://kuafor.panola.app",
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 3. Audit — logs all POST/PUT/PATCH/DELETE to DB
    app.add_middleware(AuditMiddleware)

    # 4. Global rate limit — 200 req/min per client IP (safety net)
    app.add_middleware(GlobalRateLimitMiddleware, rate=200, per_seconds=60)

    @app.exception_handler(ServerError)
    async def server_error_handler(request: Request, exc: ServerError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": type(exc).__name__,
                "message": exc.message,
                "detail": exc.detail,
            },
        )

    # ── Tutarlı hata zarfı (#4): HTTPException + validation + unhandled hepsi
    # ServerError ile AYNI {error, message, detail} şeklini döner. `detail` her
    # zaman KORUNUR (geri-uyum — mevcut detail-okuyan test/UI bozulmaz), `error`+
    # `message` eklenir. HTTPException header'ları (Retry-After/WWW-Authenticate)
    # korunur. Unhandled → consistent 500 + traceback LOGLANIR (eskiden sessiz).
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "HTTPException", "message": exc.detail, "detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = jsonable_encoder(exc.errors())
        return JSONResponse(
            status_code=422,
            content={"error": "ValidationError", "message": "Request validation failed", "detail": errors},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception: %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": "InternalError", "message": "Internal server error", "detail": None},
        )

    # ---- Health (no auth, public, monitoring) ----
    # /health: Docker/systemd healthcheck pattern (root, no prefix)
    # /api/v1/health: versioned API parallel
    @app.get("/health")
    @app.get("/api/v1/health")
    async def health():
        disk = _current_disk_sha()
        # Codex P2: SHA belirlenemezse (git-yok + env-yok) stale SESSİZCE False olmasın —
        # None döndür ('belirlenemez'), yanlış 'drift-yok' güvencesi verme (silent-no-signal).
        stale = (disk != _DEPLOYED_SHA) if (_DEPLOYED_SHA and disk) else None
        return {
            "status": "healthy",
            "service": "linux-ai-server",
            "version": __version__,
            "sha": _DEPLOYED_SHA,  # ÇALIŞAN kod (startup'ta sabitlendi)
            "disk_sha": disk,  # disk-HEAD (canlı)
            "stale": stale,  # True=restart gerekli (deployed≠running) · None=belirlenemez
        }

    # ---- Routes ----
    app.include_router(auth_router)
    app.include_router(kernel_router)
    app.include_router(system_router)
    app.include_router(files_router)
    app.include_router(shell_router)
    app.include_router(network_router)
    app.include_router(dev_router)
    app.include_router(ssh_router)
    app.include_router(agents_router)
    app.include_router(webops_router)
    app.include_router(ai_router)
    app.include_router(monitoring_router)
    app.include_router(n8n_router)
    app.include_router(classifier_router)
    app.include_router(dispatch_router)
    app.include_router(logs_router)
    app.include_router(ws_monitor_router)
    app.include_router(ws_terminal_router)
    app.include_router(ws_logs_router)
    app.include_router(prometheus_router)
    app.include_router(backup_router)
    app.include_router(ws_status_router)
    app.include_router(rag_router)
    app.include_router(research_router)
    app.include_router(llm_router)
    app.include_router(telegram_bot_router)
    app.include_router(devops_router)
    app.include_router(deploy_router)
    app.include_router(vps_router)
    app.include_router(claude_code_router)
    app.include_router(projects_router)
    app.include_router(social_router)
    app.include_router(memory_router)
    app.include_router(memory_public_router)
    app.include_router(admin_router)
    app.include_router(validation_router)
    app.include_router(csp_router)
    app.include_router(ci_router)
    app.include_router(digest_router)
    app.include_router(security_router)

    @app.get("/ready")
    async def ready() -> dict:
        return {"ready": True, "version": __version__}

    # Dashboard — serve at /dashboard
    dashboard_dir = Path(__file__).parent / "dashboard"
    if dashboard_dir.is_dir():

        @app.get("/dashboard")
        async def dashboard():
            return FileResponse(dashboard_dir / "index.html")

    # Claude Code UI — serve at /claude
    claude_ui_dir = Path(__file__).parent / "claude_ui"
    if claude_ui_dir.is_dir():

        @app.get("/claude")
        async def claude_page():
            return FileResponse(claude_ui_dir / "index.html")

    return app


def main() -> None:
    uvicorn.run("app.main:create_app", factory=True, host="0.0.0.0", port=8420, workers=2)


if __name__ == "__main__":
    main()
