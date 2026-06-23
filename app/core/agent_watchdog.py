"""Agent watchdog — klipper'in KENDI sub-ajanlarinin saglik-farkindaligi (gap-7).

LIVESYS ingestion-producer: runaway-process + heartbeat-stall'i tespit edip MEVCUT
events-spine'a (app.core.events.emit_event) yazar -> LSA-Faz2/agent-feed otomatik gosterir.
Mevcut agent_freshness() cron-scheduling-freshness yapar; bu modul EKSIK parcayi kapatir:
core-pinned RUNAWAY proc (klipper 88°C-incident: 4 kacak scanner %100×4core 17-25dk) +
always-on-ajan heartbeat-STALL.

FP-ONLEME (klipper #100115 — yanlis-pozitif "felaketten beter", mesru-isi oldurmesin):
- COMERT esik: core-pinned >RUNAWAY_CPU_PCT + SURE >RUNAWAY_MIN_MINUTES (pytest~90s,
  ruff/rsync birkac-dk MESRU, oldurulmemeli).
- ZORUNLU ALLOWLIST: pytest/ruff/mypy/npm/node/rsync/backup/cron-wrap/git -> ASLA auto-kill.
- KADEMELI: borderline/allowlist -> notify-only; auto-kill SADECE net-runaway + cmdline-
  dogrulanmis + allowlist-disi + AUTO_KILL gate-ON. Reversible + provenance.
- auto-kill DEFAULT-OFF (read_env_var AGENT_WATCHDOG_AUTOKILL; notify-only baslar).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import read_env_var

logger = logging.getLogger(__name__)

# --- Esikler (config; gate'ler read_env_var ile — os.environ.get DEGIL, #174 sinifi) ---
RUNAWAY_CPU_PCT = 90.0  # core-pinned sayilir
RUNAWAY_MIN_MINUTES = 15.0  # bu sureden uzun core-pin = net-runaway (comert: 90s-pytest haric)
ALLOWLIST_WARN_MINUTES = 30.0  # allowlist'teki proc bile bu kadar uzun surerse warn (kill YOK)
HEARTBEAT_MAX_AGE_MINUTES = 10.0  # hook-state heartbeat bu kadar bayatsa stall
# Producer-dedup penceresi (klipper #100128 ortak emit_throttled): cron */3 -> ayni
# (type, source) olayi her 3dk RE-EMIT etme. 15dk pencere = devam-eden runaway/stall
# ~5 turda 1 emit (flood-bastir ama periyodik re-surface). events-tablosu age-sorgusu
# process-bagimsiz -> cron'un her-tur yeni-process'i de dedup eder (in-proc dict ise
# runs-arasi paylasilmaz, bkz app/core/emit_throttle.py docstring).
WATCHDOG_DEDUP_WINDOW_SECONDS = 900.0
# Sureklilik-state: per-PID high-CPU streak'i runs-arasi takip (cadvisor-FP-fix 2026-06-23).
# Tek-cron-turu high-CPU process-uptime'a bakamaz (mesru-daemon spike'i = sahte-runaway);
# bunun yerine "kac dk-dir SUREKLI >=esik" olculur. data/hook-state/ icinde JSON.
CPU_STREAK_FILE = "watchdog-cpu-streak.json"

# Mesru uzun-CPU proc'lari (cmdline-substring, case-insensitive). ASLA auto-kill edilmez.
ALLOWLIST_PATTERNS: tuple[str, ...] = (
    "pytest",
    "ruff",
    "mypy",
    "npm",
    "node",
    "rsync",
    "backup",
    "test-runner",
    "run-all-tests",
    "klipper-cron-wrap",
    "git",
    "uvicorn",
    "gunicorn",
    "ollama",
    "docker",
    "playwright",
)


def _autokill_enabled() -> bool:
    """AUTO_KILL gate — read_env_var (.env + os.environ), os.environ.get DEGIL (#174 sinifi;
    systemd EnvironmentFile gecmiyor -> os.environ.get .env'i goremez). DEFAULT KAPALI."""
    return (read_env_var("AGENT_WATCHDOG_AUTOKILL") or "0").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ProcSnapshot:
    """Bir process'in watchdog-anlik gorunumu."""

    pid: int
    name: str
    cmdline: str
    cpu_pct: float
    age_minutes: float


@dataclass
class HeartbeatStall:
    agent: str
    age_minutes: float
    path: str


@dataclass
class Verdict:
    """Bir runaway-aday icin karar."""

    snap: ProcSnapshot
    allowlisted: bool
    runaway: bool
    action: str  # ignore | notify | kill
    reasons: list[str] = field(default_factory=list)


def is_allowlisted(cmdline: str, name: str = "") -> bool:
    """cmdline/name mesru-uzun-CPU listesinde mi? (ASLA auto-kill)."""
    hay = f"{name} {cmdline}".lower()
    return any(p in hay for p in ALLOWLIST_PATTERNS)


def classify(
    snap: ProcSnapshot,
    *,
    sustained_minutes: float = 0.0,
    cpu_pct: float = RUNAWAY_CPU_PCT,
    min_minutes: float = RUNAWAY_MIN_MINUTES,
    warn_minutes: float = ALLOWLIST_WARN_MINUTES,
) -> Verdict:
    """Runaway siniflandirma (saf-mantik, test-edilebilir). FP-onleme klipper #100115.

    KRITIK (cadvisor-FP-fix 2026-06-23): runaway = SU AN core-pinned + bunu SURDURME suresi
    (sustained_minutes) >= min_minutes. Eski kod `snap.age_minutes` (process-UPTIME) kullaniyordu
    -> 12-gun-uptime'li mesru daemon (cadvisor/prometheus) ANLIK 1sn-ornekte >=%90 spike yaparsa
    sahte "runaway" CRITICAL page uretiyordu. sustained_minutes runs-arasi persistence ile
    olculur (bkz _compute_sustained); tek-anlik-spike sustained'a ulasmaz, gercek-core-pin ulasir
    (#207 anomaly-persistence ile ayni ilke). Default 0.0 = fail-safe (sustained-verisi yok -> ignore).

    - cpu < esik VEYA sustained < min -> ignore (mesru/gecici-spike).
    - core-pinned + sustained>=min:
        - allowlist'te -> notify yalniz sustained>=warn_minutes ise (asla kill).
        - allowlist-disi -> kill-adayi (gercek kill AUTO_KILL+cmdline-verify ile gate'li).
    """
    reasons: list[str] = []
    allow = is_allowlisted(snap.cmdline, snap.name)
    runaway = snap.cpu_pct >= cpu_pct and sustained_minutes >= min_minutes
    if not runaway:
        reasons.append(f"cpu={snap.cpu_pct:.0f}%<{cpu_pct:.0f} veya surekli={sustained_minutes:.0f}dk<{min_minutes:.0f}")
        return Verdict(snap, allow, False, "ignore", reasons)
    if allow:
        # mesru ama cok-uzun-SUREKLI: yalniz uyari (asla oldurme)
        if sustained_minutes >= warn_minutes:
            reasons.append(f"allowlist ama surekli {sustained_minutes:.0f}dk>={warn_minutes:.0f} -> warn (kill YOK)")
            return Verdict(snap, True, True, "notify", reasons)
        reasons.append("allowlist + surekli<warn -> ignore")
        return Verdict(snap, True, True, "ignore", reasons)
    reasons.append(
        f"net-runaway: cpu={snap.cpu_pct:.0f}%>={cpu_pct:.0f} + surekli={sustained_minutes:.0f}dk>={min_minutes:.0f} + allowlist-disi"
    )
    return Verdict(snap, False, True, "kill", reasons)


def check_heartbeat_stalls(
    hook_state_dir: str | Path,
    *,
    max_age_minutes: float = HEARTBEAT_MAX_AGE_MINUTES,
    now_ts: float | None = None,
) -> list[HeartbeatStall]:
    """data/hook-state/*.json heartbeat'lerini oku; max_age'den bayat olanlari dondur.

    code_review._write_heartbeat deseni: {"ts": ISO8601, ...}. now_ts test-edilebilirlik
    icin enjekte edilebilir (epoch saniye); None -> sistem-saati. Hata -> atla (fail-safe)."""
    import time
    from datetime import datetime

    base = Path(hook_state_dir)
    if not base.exists():
        return []
    now = now_ts if now_ts is not None else time.time()
    stalls: list[HeartbeatStall] = []
    for hb in sorted(base.glob("*.json")):
        try:
            data = json.loads(hb.read_text(encoding="utf-8"))
            # hook-state'te heartbeat-OLMAYAN json da var (ör. pending-notes.json = LIST) → dict
            # değilse atla; yoksa data.get('ts') AttributeError → TÜM stall-tarama çöker (fail-safe yutar).
            if not isinstance(data, dict):
                continue
            ts_raw = data.get("ts")
            if not ts_raw:
                continue
            ts = datetime.fromisoformat(str(ts_raw)).timestamp()
        except (OSError, ValueError, AttributeError, json.JSONDecodeError):
            continue
        age_min = (now - ts) / 60.0
        if age_min >= max_age_minutes:
            stalls.append(HeartbeatStall(agent=hb.stem, age_minutes=age_min, path=str(hb)))
    return stalls


def snapshot_processes(interval: float = 1.0, self_pid: int | None = None) -> list[ProcSnapshot]:
    """psutil ile anlik process gorunumu (cpu_percent interval-ornekli, 2-sample). self haric.
    psutil yoksa/hata -> [] (fail-safe). Linux-runtime; testler mock'lar."""
    try:
        import os
        import time as _t

        import psutil
    except Exception:
        return []
    me = self_pid if self_pid is not None else os.getpid()
    primed: list[Any] = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        if p.info["pid"] == me:
            continue
        try:
            p.cpu_percent(None)  # 1. ornek (priming)
            primed.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _t.sleep(interval)
    now = _t.time()
    out: list[ProcSnapshot] = []
    for p in primed:
        try:
            cpu = float(p.cpu_percent(None))  # 2. ornek -> interval-ortalama
            info = p.info
            age_min = max(0.0, (now - (info.get("create_time") or now)) / 60.0)
            cmd = " ".join(info.get("cmdline") or [])
            out.append(ProcSnapshot(pid=int(info["pid"]), name=info.get("name") or "", cmdline=cmd, cpu_pct=cpu, age_minutes=age_min))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def _compute_sustained(
    snaps: list[ProcSnapshot],
    *,
    state_dir: str | Path,
    now_ts: float,
    cpu_pct: float = RUNAWAY_CPU_PCT,
) -> dict[int, float]:
    """Per-PID SUREKLI-high-CPU suresi (dk), runs-arasi persistent state ile. cadvisor-FP-fix.

    Her tur: cpu>=esik proc'lar icin streak-baslangici (`since`) korunur; eski-tur'da bu PID
    yoksa veya cpu dustuyse streak KIRILIR (since=now). PID-reuse/restart guard: process'in
    YASI (age_minutes) iddia-edilen-streak'ten KUCUKSE (process streak'ten genc = imkansiz) ->
    since sifirlanir. cpu<esik proc'lar state'ten DUSER (streak kirildi). FAIL-SAFE: state
    okuma/yazma hatasi -> bos-state'le devam (tek-tur sustained=0, conservatif=emit-yok).

    Donus: {pid: sustained_minutes}. only-high proc'lar icin. state JSON'a yazilir."""
    path = Path(state_dir) / CPU_STREAK_FILE
    try:
        prev_raw = json.loads(path.read_text(encoding="utf-8"))
        prev: dict[str, Any] = prev_raw if isinstance(prev_raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        prev = {}
    new_state: dict[str, dict[str, float]] = {}
    sustained: dict[int, float] = {}
    for s in snaps:
        if s.cpu_pct < cpu_pct:
            continue  # high degil -> streak yok/kirildi (state'e yazilmaz -> dusar)
        key = str(s.pid)
        since = now_ts
        p = prev.get(key)
        if isinstance(p, dict):
            # #209-P2 (Codex): malformed `since` (null/string/hand-edit) -> float TypeError/ValueError
            # state-load-guard'ı DIŞINDA patlıyordu -> run_watchdog try'ı TÜM runaway-taramayı iptal
            # ediyordu (tek bozuk-PID = tüm-tespit durur). Per-entry guard: malformed -> since=now (fresh).
            try:
                cand_since = float(p.get("since", now_ts))
            except (TypeError, ValueError):
                cand_since = now_ts
            streak_min = (now_ts - cand_since) / 60.0
            # PID-reuse/restart guard: process en az streak kadar yasamis OLMALI (+0.5dk tolerans)
            if 0.0 <= streak_min <= s.age_minutes + 0.5:
                since = cand_since
        new_state[key] = {"since": since}
        sustained[s.pid] = max(0.0, (now_ts - since) / 60.0)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(new_state), encoding="utf-8")
    except OSError:
        # #209-P2 (Codex): state YAZILAMADIYSA stale-state'i KULLANMA -> boş-sustained dön (bu tur emit-yok,
        # konservatif). Yoksa eski-dosya kalır, düşüp-tekrar-spike yapan uzun-ömürlü PID sonraki turda
        # stale `since`'i devralıp sahte-sustained-runaway üretebilirdi.
        logger.warning("watchdog cpu-streak state yazilamadi -> bu tur sustained={} (stale-state kullanilmaz)")
        return {}
    return sustained


def _verify_and_kill(snap: ProcSnapshot, *, dry_run: bool) -> dict[str, object]:
    """/proc/PID/cmdline dogrula-SONRA-kill (per-PID; mass-kill YOK, PID-reuse koruma).
    Graduated: SIGTERM -> bekle -> SIGKILL. dry_run -> sadece niyet (provenance). Linux."""
    import os
    import signal
    import time as _t

    prov: dict[str, object] = {
        "pid": snap.pid,
        "name": snap.name,
        "cmdline": snap.cmdline[:200],
        "cpu_pct": snap.cpu_pct,
        "age_minutes": round(snap.age_minutes, 1),
    }
    try:
        live = Path(f"/proc/{snap.pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        prov["result"] = "vanished"
        return prov
    # cmdline ilk-token eslesmesi -> PID-reuse'da yanlis-proc oldurme
    first = snap.cmdline.split()[0] if snap.cmdline.strip() else ""
    if first and first not in live:
        prov["result"] = "cmdline-mismatch-skip"
        return prov
    if dry_run:
        prov["result"] = "dry_run-intent"
        return prov
    try:
        os.kill(snap.pid, signal.SIGTERM)
        _t.sleep(5)
        try:
            os.kill(snap.pid, 0)
            os.kill(snap.pid, 9)  # SIGKILL — numeric (Windows-mypy stub'inda signal.SIGKILL yok)
            prov["result"] = "SIGTERM+SIGKILL"
        except ProcessLookupError:
            prov["result"] = "SIGTERM"
    except (ProcessLookupError, PermissionError) as exc:
        prov["result"] = f"kill-fail:{type(exc).__name__}"
    return prov


def run_watchdog(hook_state_dir: str | Path = "data/hook-state") -> dict[str, int]:
    """Tek watchdog-tur: runaway-proc + heartbeat-stall -> events-spine (emit_throttled).
    Fail-safe (hicbir dal startup/cron'u bozmaz). Dondurur: ozet sayaclari.

    emit_throttled (klipper #100128): ayni (type, source) WATCHDOG_DEDUP_WINDOW icinde
    RE-EMIT edilmez (cron */3 flood-bastir); devam-eden runaway/stall periyodik re-surface.
    summary['suppressed'] = pencere-ici bastirilan emit sayisi."""
    import time as _t

    from app.core.emit_throttle import emit_throttled

    summary: dict[str, int] = {"runaways": 0, "stalls": 0, "killed": 0, "emitted": 0, "suppressed": 0}
    autokill = _autokill_enabled()
    try:
        snaps = snapshot_processes()
        now_ts = _t.time()
        # cadvisor-FP-fix: runaway = SUREKLI-high (process-uptime DEGIL). Persistent streak.
        sustained_map = _compute_sustained(snaps, state_dir=hook_state_dir, now_ts=now_ts)
        for snap in snaps:
            sustained = sustained_map.get(snap.pid, 0.0)
            v = classify(snap, sustained_minutes=sustained)
            if v.action == "ignore":
                continue
            summary["runaways"] += 1
            sev = "critical" if v.action == "kill" else "warn"
            payload: dict[str, object] = {
                "pid": snap.pid,
                "cpu_pct": snap.cpu_pct,
                "sustained_minutes": round(sustained, 1),
                "age_minutes": round(snap.age_minutes, 1),
                "allowlisted": v.allowlisted,
                "action": v.action,
                "reasons": v.reasons,
                "cmdline": snap.cmdline[:200],
            }
            if v.action == "kill":
                prov = _verify_and_kill(snap, dry_run=not autokill)
                payload["kill"] = prov
                if str(prov.get("result", "")).startswith(("SIGTERM", "SIGKILL")):
                    summary["killed"] += 1
            res = emit_throttled(
                type="agent-health",
                source=f"watchdog:proc:{snap.name or snap.pid}",
                title=f"runaway proc {snap.name} ({snap.cpu_pct:.0f}% {sustained:.0f}dk-surekli)",
                severity=sev,
                detail=" ; ".join(v.reasons),
                payload=payload,
                window_seconds=WATCHDOG_DEDUP_WINDOW_SECONDS,
            )
            if res.emitted:
                summary["emitted"] += 1
            elif res.suppressed:
                summary["suppressed"] += 1
    except Exception:
        logger.exception("watchdog runaway-tarama hatasi (fail-safe)")
    try:
        for st in check_heartbeat_stalls(hook_state_dir):
            summary["stalls"] += 1
            res = emit_throttled(
                type="agent-health",
                source=f"watchdog:heartbeat:{st.agent}",
                title=f"ajan heartbeat-stall: {st.agent} ({st.age_minutes:.0f}dk)",
                severity="warn",
                detail=f"{st.path} {st.age_minutes:.0f}dk bayat (esik {HEARTBEAT_MAX_AGE_MINUTES:.0f}dk)",
                payload={"agent": st.agent, "age_minutes": round(st.age_minutes, 1)},
                window_seconds=WATCHDOG_DEDUP_WINDOW_SECONDS,
            )
            if res.emitted:
                summary["emitted"] += 1
            elif res.suppressed:
                summary["suppressed"] += 1
    except Exception:
        logger.exception("watchdog heartbeat-tarama hatasi (fail-safe)")
    return summary
