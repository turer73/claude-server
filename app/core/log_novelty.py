"""Gap-3 ingestion-producer: log-akışından NOVEL template tespiti (Drain3) → events-spine.

`journalctl -u linux-ai-server` → Drain3 online-template-miner → `change_type=cluster_created`
(ilk-kez-görülen log-deseni = NOVEL) → `emit_event(type="log-novelty", severity="warn")`.
Klipper static-eşik + app-exception'ı (gap-2) izliyordu ama ÖNGÖRÜLMEYEN yeni log-desenleri
(yeni hata-türü, beklenmedik trace, 3rd-party uyarısı) sinyal-dışıydı; bu producer onu kapatır
(awareness-research gap-3).

NEDEN Drain3: online log-template-miner (saf-Python, CPU; akıştan kümeleme). State `save_state`
ile persist → `load_state` ile yüklenir → "novel" = TÜM-zamanlarda-ilk-kez (cross-run, doğrulandı).
cluster_created once-only = DOĞAL dedup (aynı desen tekrar emit edilmez).

KVKK (2 katman): (1) explicit `redact()` — email/jwt/secret/ip/uzun-sayı/home-path scrub.
ZORUNLU çünkü Drain3 ilk-occurrence'da generalize ETMEZ (cluster_created = ham satır; `<*>`
ancak 2. benzer satırda gelir = cluster_template_changed, emit-edilmez) → emit-anında ham-PII
sızabilirdi. (2) Drain3 TEMPLATE generalization (`<*>`) = ikinci katman. Birlikte log-PII
(user-mail/id/path/token) maskelenir.

FİLTRE: yalnız error-ish satırlar minelenir (ERROR/CRITICAL/Exception/Traceback/Failed/Fatal/
Panic) → her yeni-INFO-format gürültü üretmesin (sinyal-değil-gürültü; gap-2 disiplini).

CAP: per-run `max_emit` novel-template emit edilir; aşılırsa kalanı LOG'lanır (no-silent-cap) —
Drain3 hepsini ÖĞRENİR (state'e girer, tekrar-novel olmaz) ama emit cap'lenir (log-overhaul
flood-koruma). severity=warn (warn DA Telegram-page'liyor = awareness doğru, gap-2 #100139 dersi).
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from app.core.config import read_env_var

logger = logging.getLogger(__name__)

JOURNAL_UNIT = "linux-ai-server"
LOG_NOVELTY_WINDOW_MIN = 10  # journalctl --since penceresi (cron-cadence + buffer)
LOG_NOVELTY_MAX_EMIT = 10  # per-run novel-emit tavanı (log-overhaul flood-koruma)
DEFAULT_STATE_PATH = "data/hook-state/log-novelty-drain3.bin"

# Error-ish satır filtresi (case-insensitive). Novel-template yalnız bunlar arasında aranır.
_INTERESTING = re.compile(r"\b(error|critical|exception|traceback|failed|fatal|panic)\b", re.IGNORECASE)

# KVKK redaction (best-effort, defense-in-depth). Drain3 template'i ilk-occurrence'da HENÜZ
# generalize DEĞİL (cluster_created = ham satır; `<*>` ancak 2. benzer satırda) → emit-anında
# ham-PII sızabilir. Bu yüzden template'e explicit scrub uygulanır (Drain3-generalize 2. katman).
_REDACT: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "<jwt>"),
    (re.compile(r"(?i)\b(?:sk|pk|key|token|secret|bearer|password|pwd)[-_:=\s]+\S{6,}"), "<secret>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\b\d{7,}\b"), "<num>"),
    (re.compile(r"/(?:home|Users)/[^/\s]+"), "/<user>"),
)


def redact(text: str) -> str:
    """Ham log-satırı/template'inden yaygın PII'yi temizle (email/jwt/secret/ip/uzun-sayı/home-path).
    Best-effort denylist (mükemmel değil; Drain3-generalize ikinci katman). bkz feedback regex-denylist."""
    for pat, repl in _REDACT:
        text = pat.sub(repl, text)
    return text


def _enabled() -> bool:
    """Kill-switch (default ON). read_env_var (.env + process-env; os.environ.get DEĞİL →
    #174 sınıfı). Değer early-return'de kullanılır → dead_gate ölü-gate sanmaz."""
    return (read_env_var("LOG_NOVELTY_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")


def is_interesting(line: str) -> bool:
    """Satır error-ish mi? Novel-arama bunlarla sınırlı (gürültü-azaltma)."""
    return bool(_INTERESTING.search(line))


def read_journal_lines(since_min: int = LOG_NOVELTY_WINDOW_MIN, unit: str = JOURNAL_UNIT, timeout: float = 15.0) -> list[str]:
    """`journalctl -u UNIT --since -Nmin -o cat` (yalnız mesaj-metni). Linux-only;
    hata/non-Linux/rc!=0 → [] (fail-safe; liveness.py:404 subprocess deseni)."""
    try:
        proc = subprocess.run(  # noqa: S603, S607 — sabit argv, shell yok
            ["journalctl", "-u", unit, "--since", f"-{int(since_min)}min", "--no-pager", "-o", "cat"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _build_miner(state_path: str | Path) -> Any:
    """Drain3 TemplateMiner + FilePersistence (state cross-run → kalıcı-novelty). config-objesi
    geçilir (drain3.ini-arama uyarısını önler). Drain3 stub'suz → lazy-import + Any-tip."""
    from drain3 import TemplateMiner  # type: ignore[import-untyped]
    from drain3.file_persistence import FilePersistence  # type: ignore[import-untyped]
    from drain3.template_miner_config import TemplateMinerConfig  # type: ignore[import-untyped]

    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    cfg = TemplateMinerConfig()
    cfg.profiling_enabled = False
    return TemplateMiner(persistence_handler=FilePersistence(str(state_path)), config=cfg)


def detect_novel(lines: list[str], miner: Any) -> list[dict[str, Any]]:
    """Filtreli satırları Drain3'e ver; `cluster_created` (NOVEL) template'leri döndür.

    KVKK: `template_mined` (PII-maskeli `<*>`) saklanır, ham-satır DEĞİL. Drain3 state
    mutasyonu burada olur (tüm-filtreli-satır öğrenilir) → çağıran save_state etmeli."""
    novel: list[dict[str, Any]] = []
    for line in lines:
        if not is_interesting(line):
            continue
        res = miner.add_log_message(line)
        if res.get("change_type") == "cluster_created":
            novel.append({"cluster_id": res.get("cluster_id"), "template": res.get("template_mined") or "<?>"})
    return novel


def run_log_novelty(
    state_path: str | Path = DEFAULT_STATE_PATH,
    since_min: int = LOG_NOVELTY_WINDOW_MIN,
    max_emit: int = LOG_NOVELTY_MAX_EMIT,
    lines: list[str] | None = None,
) -> dict[str, int]:
    """Tek tur: journalctl → filter → Drain3 → novel-template → emit_event(log-novelty, warn).

    Fail-safe (hiçbir dal cron'u bozmaz). `lines` verilirse journalctl atlanır (test-injection).
    Döndürür: {scanned, novel, emitted, suppressed_cap}."""
    from app.core.events import emit_event

    summary: dict[str, int] = {"scanned": 0, "novel": 0, "emitted": 0, "suppressed_cap": 0}
    try:
        if not _enabled():
            return summary
        src = lines if lines is not None else read_journal_lines(since_min)
        summary["scanned"] = len(src)
        miner = _build_miner(state_path)
        novel = detect_novel(src, miner)
        # KRİTİK: cron kısa-ömürlü → interval-snapshot tetiklenmez; cross-run novelty için
        # ZORUNLU explicit save (yoksa her run her-şeyi yeniden-novel sayar).
        miner.save_state("log-novelty cron-run")
        summary["novel"] = len(novel)
        for i, nv in enumerate(novel):
            if i >= max_emit:
                summary["suppressed_cap"] += 1
                continue
            template = redact(str(nv["template"]))[:300]  # KVKK: ilk-occurrence ham-PII içerebilir
            if emit_event(
                type="log-novelty",
                source=f"log-novelty:{nv['cluster_id']}",
                title=f"yeni log-deseni: {template[:80]}",
                severity="warn",
                detail=template,
                payload={"cluster_id": nv["cluster_id"], "template": template},
            ):
                summary["emitted"] += 1
        if summary["suppressed_cap"]:
            # no-silent-cap (feedback dersi): cap'lenen-sayı görünür LOG'lanır.
            logger.warning(
                "log-novelty: %d novel-template CAP'lendi (max_emit=%d) — log-format-overhaul?",
                summary["suppressed_cap"],
                max_emit,
            )
    except Exception:
        logger.exception("log-novelty tarama hatası (fail-safe)")
    return summary
