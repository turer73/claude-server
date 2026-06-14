"""LIVESYS Faz 2 — liveness/tazelik meta-monitor (read-only observer).

Sistem her veri-kaynağı/işin canlı+taze olduğunu bilsin; ölen parçayı kendi
fark etsin. Çekirdek ayrım (false-positive'i bu belirler):

- A-sınıfı (kadans-tabanlı: cron/poll): staleness eşiği çalışır. Beklenen
  kadansından çok geç = stale/dead.
- B-sınıfı (olay-tetikli / on-demand: autonomy, alerts, rag, notes): "eski =
  arıza" YANLIŞ. Atıl/sakin meşrudur. Liveness = PROCESSOR-canlı kanıtı
  (heartbeat / canary / yaş-pencereli-backlog), organik-aktiviteden BAĞIMSIZ.
  B-alert YALNIZ: heartbeat-stale | canary-fail | taze-backlog/poison. Organik
  sessizlikte ASLA (rag 16-idle'i "ölü" sanmak gibi FP'yi önler).

Pure observer: hiçbir şeye yazmaz. status ∈ {alive, stale, dead, unknown}.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import sqlite3
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

SERVER_DB = "/opt/linux-ai-server/data/server.db"
COVERAGE_DB = "/opt/linux-ai-server/data/coverage.db"
MEMORY_DB = "/opt/linux-ai-server/data/claude_memory.db"
POLLER_STATE = "/opt/linux-ai-server/data/hook-state/poller-state.json"
ALERTS_LOG = "/var/log/linux-ai-server/alerts.log"
RAG_HEALTH_URL = "http://localhost:8420/api/v1/rag/health"
ENV_FILE = "/opt/linux-ai-server/.env"

VPS_TAILSCALE_IP = "100.126.113.23"
VPS_PUBLIC_IP = "194.163.134.239"

# Boot-grace tavanı (saniye): boot sonrası bayat-veri FP'sini bastırma penceresi
# bununla sınırlı. Tüm kısa-kadanslı üreticilerin (en uzunu notify-cron 45dk)
# bir kez koşmasına yeter; uzun-kadanslı kaynakları (ci=2g, vps-backup=16h) uzun
# süre susturmaz (Codex P2 — gerçekten-ölü maskeleme regresyonunu önler).
BOOT_GRACE_CAP_S = 3600.0


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _parse(ts: str | None) -> dt.datetime | None:
    """Parse an ISO-ish timestamp (with/without tz) to aware UTC."""
    if not ts:
        return None
    try:
        d = dt.datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        return d.astimezone(dt.UTC) if d.tzinfo else d.replace(tzinfo=dt.UTC)
    except (ValueError, AttributeError):
        return None


def _age_s(ts: str | None) -> float | None:
    d = _parse(ts)
    return None if d is None else (_now() - d).total_seconds()


def _uptime_s() -> float | None:
    """Sistem uptime (saniye, /proc/uptime). Boot-grace için: makine yeni açıldıysa
    kadans-tabanlı üreticiler henüz bir kez koşma fırsatı bulamamış olabilir →
    pre-boot verisi zorunlu olarak bayat, ama bu arıza DEĞİL. Okunamazsa (test/
    non-Linux) None → grace devre-dışı (eski davranış, güvenli)."""
    try:
        with open("/proc/uptime") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _file_age_s(path: str) -> float | None:
    """Dosya mtime yaşı (saniye). İçerik-timestamp tz-belirsiz olabildiği için
    (poller-state/alerts.log yerel-saat yazıyor) heartbeat tazeliğinde mtime
    kullan — epoch, tz-bağımsız, güvenilir."""
    try:
        return _now().timestamp() - Path(path).stat().st_mtime
    except OSError:
        return None


def _db_latest_ts(db: str, query: str) -> str | None:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(query).fetchone()
            return row[0] if row and row[0] is not None else None
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _verdict(age: float | None, threshold_s: float) -> tuple[str, str]:
    """A-class staleness verdict from an age in seconds."""
    if age is None:
        return "unknown", "kaynak/timestamp okunamadı"
    if age <= threshold_s:
        return "alive", f"taze ({int(age)}s ≤ {int(threshold_s)}s)"
    # Boot-grace: makine yeni açıldıysa kadans-tabanlı üretici daha bir kez
    # koşamamış olabilir; pre-boot verisi zorunlu olarak bayat ama arıza DEĞİL →
    # stale/dead yerine 'unknown' (sessiz). FP imzası: downtime > eşik (incident:
    # 12h kapalı, notify-cron eşiği 45dk). Grace penceresi BOOT_GRACE_CAP_S ile
    # SINIRLI (Codex P2): uzun-kadanslı kaynaklar (ci=2g, vps-backup=16h) için
    # tam-eşik grace gerçekten-ölü kaynağı reboot-içi pencerede maskelerdi — ama
    # oralarda downtime ≪ eşik olduğundan grace zaten gereksiz. Cap, kısa-kadanslı
    # üreticilerin (notify-cron/metrics/autonomy, eşik ≤45dk) boot sonrası bir kez
    # koşmasına yeter; sonrası gerçek verdict. Üretici taze yazınca grace kapanır.
    up = _uptime_s()
    grace_s = min(threshold_s, BOOT_GRACE_CAP_S)
    if up is not None and up < grace_s:
        return "unknown", f"boot-grace ({int(age)}s eski; uptime {int(up)}s < {int(grace_s)}s)"
    if age <= threshold_s * 3:
        return "stale", f"gecikti ({int(age)}s > {int(threshold_s)}s)"
    return "dead", f"ölü ({int(age)}s ≫ {int(threshold_s)}s)"


# ── VPS localization ─────────────────────────────────────────────────────


def _localize_vps_failure(
    tailscale_ip: str = VPS_TAILSCALE_IP,
    public_ip: str = VPS_PUBLIC_IP,
    timeout: float = 5.0,
) -> tuple[str, str]:
    """Stale VPS metrics → TCP probe to localize root cause. Read-only, bounded.

    Returns (status, reason):
      ("stale", "probe-down")         — VPS+link canlı, collector durdu
      ("dead",  "tailscale-link-down") — VPS canlı ama Tailscale koptu
      ("dead",  "vps-down")            — VPS erişilemiyor
    """
    try:
        with socket.create_connection((tailscale_ip, 22), timeout=timeout):
            pass
        return "stale", "probe-down"
    except OSError:
        pass
    try:
        with socket.create_connection((public_ip, 22), timeout=timeout):
            pass
        return "dead", "tailscale-link-down"
    except OSError:
        pass
    return "dead", "vps-down"


# ── A-sınıfı: kadans-tabanlı staleness ──────────────────────────────────


def metrics_liveness() -> dict:
    age = _age_s(_db_latest_ts(SERVER_DB, "SELECT MAX(timestamp) FROM metrics_history"))
    st, d = _verdict(age, 300)  # ~30s kadans, >5dk geç
    return {"source": "metrics_history", "klass": "A", "status": st, "detail": d}


def vps_metrics_liveness() -> dict:
    age = _age_s(_db_latest_ts(SERVER_DB, "SELECT MAX(timestamp) FROM vps_metrics_history"))
    st, d = _verdict(age, 600)  # ~150s kadans, >10dk geç
    if st == "stale":
        # VPS-A: stale → TCP probe ile kök-neden lokalize et (read-only, 5s).
        probe_st, reason = _localize_vps_failure()
        st = probe_st
        d = f"{d} | sebep={reason}"
    return {"source": "vps_metrics_history", "klass": "A", "status": st, "detail": d}


def ci_liveness() -> dict:
    age = _age_s(_db_latest_ts(COVERAGE_DB, "SELECT MAX(timestamp) FROM test_runs"))
    st, d = _verdict(age, 2 * 86400)  # günlük, >2g
    return {"source": "ci_test_runs", "klass": "A", "status": st, "detail": d}


def cron_job_liveness(job: str, cadence_s: float, absent_status: str = "unknown") -> dict:
    """A: wrapped cron'un cron_outcomes'taki son satır tazeliği + sonucu.

    absent_status: satır yoksa döndürülecek status (default "unknown"; kritik
    job'lar için "dead" kullan — hiç koşmamış = sorun).
    """
    row = None
    try:
        con = sqlite3.connect(f"file:{SERVER_DB}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT timestamp, result FROM cron_outcomes WHERE job=? ORDER BY id DESC LIMIT 1",
                (job,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        pass
    if not row:
        return {"source": f"cron:{job}", "klass": "A", "status": absent_status, "detail": "cron_outcomes satırı yok"}
    age = _age_s(row[0])
    st, d = _verdict(age, cadence_s)
    if st == "alive" and row[1] != "pass":  # taze ama sonuç kötü
        st = "stale" if row[1] == "partial" else "dead"
        d = f"son sonuç={row[1]} ({d})"
    return {"source": f"cron:{job}", "klass": "A", "status": st, "detail": d}


# ── B-sınıfı: processor-heartbeat / canary / yaş-pencereli-backlog ───────


def notes_poller_liveness(poll_interval_s: float = 30) -> dict:
    """B (self-heartbeat): note-poller daemon her poll'da poller-state.json
    last_poll_at günceller. Liveness = o tazelik (note-SAYISI değil). Atıl=meşru."""
    # last_poll_at içeriği yerel-saat (tz'siz) → mtime kullan (dosya her poll'da
    # OVERWRITE edilir, mtime = son poll, epoch/tz-bağımsız).
    if not Path(POLLER_STATE).exists():
        return {"source": "notes_poller", "klass": "B", "status": "unknown", "detail": "poller-state yok"}
    age = _file_age_s(POLLER_STATE)
    st, d = _verdict(age, poll_interval_s * 10)  # 30s → >5dk=dead
    return {"source": "notes_poller", "klass": "B", "status": st, "detail": f"heartbeat {d}"}


def _env_flag(key: str) -> str:
    """Flag oku: os.environ ÖNCE (Codex P2: env-var override .env'i kazanır — notify-cron
    script'iyle tutarlı), sonra .env dosyası. Bulunamazsa ''."""
    v = os.environ.get(key)
    if v:
        return v
    try:
        with open(ENV_FILE) as fh:
            for line in fh:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def notify_cron_liveness(cadence_s: float = 45 * 60) -> dict:
    """notify-cron = alarm TESLİM-yolu. Codex P2: ENABLE-GATE — NOTIFY_CRON_ENABLED!=true
    ise cron-wrap koşup cron_outcomes yazsa BİLE teslim YOK (script en başta exit 0) ->
    dead. Enable ise cron_outcomes tazeliği (>45dk=dead)."""
    if _env_flag("NOTIFY_CRON_ENABLED").lower() != "true":
        return {
            "source": "notify-cron",
            "klass": "B",
            "status": "dead",
            "detail": "NOTIFY_CRON_ENABLED!=true (teslim KAPALI — alarm gitmez)",
        }
    r = cron_job_liveness("notify-cron", cadence_s, absent_status="dead")
    r["source"] = "notify-cron"  # 'cron:notify-cron' -> sade etiket
    return r


def alerts_evaluator_liveness() -> dict:
    """B (self-heartbeat): alert-check.sh (*/5) her run alerts.log'a "OK ..."
    yazar (alert olmasa bile). Liveness = log son-satır tazeliği — alerts
    TABLOSU değil (o yalnız alert-anında yazılır → staleness=FP)."""
    # [TIMESTAMP] satır içeriği yerel-saat (tz'siz) → mtime kullan (her run
    # append eder, mtime = son run). 5dk kadans → >15dk=dead.
    if not Path(ALERTS_LOG).exists():
        return {"source": "alerts_evaluator", "klass": "B", "status": "unknown", "detail": "alerts.log yok"}
    st, d = _verdict(_file_age_s(ALERTS_LOG), 900)
    return {"source": "alerts_evaluator", "klass": "B", "status": st, "detail": f"heartbeat {d}"}


def autonomy_liveness(backlog_window_s: float = 7200) -> dict:
    """B: PRIMARY = autonomous-retry cron_outcomes heartbeat (15dk wrapped).
    SECONDARY = poison (spawn_failures çözülmemiş) + YAŞ-PENCERELİ backlog
    (taze<2h pending). HAM pending-count KULLANMA: 144-stale-yetim kayıt kalıcı
    FP üretir (surer ölçtü: bugün taze=0). idle (taze-iş-yok) = meşru."""
    hb = cron_job_liveness("autonomous-retry", 35 * 60)  # 15dk×2+margin
    poison = fresh_backlog = None
    try:
        con = sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True)
        try:
            poison = con.execute("SELECT COUNT(*) FROM spawn_failures WHERE status NOT IN ('resolved','obsolete','archived')").fetchone()[0]
            fresh_backlog = con.execute(
                "SELECT COUNT(*) FROM tasks_log WHERE status='pending' AND created_at > datetime('now', ?)",
                (f"-{int(backlog_window_s)} seconds",),
            ).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        pass
    status, bits = hb["status"], [f"retry-hb={hb['status']}"]
    if poison is not None and poison > 5:  # çözülmemiş poison birikimi
        status = "dead" if status == "alive" else status
        bits.append(f"poison={poison}")
    elif poison:
        bits.append(f"poison={poison}")
    if fresh_backlog is not None and fresh_backlog > 10:  # taze birikim > drain
        status = "stale" if status == "alive" else status
        bits.append(f"taze-backlog={fresh_backlog}")
    else:
        bits.append(f"taze-backlog={fresh_backlog or 0}")
    return {"source": "autonomy", "klass": "B", "status": status, "detail": " ".join(bits)}


def _memory_key() -> str:
    """X-Memory-Key (rag router verify_key bunu ister). .env'den runtime oku."""
    try:
        from app.core.config import read_env_var

        return read_env_var("MEMORY_API_KEY") or ""
    except Exception:
        return ""


def rag_canary_liveness(timeout: float = 3.0) -> dict:
    """B (canary): /rag/health aktif-prob. Organik query-sayısından DECOUPLE —
    idle=meşru, canary-OK=canlı, canary-fail=ölü. /rag/* router-level verify_key
    ister → X-Memory-Key gönder (yoksa 401 = auth-FP, rag-ölü DEĞİL)."""
    try:
        headers = {"User-Agent": "klipper-liveness/1", "X-Memory-Key": _memory_key()}
        req = urllib.request.Request(RAG_HEALTH_URL, headers=headers)  # noqa: S310
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = resp.status
        ok = 200 <= status < 300
        return {
            "source": "rag",
            "klass": "B",
            "status": "alive" if ok else "dead",
            "detail": f"canary http={status}",
        }
    except urllib.error.HTTPError as e:
        # 401/403 = HTTP katmanı canlı ama auth reddi (key yanlış/eksik) -> bu
        # rag-processor ölü demek DEĞİL; "unknown" (auth sorunu, ayrı mesele).
        if e.code in (401, 403):
            return {"source": "rag", "klass": "B", "status": "unknown", "detail": f"canary auth http={e.code}"}
        return {"source": "rag", "klass": "B", "status": "dead", "detail": f"canary http={e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"source": "rag", "klass": "B", "status": "dead", "detail": f"canary-fail: {type(e).__name__}"}


# ── Docker konteyner canary ──────────────────────────────────────────────

# Beklenen konteynerler + host'tan HTTP sağlık probu. URL'ler canlı doğrulandı
# (2026-06-12). uptime-kuma 302->dashboard döner; urllib redirect'i takip eder.
DOCKER_CONTAINERS = {
    "n8n": "http://127.0.0.1:5678/healthz",
    "qdrant": "http://127.0.0.1:6333/healthz",
    "grafana": "http://127.0.0.1:3030/api/health",
    "prometheus": "http://127.0.0.1:9090/-/healthy",
    "node-exporter": "http://127.0.0.1:9100/",
    "cadvisor": "http://127.0.0.1:9080/healthz",
    "dozzle": "http://127.0.0.1:9999/",
    "uptime-kuma": "http://127.0.0.1:3001/",
    "stirling-pdf": "http://127.0.0.1:8090/",
}
# Boot sonrası docker'a konteyner başlatma süresi tanı (compose start ~1dk).
DOCKER_BOOT_GRACE_S = 300.0


def docker_containers_liveness(timeout: float = 2.5) -> list[dict]:
    """B (canary): beklenen Docker konteynerleri — docker ps + host'tan HTTP probu.

    2026-06-12 incident: kernel-reboot sonrası grafana+stirling-pdf 'Exited',
    n8n ise 'Up (healthy)' AMA network/port-binding'siz kalktı — host'tan 38h
    erişilmez, SIFIR alarm (kör nokta). Ders: docker-status/healthcheck yeterli
    DEĞİL (container-içi koşar) → canlılık kanıtı = host'tan HTTP probu.
    Liste-döner; check_all extend eder (per-container edge-detection: yeni ölen
    her konteyner ayrı dead-set üyesi = ayrı alarm)."""
    up = _uptime_s()
    if up is not None and up < DOCKER_BOOT_GRACE_S:
        return [
            {
                "source": "docker",
                "klass": "B",
                "status": "unknown",
                "detail": f"boot-grace (uptime {int(up)}s < {int(DOCKER_BOOT_GRACE_S)}s)",
            }
        ]
    try:
        proc = subprocess.run(  # noqa: S603, S607 — sabit argv, shell yok
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        running = set(proc.stdout.split()) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        running = None
    if running is None:
        # docker ps başarısız = daemon ölü/erişilmez → tüm konteynerler fiilen
        # kapalı; tek toplu sinyal (9 ayrı dead spam'i yerine).
        return [{"source": "docker", "klass": "B", "status": "dead", "detail": "docker ps çalışmadı (daemon ölü?)"}]
    results = []
    for name, url in DOCKER_CONTAINERS.items():
        src = f"docker:{name}"
        if name not in running:
            results.append({"source": src, "klass": "B", "status": "dead", "detail": "konteyner çalışmıyor"})
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "klipper-liveness/1"})  # noqa: S310
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                code = resp.status
            st, d = ("alive", f"probe http={code}") if code < 400 else ("dead", f"probe http={code}")
        except urllib.error.HTTPError as e:
            st, d = "dead", f"probe http={e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # Çalışıyor görünüp probe'a cevap yok = tam incident imzası
            # (network/port-binding kopuk olabilir).
            st, d = "dead", f"çalışıyor ama probe-fail: {type(e).__name__} (port-binding kopuk?)"
        results.append({"source": src, "klass": "B", "status": st, "detail": d})
    return results


REGISTRY = [
    metrics_liveness,
    vps_metrics_liveness,
    ci_liveness,
    lambda: cron_job_liveness("vps-backup-push", 16 * 3600, absent_status="dead"),  # günlük; 16h→dead@48h (~2g)
    lambda: cron_job_liveness("demo-reset-test", 28 * 3600),
    # restore-test = "yedek çalışır mı" doğrulayıcısı; tamamen DURURSA (27 May–14 Haz
    # boşluğu gibi) kimse fark etmiyordu → registry'ye al. Günlük (03:20); 28h→4h grace.
    lambda: cron_job_liveness("restore-test", 28 * 3600, absent_status="dead"),
    # notify-cron = alarm TESLİM-yolu; ölürse/kapalıysa HİÇBİR alarm gitmez (kör).
    # enable-gate + */20 kadans tazeliği. Spine'ın kalbi; meta-monitor DIRECT izler.
    notify_cron_liveness,
    notes_poller_liveness,
    alerts_evaluator_liveness,
    autonomy_liveness,
    rag_canary_liveness,
    docker_containers_liveness,
]


def check_all() -> dict:
    """Tüm registry kaynaklarını tara. Dönen: {results, dead, stale}.
    dead/stale = aksiyon gerektiren; alive/unknown sessiz."""
    results = []
    for fn in REGISTRY:
        try:
            r = fn()
            # Liste dönen komponent (docker_containers) → düzleştir; her üye
            # ayrı source = per-container edge-detection.
            results.extend(r) if isinstance(r, list) else results.append(r)
        except Exception as e:  # bir kaynak patlasa diğerleri taransın
            results.append({"source": getattr(fn, "__name__", "?"), "klass": "?", "status": "unknown", "detail": str(e)[:80]})
    return {
        "results": results,
        "dead": [r for r in results if r["status"] == "dead"],
        "stale": [r for r in results if r["status"] == "stale"],
    }
