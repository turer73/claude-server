"""Tests for LIVESYS Faz 2 liveness monitor (app/core/liveness.py).

Gate: A-staleness yakalanır + B-sınıfı FALSE-POSITIVE üretmez (idle≠dead).
B-FP en kritik risk — özellikle 144-stale-pending tuzağı (surer ölçtü)."""

from __future__ import annotations

import os
import sqlite3
import time

from app.core import liveness as lv


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


def test_verdict_fresh_stale_dead_unknown():
    assert lv._verdict(10, 300)[0] == "alive"
    assert lv._verdict(500, 300)[0] == "stale"  # 1-3× eşik
    assert lv._verdict(5000, 300)[0] == "dead"  # >3× eşik
    assert lv._verdict(None, 300)[0] == "unknown"


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
