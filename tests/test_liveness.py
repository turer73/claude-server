"""Tests for LIVESYS Faz 2 liveness monitor (app/core/liveness.py).

Gate: A-staleness yakalanır + B-sınıfı FALSE-POSITIVE üretmez (idle≠dead).
B-FP en kritik risk — özellikle 144-stale-pending tuzağı (surer ölçtü)."""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from app.core import liveness as lv


@pytest.fixture(autouse=True)
def _high_uptime(monkeypatch):
    """Boot-grace varsayılan-OFF: tüm staleness testleri uptime'ı eşiğin üstünde
    varsayar (gerçek runner uptime'ı düşükse stale/dead -> 'unknown' = flaky).
    Boot-grace'i test eden testler kendi monkeypatch'iyle bunu ezer (son setattr kazanır)."""
    monkeypatch.setattr(lv, "_uptime_s", lambda: 10**9)


def _set_mtime(path, age_s):
    t = time.time() - age_s
    os.utime(path, (t, t))


def _cron_db(path, rows):
    """server.db-shaped: cron_outcomes (job,result,timestamp via sqlite age modifier)."""
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT DEFAULT (datetime('now')), job TEXT, result TEXT, rc INTEGER, "
        "source TEXT, detail TEXT, attempt_no INTEGER DEFAULT 1)"
    )
    for job, result, age in rows:
        db.execute(
            "INSERT INTO cron_outcomes (timestamp, job, result, rc, source) VALUES (datetime('now', ?), ?, ?, 0, 'predicate')",
            (age, job, result),
        )
    db.commit()
    db.close()


# ── _verdict (A-staleness çekirdeği) ──


def test_verdict_fresh_stale_dead_unknown(monkeypatch):
    # uptime'ı eşiğin çok üstüne sabitle: boot-grace karışmasın (düşük-uptime CI
    # runner'da aksi halde stale/dead -> 'unknown'a düşer = flaky).
    monkeypatch.setattr(lv, "_uptime_s", lambda: 10**9)
    assert lv._verdict(10, 300)[0] == "alive"
    assert lv._verdict(500, 300)[0] == "stale"  # 1-3× eşik
    assert lv._verdict(5000, 300)[0] == "dead"  # >3× eşik
    assert lv._verdict(None, 300)[0] == "unknown"


def test_uptime_s_reads_proc(monkeypatch):
    monkeypatch.undo()  # autouse _high_uptime'ı geri al — gerçek _uptime_s'i test et
    up = lv._uptime_s()
    assert up is None or (isinstance(up, float) and up >= 0)  # Linux'ta float, non-Linux'ta None


def test_uptime_s_returns_none_on_read_error(monkeypatch):
    import builtins

    monkeypatch.undo()  # autouse _high_uptime'ı geri al — gerçek _uptime_s'i test et

    def _boom(*a, **k):
        raise OSError("boom")

    monkeypatch.setattr(builtins, "open", _boom)
    assert lv._uptime_s() is None


def test_verdict_boot_grace_suppresses_stale_and_dead(monkeypatch):
    # Makine yeni açıldı (uptime < eşik): bayat-ama-üretici-koşamadı -> 'unknown'.
    monkeypatch.setattr(lv, "_uptime_s", lambda: 120)  # 2dk uptime
    assert lv._verdict(500, 300)[0] == "unknown"  # normalde stale
    assert lv._verdict(5000, 300)[0] == "unknown"  # normalde dead
    # Taze veri grace'ten bağımsız hâlâ alive.
    assert lv._verdict(10, 300)[0] == "alive"
    # uptime eşiği aşınca (üretici koşma fırsatı buldu) gerçek verdict döner.
    monkeypatch.setattr(lv, "_uptime_s", lambda: 400)
    assert lv._verdict(500, 300)[0] == "stale"
    # /proc/uptime okunamazsa (None) grace devre-dışı = eski davranış.
    monkeypatch.setattr(lv, "_uptime_s", lambda: None)
    assert lv._verdict(5000, 300)[0] == "dead"


def test_verdict_boot_grace_capped_for_long_cadence(monkeypatch):
    # Codex P2: uzun-kadanslı kaynak (ci eşik=2g) reboot-içi penceresinde gerçekten
    # -ölü'yü maskelemesin. Grace tavanı BOOT_GRACE_CAP_S (1h).
    two_days = 2 * 86400
    nine_days = 9 * 86400  # gerçekten ölü
    # uptime tavanın altında (30dk): hâlâ grace.
    monkeypatch.setattr(lv, "_uptime_s", lambda: 1800)
    assert lv._verdict(nine_days, two_days)[0] == "unknown"
    # uptime tavanı aşmış (2h) ama eşiğin çok altında: grace BİTTİ -> gerçek dead.
    monkeypatch.setattr(lv, "_uptime_s", lambda: 7200)
    assert lv._verdict(nine_days, two_days)[0] == "dead"


# ── A-sınıfı: staleness yakala ──


def test_ci_liveness_fresh_vs_stale(monkeypatch, tmp_path):
    p = tmp_path / "coverage.db"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE test_runs (id INTEGER PRIMARY KEY, timestamp TEXT)")
    db.execute("INSERT INTO test_runs (timestamp) VALUES (datetime('now'))")
    db.commit()
    db.close()
    monkeypatch.setattr(lv, "COVERAGE_DB", str(p))
    assert lv.ci_liveness()["status"] == "alive"
    # 3 gün eski → dead (>2g eşik ×3)
    db = sqlite3.connect(p)
    db.execute("UPDATE test_runs SET timestamp = datetime('now','-9 days')")
    db.commit()
    db.close()
    assert lv.ci_liveness()["status"] == "dead"


def test_cron_job_fresh_pass_partial_old_missing(monkeypatch, tmp_path):
    p = tmp_path / "server.db"
    _cron_db(p, [("demo-reset-test", "pass", "-1 hour")])
    monkeypatch.setattr(lv, "SERVER_DB", str(p))
    assert lv.cron_job_liveness("demo-reset-test", 28 * 3600)["status"] == "alive"
    assert lv.cron_job_liveness("nonexistent", 3600)["status"] == "unknown"
    # taze ama partial → stale
    _cron_db(tmp_path / "s2.db", [("x", "partial", "-1 minute")])
    monkeypatch.setattr(lv, "SERVER_DB", str(tmp_path / "s2.db"))
    assert lv.cron_job_liveness("x", 3600)["status"] == "stale"


# ── B-sınıfı FALSE-POSITIVE disiplini (idle ≠ dead) ──


def test_notes_poller_fresh_alive_stale_dead(monkeypatch, tmp_path):
    p = tmp_path / "poller-state.json"
    p.write_text('{"last_seen_id": 1, "last_poll_at": "irrelevant"}')
    monkeypatch.setattr(lv, "POLLER_STATE", str(p))
    _set_mtime(p, 60)  # 1dk önce poll → canlı (note GELMESE bile)
    assert lv.notes_poller_liveness()["status"] == "alive"
    _set_mtime(p, 3600)  # 1h sessiz → daemon ölü
    assert lv.notes_poller_liveness()["status"] == "dead"


def test_alerts_evaluator_fresh_alive_stale_dead(monkeypatch, tmp_path):
    p = tmp_path / "alerts.log"
    p.write_text("[2026-06-02 14:00:00] OK cpu:10% mem:20%\n")
    monkeypatch.setattr(lv, "ALERTS_LOG", str(p))
    _set_mtime(p, 120)  # 2dk önce "OK" yazdı → canlı (0 alert olsa bile)
    assert lv.alerts_evaluator_liveness()["status"] == "alive"
    _set_mtime(p, 3600)  # 1h yazı yok → evaluator ölü
    assert lv.alerts_evaluator_liveness()["status"] == "dead"


def _memory_db(path, poison, old_pending, fresh_pending):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE spawn_failures (id INTEGER PRIMARY KEY, status TEXT, last_retry_at TEXT)")
    db.execute("CREATE TABLE tasks_log (id INTEGER PRIMARY KEY, status TEXT, created_at TEXT)")
    for _ in range(poison):
        db.execute("INSERT INTO spawn_failures (status) VALUES ('pending_retry')")
    for _ in range(old_pending):
        db.execute("INSERT INTO tasks_log (status, created_at) VALUES ('pending', datetime('now','-30 days'))")
    for _ in range(fresh_pending):
        db.execute("INSERT INTO tasks_log (status, created_at) VALUES ('pending', datetime('now','-10 minutes'))")
    db.commit()
    db.close()


def test_autonomy_144_stale_pending_no_false_positive(monkeypatch, tmp_path):
    """KRİTİK regresyon (surer FP-tuzağı): 144 STALE pending + 0 taze + retry-hb
    taze + poison≤5 → autonomy ALIVE (ham-count kullanılmaz). idle≠dead."""
    s = tmp_path / "server.db"
    _cron_db(s, [("autonomous-retry", "pass", "-5 minutes")])  # retry-hb taze
    m = tmp_path / "memory.db"
    _memory_db(m, poison=2, old_pending=144, fresh_pending=0)
    monkeypatch.setattr(lv, "SERVER_DB", str(s))
    monkeypatch.setattr(lv, "MEMORY_DB", str(m))
    r = lv.autonomy_liveness()
    assert r["status"] == "alive", r  # 144 stale'e RAĞMEN FP yok
    assert "taze-backlog=0" in r["detail"]


def test_autonomy_dead_on_poison_flood(monkeypatch, tmp_path):
    s = tmp_path / "server.db"
    _cron_db(s, [("autonomous-retry", "pass", "-5 minutes")])
    m = tmp_path / "memory.db"
    _memory_db(m, poison=12, old_pending=0, fresh_pending=0)  # çözülmemiş poison birikimi
    monkeypatch.setattr(lv, "SERVER_DB", str(s))
    monkeypatch.setattr(lv, "MEMORY_DB", str(m))
    assert lv.autonomy_liveness()["status"] == "dead"


def test_autonomy_dead_on_retry_heartbeat_stale(monkeypatch, tmp_path):
    s = tmp_path / "server.db"
    _cron_db(s, [("autonomous-retry", "pass", "-3 hours")])  # retry-loop durmuş
    m = tmp_path / "memory.db"
    _memory_db(m, poison=0, old_pending=144, fresh_pending=0)
    monkeypatch.setattr(lv, "SERVER_DB", str(s))
    monkeypatch.setattr(lv, "MEMORY_DB", str(m))
    assert lv.autonomy_liveness()["status"] == "dead"


def test_rag_canary_alive_and_dead(monkeypatch):
    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(lv.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert lv.rag_canary_liveness()["status"] == "alive"

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(lv.urllib.request, "urlopen", _boom)
    assert lv.rag_canary_liveness()["status"] == "dead"  # canary-fail (idle değil)


def test_rag_canary_401_is_unknown_not_dead(monkeypatch):
    """Auth reddi (401/403) = HTTP katmanı canlı ama key sorunu → rag-ölü DEĞİL
    (unknown). Aksi halde auth-eksikliği kalıcı FP-dead üretirdi (canlı yakalandı)."""

    def _401(*a, **k):
        raise lv.urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(lv.urllib.request, "urlopen", _401)
    assert lv.rag_canary_liveness()["status"] == "unknown"


# ── VPS-A gate-ek regresyon ──


def test_vps_metrics_stale_probe_down(monkeypatch, tmp_path):
    """VPS-A gate: stale metrics + Tailscale-SSH-OK → status=stale, sebep=probe-down.
    "VPS ölü" dememe: collector durmuş, VPS canlı."""
    p = tmp_path / "server.db"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE vps_metrics_history (id INTEGER PRIMARY KEY, timestamp TEXT)")
    db.execute("INSERT INTO vps_metrics_history (timestamp) VALUES (datetime('now', '-15 minutes'))")
    db.commit()
    db.close()
    monkeypatch.setattr(lv, "SERVER_DB", str(p))
    monkeypatch.setattr(lv, "_localize_vps_failure", lambda: ("stale", "probe-down"))
    r = lv.vps_metrics_liveness()
    assert r["status"] == "stale", r
    assert "probe-down" in r["detail"], r


def test_vps_metrics_stale_tailscale_link_down(monkeypatch, tmp_path):
    """VPS-A gate: stale + Tailscale-FAIL + public-OK → status=dead, sebep=tailscale-link-down."""
    p = tmp_path / "server.db"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE vps_metrics_history (id INTEGER PRIMARY KEY, timestamp TEXT)")
    db.execute("INSERT INTO vps_metrics_history (timestamp) VALUES (datetime('now', '-15 minutes'))")
    db.commit()
    db.close()
    monkeypatch.setattr(lv, "SERVER_DB", str(p))
    monkeypatch.setattr(lv, "_localize_vps_failure", lambda: ("dead", "tailscale-link-down"))
    r = lv.vps_metrics_liveness()
    assert r["status"] == "dead", r
    assert "tailscale-link-down" in r["detail"], r


def test_backup_push_gate_ek(monkeypatch, tmp_path):
    """VPS-A gate: backup-push fail-row→dead, no-row→dead, pass-fresh→alive."""
    # fail row → dead
    p1 = tmp_path / "s1.db"
    _cron_db(p1, [("vps-backup-push", "fail", "-1 hour")])
    monkeypatch.setattr(lv, "SERVER_DB", str(p1))
    r = lv.cron_job_liveness("vps-backup-push", 16 * 3600, absent_status="dead")
    assert r["status"] == "dead", r

    # no row → dead (absent_status="dead")
    p2 = tmp_path / "s2.db"
    db = sqlite3.connect(p2)
    db.execute(
        "CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT, job TEXT, result TEXT, rc INTEGER, source TEXT, "
        "detail TEXT, attempt_no INTEGER DEFAULT 1)"
    )
    db.commit()
    db.close()
    monkeypatch.setattr(lv, "SERVER_DB", str(p2))
    r = lv.cron_job_liveness("vps-backup-push", 16 * 3600, absent_status="dead")
    assert r["status"] == "dead", r

    # fresh pass → alive
    p3 = tmp_path / "s3.db"
    _cron_db(p3, [("vps-backup-push", "pass", "-1 hour")])
    monkeypatch.setattr(lv, "SERVER_DB", str(p3))
    r = lv.cron_job_liveness("vps-backup-push", 16 * 3600, absent_status="dead")
    assert r["status"] == "alive", r


def test_check_all_shape(monkeypatch, tmp_path):
    # Tüm kaynaklar okunamasa bile check_all patlamamalı, dead/stale derlemeli.
    monkeypatch.setattr(lv, "SERVER_DB", str(tmp_path / "none.db"))
    monkeypatch.setattr(lv, "COVERAGE_DB", str(tmp_path / "none.db"))
    monkeypatch.setattr(lv, "MEMORY_DB", str(tmp_path / "none.db"))
    monkeypatch.setattr(lv, "POLLER_STATE", str(tmp_path / "none.json"))
    monkeypatch.setattr(lv, "ALERTS_LOG", str(tmp_path / "none.log"))
    monkeypatch.setattr(lv.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    out = lv.check_all()
    assert "results" in out
    assert isinstance(out["dead"], list)
    assert isinstance(out["stale"], list)


def test_notify_cron_liveness_enable_gate(monkeypatch):
    """Codex P2: NOTIFY_CRON_ENABLED!=true -> wrapper koşsa bile dead (teslim kapalı)."""
    monkeypatch.setattr(lv, "_env_flag", lambda k: "false")
    r = lv.notify_cron_liveness()
    assert r["status"] == "dead"
    assert r["source"] == "notify-cron"
    assert "KAPALI" in r["detail"]


def test_notify_cron_liveness_enabled_checks_recency(monkeypatch, tmp_path):
    """ENABLED=true -> cron_outcomes tazeliğine bakar; source sade 'notify-cron'."""
    monkeypatch.setattr(lv, "_env_flag", lambda k: "true")
    p = tmp_path / "s.db"
    _cron_db(p, [("notify-cron", "pass", "-5 minutes")])
    monkeypatch.setattr(lv, "SERVER_DB", str(p))
    r = lv.notify_cron_liveness(45 * 60)
    assert r["source"] == "notify-cron"
    assert r["status"] == "alive"


def test_env_flag_prefers_os_environ(monkeypatch):
    """Codex P2: env-var override .env'i kazanır (notify-cron script'iyle tutarlı)."""
    monkeypatch.setenv("NOTIFY_CRON_ENABLED", "true")
    assert lv._env_flag("NOTIFY_CRON_ENABLED") == "true"
    monkeypatch.setenv("NOTIFY_CRON_ENABLED", "false")
    assert lv.notify_cron_liveness()["status"] == "dead"  # env override -> dead
