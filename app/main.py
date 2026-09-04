"""FastAPI application factory and server entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBearer
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

from app import __version__
from app.api.admin import router as admin_router
from app.api.agents import router as agents_router
from app.api.ai import router as ai_router
from app.api.auth import router as auth_router
from app.api.backup import router as backup_router
from app.api.ci import router as ci_router
from app.api.classifier import router as classifier_router
from app.api.claude_code import router as claude_code_router
from app.api.consciousness import router as consciousness_router
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
from app.api.memory.discussions import ui_router as discussions_ui_router
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
from app.middleware.exception_events import record_exception_event, route_template
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


def _app_code_drifted(deployed: str, disk: str) -> bool:
    """deployed..disk arası app/ (uvicorn-yüklü kod) değişti mi? klipper #100224: drift YALNIZ
    app/ değişince restart gerektirir; docs/scripts/tests/automation cron'ları disk'ten okunur
    (restart gerekmez). Eski sha-tabanlı `stale` her commit'te (docs dahil) True olup her merge'de
    drift-WARN flood'u tetikliyordu. git-diff belirlenemezse True = güvenli-taraf (eski davranış)."""
    import subprocess

    if deployed == disk:
        return False
    try:
        r = subprocess.run(
            ["git", "-C", str(Path(__file__).parent.parent), "diff", "--quiet", deployed, disk, "--", "app/"],
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return r.returncode != 0  # 0=app/ aynı (yalnız docs/script/test); !=0=app/ değişti=restart
    except Exception:
        return True  # belirlenemez (sha-yok/git-yok) → güvenli: stale (sha-tabanlı eski davranış)


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


async def _ensure_admin_key(db) -> str | None:
    """Hiç key yoksa default admin key oluştur (idempotent + race-safe). Üretilmiş
    (env-dışı) plaintext key'i döndürür ki çağıran/test görebilsin; aksi → None.

    #1197: uvicorn 2-worker startup race — eski SELECT-then-INSERT non-atomik'ti, iki
    worker da boş görüp İKİ admin-key INSERT edebiliyordu. Atomik INSERT...WHERE NOT
    EXISTS (WAL writer-serialize + busy_timeout=10s) → en fazla TEK key; rowcount=1
    yalnız gerçekten ekleyen worker'da (diğeri 0 = no-op).
    #1198: DEFAULT_API_KEY verilmemişse rastgele key üretilir — eskiden plaintext HİÇBİR
    yere yazılmıyordu → admin asla kullanamıyordu (kurtarılamaz). Üreten worker
    (rowcount=1 + env-yok) plaintext'i ilk-boot'ta WARNING log'lar → kurtarılabilir."""
    import os

    from app.auth.api_key import generate_api_key, hash_api_key

    env_key = os.environ.get("DEFAULT_API_KEY")
    default_key = env_key or generate_api_key()
    cursor = await db.execute(
        "INSERT INTO api_keys (key_hash, name, permissions) SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM api_keys)",
        (hash_api_key(default_key), "admin", "admin"),
    )
    if cursor.rowcount and not env_key:
        # #1304 güvenlik: plaintext key'i LOG'a yazma. /var/log rotate-yok (append-only,
        # CLAUDE.md) → plaintext-secret birikir, backup/merkezi-log/okuma ile sızar. Bunun
        # yerine 0600-restricted dosyaya yaz + log'da yalnız POINTER. Kurtarılabilirlik korunur
        # (#1198: admin dosyadan okur→.env'e taşır→siler), ama log-sızıntı-yüzeyi kalkar.
        from pathlib import Path

        from app.db.data_layer import server_db_path

        key_file = Path(os.environ.get("ADMIN_KEY_BOOTSTRAP_FILE") or (Path(server_db_path()).parent / "admin-key-firstboot.txt"))
        # klipper-review P2#3: env-yoksa server_db_path /tmp'e düşebilir. systemd PrivateTmp=yes →
        # operatör izole-tmpfs'e ERİŞEMEZ → key kurtarılamaz. 2.tur#1: explicit /tmp exemption KALDIRILDI.
        # 3.tur (#100655, CI-log-kanıtı): INVOCATION_ID GÜVENİLMEZ — GitHub-runner da systemd-altında
        # set eder (CI'da 2 test kırdı). Runtime-heuristic yerine DEPLOY-ZAMANI-CONFIG-FACT: systemd
        # unit'te PrivateTmp=yes'in YANINA Environment="ADMIN_KEY_TMP_ISOLATED=1" konur (install.sh).
        # Yalnız BU-unit True; CI/manuel/başka-systemd false-pozitif vermez (GITHUB_TOKEN Environment-deseni).
        # dead-gate-guard (klipper #100658 + bugünkü ders): raw os.environ.get() config-gate'i
        # systemd Environment= geçmezse sessiz-kırılır → read_env_var (process-env + .env-file okur).
        from app.core.config import read_env_var

        tmp_isolated = read_env_var("ADMIN_KEY_TMP_ISOLATED") == "1"
        write_ok = False
        try:
            # 3.tur (#100653): path-traversal — resolve() ÖNCE, sonra startswith (symlink+relatif kapanır).
            # klipper 4.tur #1: resolve() try-İÇİNDE — symlink-loop'ta OSError atarsa da rollback-yoluna
            # düşer (eskiden try-dışıydı → patlar, DB-row kilitli kalırdı).
            resolved = str(key_file.resolve())
            if tmp_isolated and resolved.startswith(("/tmp/", "/var/tmp/")):
                raise OSError("tmp-izole hedef (systemd PrivateTmp) — operatör erişemez, kurtarılamaz")
            data = (default_key + "\n").encode()
            # klipper-review P2#2: write_text()+chmod() arası TOCTOU — dosya varsayılan-umask (644) ile
            # oluşup chmod'a dek plaintext AÇIKTA. os.open(mode=0o600) atomik-oluşturur.
            # klipper 4.tur #2: O_EXCL — saldırgan hedef-dosyayı ÖNCEDEN kendi-owner'ıyla oluşturursa
            # fchmod(0600) SAHİPLİĞİ değiştirmez → owner-okur (priv-esc). O_EXCL dosya-varsa fail eder →
            # rollback. NOT: legit-kalıntı (eski-key silinmemiş + DB-boş) durumunda da fail → operatör
            # dosyayı silip restart etmeli (fail-loud; tek-kullanıcı-lab'da kabul).
            fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                if hasattr(os, "fchmod"):  # POSIX; Windows'ta os.open mode kısmen uygular
                    os.fchmod(fd, 0o600)
                # klipper-review 2.tur: os.write kısmi-yazabilir (disk-dolu/quota) → TRUNCATED-key.
                written = os.write(fd, data)
                if written != len(data):
                    raise OSError(f"kısmi-yazma: {written}/{len(data)} byte (disk-dolu/quota?)")
                # klipper 4.tur #3: fsync — os.write başarılı dönse de kernel-crash/güç-kesintisinde
                # dosya-içeriği kaybolabilir (DB-commit kalıcı) → restart-tutarsızlık. fsync ile kalıcılaştır.
                if hasattr(os, "fsync"):
                    os.fsync(fd)
            finally:
                os.close(fd)
            write_ok = True
        except OSError as e:
            logger.error("İlk-boot admin-key dosya-yazma/güvenlik hatası (%s): %s", key_file, e)
        if write_ok:
            logger.warning(
                "İlk-boot: admin API key ÜRETİLDİ (DEFAULT_API_KEY set değil). Plaintext key "
                "LOG'a yazılmadı (güvenlik); 0600-dosyaya yazıldı: %s — OKU, .env'de DEFAULT_API_KEY "
                "olarak sabitle, sonra DOSYAYI SİL.",
                key_file,
            )
            return default_key
        # klipper-review P2#1 (KRİTİK, surer'in şerh-hatası): dosya-yazılamadı VEYA /tmp-izole →
        # plaintext kurtarılamaz. Key DB'de KALIRSA sonraki restart'ta DEFAULT_API_KEY versen bile
        # 'WHERE NOT EXISTS' yeni-insert'i engeller → servis KALICI kilitlenir. Üretilen row'u GERİ
        # AL ki restart temiz-başlasın (env-key insert edilebilsin). Fail-loud.
        await db.execute("DELETE FROM api_keys WHERE key_hash = ? AND name = 'admin'", (hash_api_key(default_key),))
        logger.error(
            "İlk-boot admin-key güvenli-kaydedilemedi (path=%s). Üretilen key GERİ ALINDI; "
            ".env'de DEFAULT_API_KEY ayarlayıp yeniden başlatın.",
            key_file,
        )
        return None
    return None


def _acquire_remediation_leader_lock(lock_path: str | None = None) -> int | None:
    """disc#1352 P0-fix: uvicorn --workers N → her worker kendi DevOpsAgent'ını başlatır, hepsi
    aynı alarmı bağımsız görüp bağımsız remediation tetikler (kanıt: 07-17 çift docker-restart,
    remediation-id 78-83 aynı-saniye çiftler). Non-blocking flock (LOCK_EX|LOCK_NB): ilk-worker
    kilidi alır ve PROCESS-ÖMRÜ boyunca tutar (fd kasıtlı açık bırakılır — çağıran onu
    app.state'te canlı tutmalı, aksi halde GC→close→unlock olur); diğer worker'lar hemen fail
    olup non-leader kalır (OS lock zaten tutuluyorsa bekleMEZ, anında döner).

    Codex-P1 (PR#334): default-path tempfile.gettempdir() altında — consciousness.py'deki
    KANITLANMIŞ aynı-problem deseniyle (_try_worker_lock) birebir. İlk denemem /opt/.../data/
    hook-state altındaydı; scripts/install.sh'nin ProtectSystem=strict+ReadWritePaths
    (/var/lib, /var/log, /var/AI-stump — /opt DAHİL DEĞİL) sertleştirmesinde os.makedirs/open
    PermissionError fırlatıp lifespan'i (dolayısıyla TÜM app boot'unu) çökertirdi. /tmp,
    systemd PrivateTmp ile servise-özel ve hem klipperos hem hardened-aiserver dağıtımında
    yazılabilir — path-varsayımı yerine KANITLANMIŞ-yazılabilir konum kullanılır. AYRICA:
    tüm makedirs+open+flock TEK try/except (BlockingIOError, OSError) içinde — herhangi bir
    dosya-sistemi/izin hatası da (yalnız 'zaten kilitli' değil) sessizce None döner (fail-safe
    degrade = bu worker'da remediation kapanır, ama app YİNE DE AÇILIR — çökme yerine
    'notify-mode gibi davran')."""
    import fcntl
    import os
    import tempfile

    path = lock_path or os.path.join(tempfile.gettempdir(), "devops-remediation-leader.lock")
    fd: int | None = None
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except (BlockingIOError, OSError):
        if fd is not None:  # open başarılı ama flock/write başarısız — fd sızmasın
            os.close(fd)
        return None


_CONTINUOUS_AGENT_RETRY_SECONDS = 5.0
_CONTINUOUS_AGENT_START_ORDER = (
    "memory_consolidator",
    "learning_loop",
    "critic_agent",
    "code_review_agent",
    "consciousness_stream",
)
_CONTINUOUS_AGENT_STOP_ORDER = (
    "consciousness_stream",
    "code_review_agent",
    "critic_agent",
    "learning_loop",
    "memory_consolidator",
)


def _set_continuous_agent_role(app: FastAPI, leader_lock: Any, role: str) -> None:
    app.state.continuous_agents_lock_role = role
    app.state.continuous_agents_role = (
        "starting" if role == "leader" and not getattr(app.state, "continuous_agents_started", False) else role
    )
    app.state.continuous_agents_lock_fd = leader_lock.fd
    app.state.continuous_agents_lock_error = leader_lock.error


async def _start_continuous_agent_cohort(app: FastAPI) -> bool:
    """Start all process-local AgentBus peers together, consumers before publisher."""
    if getattr(app.state, "continuous_agents_started", False):
        return True

    attempted: list[Any] = []
    app.state.continuous_agents_rollback_clean = True
    try:
        for attr in _CONTINUOUS_AGENT_START_ORDER:
            agent = getattr(app.state, attr)
            attempted.append(agent)
            agent.start()
    except Exception as exc:
        logger.exception("continuous-agent cohort startup failed; this worker stays fail-closed")
        for agent in reversed(attempted):
            try:
                await agent.stop()
            except Exception:
                app.state.continuous_agents_rollback_clean = False
                logger.exception("continuous-agent rollback stop failed: %s", type(agent).__name__)
        app.state.continuous_agents_started = False
        app.state.continuous_agents_role = "lock_error"
        app.state.continuous_agents_lock_error = f"agent startup failed: {type(exc).__name__}: {exc}"
        return False

    app.state.continuous_agents_started = True
    app.state.continuous_agents_rollback_clean = True
    app.state.continuous_agents_role = "leader"
    app.state.continuous_agents_lock_error = None
    logger.info("continuous-agent cohort started in leader worker")
    return True


async def _stop_continuous_agent(app: FastAPI, attr: str) -> None:
    agent = getattr(app.state, attr, None)
    if agent is None:
        return
    try:
        await agent.stop()
    except Exception:
        logger.exception("continuous-agent stop failed: %s", attr)


async def _stop_continuous_agent_cohort(app: FastAPI) -> None:
    """Stop every cohort member; one broken stop must not strand the others."""
    for attr in _CONTINUOUS_AGENT_STOP_ORDER:
        await _stop_continuous_agent(app, attr)
    app.state.continuous_agents_started = False


def _terminate_worker_for_cohort_failure() -> None:
    """Fail-stop a worker whose partially-started cohort could not be cleaned up."""
    import os
    import signal

    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception:
        # A live process retaining the cohort fd after failed startup would block
        # every standby indefinitely.  If graceful termination cannot be
        # requested, exit immediately so the kernel still releases the lock.
        logger.critical("failed to signal unsafe continuous-agent worker; exiting immediately", exc_info=True)
        os._exit(1)


async def _continuous_agent_leadership_retry(app: FastAPI) -> None:
    """Let a standby worker take over promptly if the elected worker exits."""
    leader_lock = app.state.continuous_agents_leader_lock
    previous_role = app.state.continuous_agents_role
    while True:
        await asyncio.sleep(_CONTINUOUS_AGENT_RETRY_SECONDS)
        role = leader_lock.try_acquire()
        _set_continuous_agent_role(app, leader_lock, role)
        worker_role = app.state.continuous_agents_role
        if worker_role != previous_role:
            logger.info("continuous-agent worker role changed: %s -> %s", previous_role, worker_role)
            previous_role = worker_role
        if role == "leader":
            if await _start_continuous_agent_cohort(app):
                app.state.continuous_agents_retry_task = None
                return
            logger.critical(
                "continuous-agent takeover startup failed (rollback_clean=%s); retaining leadership until worker exit",
                app.state.continuous_agents_rollback_clean,
            )
            app.state.continuous_agents_retry_task = None
            _terminate_worker_for_cohort_failure()
            return


async def _initialize_continuous_agent_cohort(app: FastAPI, leader_lock: Any) -> None:
    app.state.continuous_agents_leader_lock = leader_lock
    app.state.continuous_agents_started = False
    app.state.continuous_agents_retry_task = None

    role = leader_lock.try_acquire()
    _set_continuous_agent_role(app, leader_lock, role)
    if role == "leader":
        if await _start_continuous_agent_cohort(app):
            return
        raise RuntimeError(
            "continuous-agent startup failed; retaining leadership until worker exit "
            f"(rollback_clean={app.state.continuous_agents_rollback_clean})"
        )

    if role == "standby":
        logger.info("continuous-agent cohort standby; another worker owns %s", leader_lock.path)
    else:
        logger.error("continuous-agent cohort unavailable; retrying fail-closed: %s", app.state.continuous_agents_lock_error)
    app.state.continuous_agents_retry_task = asyncio.create_task(_continuous_agent_leadership_retry(app))


async def _shutdown_continuous_agent_cohort(app: FastAPI, bus: Any, bridge_handler: Any) -> None:
    retry_task = getattr(app.state, "continuous_agents_retry_task", None)
    if retry_task is not None:
        retry_task.cancel()
        await asyncio.gather(retry_task, return_exceptions=True)
        app.state.continuous_agents_retry_task = None

    # Quiesce the sole thought publisher before cancelling delivery retries;
    # then stop the consumers in their normal reverse dependency order.
    await _stop_continuous_agent(app, "consciousness_stream")
    try:
        await bus.stop()
    except Exception:
        logger.exception("agent-bus retry shutdown failed")
    for attr in _CONTINUOUS_AGENT_STOP_ORDER[1:]:
        await _stop_continuous_agent(app, attr)
    app.state.continuous_agents_started = False
    bus.unsubscribe("*", bridge_handler)
    # Keep a successfully-acquired fd open until process exit. A cancelled
    # asyncio.to_thread call can leave its worker thread running briefly; early
    # unlock would let another process overlap that in-flight work. This makes
    # one lifespan per Uvicorn worker process an explicit deployment invariant;
    # embedded same-process lifespan restarts are intentionally unsupported.


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    import os

    from app.db.database import DEFAULT_DB_PATH, Database

    db_path = os.environ.get("DB_PATH", DEFAULT_DB_PATH)
    db = Database(db_path)
    await db.initialize()

    await _ensure_admin_key(db)

    app.state.db = db

    # Start DevOps Agent daemon
    from app.core.devops_agent import DevOpsAgent

    devops = DevOpsAgent(db=db, interval=30)
    app.state.devops_agent = devops
    # disc#1352 P0-fix: çok-worker'da yalnız 1 worker gerçek-remediation yürütsün (bkz
    # _acquire_remediation_leader_lock docstring). Kilit fd'sini app.state'te tut — yerel
    # değişken GC'lenirse (raw os.open fd'si GC'den bağımsız ama referans kaybı okunaksız kod
    # olurdu) kilit-durumu izlenemez hâle gelir; process-ömrü boyunca canlı-referans şart.
    app.state.devops_leader_lock_fd = _acquire_remediation_leader_lock()
    devops._is_remediation_leader = app.state.devops_leader_lock_fd is not None
    if not devops._is_remediation_leader:
        logger.info("devops-agent: bu worker remediation-lider DEĞİL (başka worker kilidi tutuyor veya kilit-yolu yazılamıyor, disc#1352)")
    devops.start()

    # Build the process-local AgentBus cohort. Only one worker starts the whole
    # group so publishers and consumers share the same in-memory bus.
    from app.core.consciousness import ConsciousnessStream

    consciousness = ConsciousnessStream(interval=15, devops_agent=devops, manage_worker_lock=False)
    app.state.consciousness_stream = consciousness

    from app.core.agent_bus import get_bus

    bus = get_bus()
    app.state.agent_bus = bus

    from app.core.critic_agent import CriticAgent

    critic = CriticAgent(interval=60)
    app.state.critic_agent = critic

    from app.core.memory_consolidator import MemoryConsolidator

    consolidator = MemoryConsolidator(interval=600)
    app.state.memory_consolidator = consolidator

    from app.core.learning_loop import LearningLoop

    learning = LearningLoop(interval=3600)
    app.state.learning_loop = learning

    # AgentBus ↔ events spine bridge (çift yönlü)
    from app.core.event_spine_bridge import bridge_handler

    bus.subscribe_to_all(bridge_handler)
    logger.info("event spine bridge registered (bus → events)")

    # Attention Router: bus → work_items (olü sinyaller iş itemi üretir)
    from app.core.attention_router import route_event

    bus.subscribe_to_all(route_event)
    logger.info("attention router registered (bus → work_items)")

    # Read-only kod-mühendisi ajanı (qwen2.5-coder): commit-diff + idle-sweep ile
    # sürekli inceleme → discoveries (dedup'lı) + P1 Telegram. KOD DEĞİŞTİRMEZ.
    # CODE_REVIEW_ENABLED=0 ile kapatılır. start() yalnız enabled ise task açar.
    from app.core.code_review_agent import CodeReviewAgent

    code_reviewer = CodeReviewAgent(interval=300)
    app.state.code_review_agent = code_reviewer

    from app.core.continuous_agent_leader import ContinuousAgentLeaderLock

    await _initialize_continuous_agent_cohort(app, ContinuousAgentLeaderLock())

    # Presence + durable event dispatcher (yalniz leader worker)
    #
    # KAPALI — 2026-09-03, kontrollu deney (server.db bozulmasi #10, kesif #1676).
    # Bu iki dongu 2026-08-27'de commit EDILMEDEN production'a girdi: dispatcher
    # 1 sn'de bir cursor yaziyor, presence 15 sn'de bir heartbeat atiyordu.
    # Bozulma sikligi ayni tarihte ikiye katlandi ve son iki olayin TEK ortak
    # paydasi bu kod (backup da restart da ortak degil). NEDENSELLIK KANITLANMADI;
    # bir hafta bozulma olmazsa sebep buradadir. Deney biti: 2026-09-10.
    #
    # KAPSAM UYARISI — bu tek-degiskenli bir deney DEGIL: attention_router
    # (bus -> work_items, ~251 satir/saat) ayni partide eklendi ve kapinin
    # DISINDA, hala yaziyor. Kalkan yuk eklenen yazmalarin ~%96'si; kalan %4
    # hala aday. Bozulma tekrarlarsa ilk bakilacak yer orasi.
    #
    # Geri acmak icin: asagidaki bayragi True yap.
    presence_dispatcher_enabled = False
    if presence_dispatcher_enabled and getattr(app.state, "continuous_agents_role", "") == "leader":
        try:
            from app.core.durable_dispatcher import create_dispatcher

            dispatcher = create_dispatcher(bus)
            app.state.durable_dispatcher = dispatcher
            await dispatcher.start()
            logger.info("durable event dispatcher started (leader worker)")
        except Exception:
            logger.exception("durable dispatcher startup failed")
        try:
            from app.core.presence_heartbeat_task import run_presence_heartbeat_loop

            app.state.presence_task = asyncio.create_task(run_presence_heartbeat_loop())
            logger.info("presence heartbeat loop started (leader worker)")
        except Exception:
            logger.exception("presence heartbeat loop startup failed")

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

    try:
        yield
    finally:
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

        await _shutdown_continuous_agent_cohort(app, bus, bridge_handler)
        running_dispatcher = getattr(app.state, "durable_dispatcher", None)
        if running_dispatcher:
            await running_dispatcher.stop()
        ptask = getattr(app.state, "presence_task", None)
        if ptask:
            ptask.cancel()
            try:
                await ptask
            except asyncio.CancelledError:
                pass
        try:
            await devops.stop()
        except Exception:
            logger.exception("devops-agent stop failed")
        try:
            await db.close()
        except Exception:
            logger.exception("database close failed")


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
        # gap-2: unhandled-exc → events-spine (fingerprint + throttle). Producer 500-
        # yanıtını ASLA bloklamaz/çökertmez (kendi try/except'i + cold-path to_thread).
        # method/path thread-ÖNCESİ extract (live Request thread'e geçmesin); path =
        # route-template (KVKK: PII'siz).
        try:
            method = request.method
            path = route_template(request)
            await asyncio.to_thread(record_exception_event, exc, method=method, path=path)
        except Exception:
            logger.exception("exception-event dispatch hatası (fail-safe; yanıt etkilenmez)")
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
        # klipper #100224: stale artık CONTENT-aware — yalnız app/ (uvicorn-yüklü kod) değişince
        # True. docs/scripts/tests/automation commit'leri restart gerektirmez → drift-flood yok.
        stale = _app_code_drifted(_DEPLOYED_SHA, disk) if (_DEPLOYED_SHA and disk) else None
        return {
            "status": "healthy",
            "service": "linux-ai-server",
            "version": __version__,
            "sha": _DEPLOYED_SHA,  # ÇALIŞAN kod (startup'ta sabitlendi)
            "disk_sha": disk,  # disk-HEAD (canlı)
            "stale": stale,  # True=app/ drift, restart gerekli · False=app/ aynı · None=belirlenemez
        }

    # ---- Routes ----
    app.include_router(auth_router)
    app.include_router(discussions_ui_router)
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
    app.include_router(consciousness_router)
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
