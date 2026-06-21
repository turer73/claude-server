"""Gap-2 ingestion-producer: FastAPI unhandled-exception → events-spine.

Klipper SİSTEM'i izliyor ama kendi FastAPI traceback'lerini events'e yazmıyordu
(awareness-research'in en büyük "tüm-hatalar" boşluğu). Bu modül mevcut
unhandled_exception_handler'a (app/main.py @app.exception_handler(Exception))
bağlanır: unhandled exc → fingerprint → emit_throttled(type="exception").

NEDEN HANDLER-HOOK (ayrı BaseHTTPMiddleware DEĞİL): Starlette ServerErrorMiddleware
@app.exception_handler(Exception)'a YALNIZ gerçekten-unhandled exc yollar.
StarletteHTTPException (4xx/5xx), RequestValidationError (422) ve ServerError kendi
handler'larına gider, BURAYA düşmez → "unhandled-5xx emit, 4xx ASLA emit" YAPISAL
garanti (try/except-tahmin değil, framework-routing).

KVKK: ham str(exc) PERSIST EDİLMEZ — exc-mesajı en büyük PII-vektörü (user-mail/id/
sorgu-değeri sızar). Sadece exc-tipi + APP-frame + method + route-template yazılır.
Tam-traceback zaten main.py logger.exception ile server-log'da (erişim-kontrollü).

FINGERPRINT: {ExcType}:{rel_module}:{func} — en-derin APP-frame (lib/site-packages
atlanır). LINE fingerprint'e GİRMEZ (her edit'te kayar → known-exc yanlış "novel"
görünür); line payload+title'da debug için tutulur.
"""

from __future__ import annotations

import logging
import os
import traceback
from collections.abc import Callable
from typing import Any

from app.core.config import read_env_var
from app.core.emit_throttle import ThrottleResult, emit_throttled

logger = logging.getLogger(__name__)

EXCEPTION_EVENT_TYPE = "exception"
EXCEPTION_WINDOW_SECONDS = 600.0  # aynı fingerprint 10 dk: re-emit YOK (throttle)
# severity=WARN ile başla, critical DEĞİL (klipper #100136): prod unhandled-exc HACMİNE
# dair sıfır-veri var → critical/real-time-page "nadir"-varsayımına bahis olur + gün-1
# pager-fatigue kanalı zehirler. WARN yine events-spine→LSA-feed'e düşer (sinyal kaybı
# YOK). ~1 hafta gözle; distinct-fingerprint hacmi düşük + bulgular gerçek-prod-bug ise
# gate ile critical'e TERFİ (bu sabiti bump et / env-gate ekle).
EXCEPTION_SEVERITY = "warn"

# Repo-kökü: bu dosya app/middleware/exception_events.py → 3 dizin yukarı.
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_APP_DIR = os.path.join(_APP_ROOT, "app")  # {repo}/app — APP-frame sınırı


def _enabled() -> bool:
    """Kill-switch (default ON). read_env_var (.env + process-env; os.environ.get
    DEĞİL → #174 sınıfı: systemd EnvironmentFile'ı os.environ'a yüklemez). Değer
    early-return'de kullanılır → dead_gate scanner ölü-gate sanmaz."""
    return (read_env_var("EXCEPTION_EVENTS_ENABLED") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _is_app_frame(filename: str) -> bool:
    """Frame APP-kodu mu (lib/site-packages/stdlib/<string> değil)?"""
    try:
        norm = os.path.abspath(filename).replace("\\", "/")
    except (OSError, ValueError):
        return False
    if "site-packages" in norm or "/dist-packages/" in norm or norm.startswith("<"):
        return False
    appdir = _APP_DIR.replace("\\", "/").rstrip("/") + "/"
    return norm.startswith(appdir)


def _rel_module(filename: str) -> str:
    """{repo}/app/api/shell.py → app/api/shell.py (repo-rel). Kök-dışı → basename."""
    norm = os.path.abspath(filename).replace("\\", "/")
    root = _APP_ROOT.replace("\\", "/").rstrip("/") + "/"
    return norm[len(root) :] if norm.startswith(root) else os.path.basename(norm)


def _extract_app_frame(exc: BaseException, *, is_app: Callable[[str], bool] | None = None) -> tuple[str, str, int] | None:
    """En-DERİN app-frame: (rel_module, function, lineno). Yoksa None.

    "En-derin" = exc'in raise-edildiği yere en yakın app-kodu (en spesifik, en stabil
    fingerprint). extract_tb dış→iç sıralı; reversed → en-derinden ilk app-frame.
    """
    pred = is_app or _is_app_frame
    for fs in reversed(traceback.extract_tb(exc.__traceback__)):
        if pred(fs.filename):
            return (_rel_module(fs.filename), fs.name or "?", int(fs.lineno or 0))
    return None


def fingerprint(exc: BaseException, *, is_app: Callable[[str], bool] | None = None) -> str:
    """{ExcType}:{rel_module}:{func} (LINE hariç — stabilite). App-frame yok → sentinel."""
    etype = type(exc).__name__
    frame = _extract_app_frame(exc, is_app=is_app)
    if frame is None:
        return f"{etype}:<no-app-frame>"
    rel, func, _line = frame
    return f"{etype}:{rel}:{func}"


def route_template(request: Any) -> str:
    """Eşleşen route'un TEMPLATE'i (/api/v1/items/{id} — PII'siz). Yoksa gerçek path.

    KVKK: template path-param değerlerini ({id}) maskeler → URL'deki PII'yi tutmaz.
    Async-handler'da (thread-öncesi) çağrılmalı; sadece request.scope'a dokunur.
    """
    try:
        route = request.scope.get("route")
        tmpl = getattr(route, "path", None)
        if isinstance(tmpl, str) and tmpl:
            return tmpl
        return str(request.url.path)
    except Exception:
        return "<unknown>"


def record_exception_event(
    exc: BaseException,
    *,
    method: str,
    path: str,
    is_app: Callable[[str], bool] | None = None,
) -> ThrottleResult | None:
    """unhandled_exception_handler'dan (asyncio.to_thread ile) çağrılır.

    500-yanıtını ASLA çökertmez: her dal yakalanır, iç-hata → logger.exception
    (sessiz-yutma YOK, #186 dersi). Kapalı/iç-hata → None; aksi → ThrottleResult.
    """
    try:
        if not _enabled():
            return None
        etype = type(exc).__name__
        frame = _extract_app_frame(exc, is_app=is_app)
        if frame is not None:
            rel, func, line = frame
            title = f"{etype} @ {rel}:{func}"
        else:
            rel, func, line = ("<no-app-frame>", "?", 0)
            title = f"{etype} (no-app-frame)"
        fp = fingerprint(exc, is_app=is_app)
        payload = {
            "fingerprint": fp,
            "exc_type": etype,
            "module": rel,
            "function": func,
            "lineno": line,
            "method": method,
            "path": path,  # route-template (KVKK: PII maskeli), fallback gerçek-path
        }
        res = emit_throttled(
            type=EXCEPTION_EVENT_TYPE,
            source=f"exception:{fp}",  # gerçek source-kolonu → novelty/throttle sorgusu
            title=title,
            severity=EXCEPTION_SEVERITY,  # klipper #100136: warn-ile-başla (pager-fatigue önle)
            detail=f"{method} {path}",
            payload=payload,
            window_seconds=EXCEPTION_WINDOW_SECONDS,
        )
        if res.emitted:
            logger.info("exception-event emit: %s novel=%s id=%s", fp, res.novel, res.event_id)
        return res
    except Exception:
        logger.exception("exception-event producer iç-hata (fail-safe; 500-yanıtı etkilenmez)")
        return None
