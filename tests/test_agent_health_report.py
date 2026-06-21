"""automation/agent-health-report.py — ajan freshness sınıflandırma mantığı (mock-DB).

Saf fonksiyonlar: agent_freshness (data-driven cadence → healthy/stale/son-fail) + build_summary.
Canlı LLM/HTTP/Telegram test edilmez; sınıflandırma kuralları test edilir (bayat-fail dersi)."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ahr", ROOT / "automation" / "agent-health-report.py")
ahr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ahr)


@pytest.fixture
def srv_db(tmp_path):
    db = tmp_path / "server.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY, job TEXT, result TEXT, timestamp TEXT)")
    # healthy: günlük, 4 çalışma, sonuncu 1s önce, pass
    for i in (4, 3, 2, 1):
        conn.execute("INSERT INTO cron_outcomes (job,result,timestamp) VALUES ('daily-ok','pass',datetime('now',?))", (f"-{i} days",))
    conn.execute("INSERT INTO cron_outcomes (job,result,timestamp) VALUES ('daily-ok','pass',datetime('now','-1 hours'))")
    # stale: günlük cadence ama 5 gün koşmamış (overdue)
    for i in (20, 19, 18, 5):
        conn.execute("INSERT INTO cron_outcomes (job,result,timestamp) VALUES ('daily-stale','pass',datetime('now',?))", (f"-{i} days",))
    # son-fail: haftalık (cadence ~7g), periyodunda koştu (2g önce) ama son-sonuç fail
    for i in (16, 9, 2):
        r = "fail" if i == 2 else "pass"
        conn.execute("INSERT INTO cron_outcomes (job,result,timestamp) VALUES ('weekly-fail',?,datetime('now',?))", (r, f"-{i} days"))
    # dormant: tek çalışma 30 gün önce (az-veri + sessiz → stale)
    conn.execute("INSERT INTO cron_outcomes (job,result,timestamp) VALUES ('dormant','pass',datetime('now','-30 days'))")
    # garbage job (sayı) elenmeli
    conn.execute("INSERT INTO cron_outcomes (job,result,timestamp) VALUES ('90.7','26',datetime('now','-1 hours'))")
    conn.commit()
    conn.close()
    return str(db)


def _status(agents, job):
    return next((a["status"] for a in agents if a["job"] == job), None)


# expected= ile çağır (yoksa gerçek crontab okunur, test-jobları dışlanır)
_EXP = {"daily-ok", "daily-stale", "weekly-fail", "dormant"}


def test_freshness_classification(srv_db):
    agents = ahr.agent_freshness(srv_db, expected=_EXP)
    assert _status(agents, "daily-ok") == "healthy"
    assert _status(agents, "daily-stale") == "stale"  # periyodunda koşmadı = gerçek sorun
    assert _status(agents, "weekly-fail") == "son-fail"  # koştu ama son fail (haftalık → acil değil)
    assert _status(agents, "dormant") == "stale"  # az-veri + 14g+ sessiz


def test_garbage_job_excluded(srv_db):
    jobs = {a["job"] for a in ahr.agent_freshness(srv_db, expected=_EXP)}
    assert "90.7" not in jobs  # sayısal/garbage job elenir


def test_non_expected_kept_not_stale_alarmed(srv_db):
    # Codex#5+#176: expected-dışı job (retired/relay) rapordan DIŞLANMAZ (relay-koru) ama STALE-alarm
    # ALMAZ (managed-ajan değil). daily-stale expected-dışı → dahil ama stale değil.
    agents = ahr.agent_freshness(srv_db, expected={"daily-ok"})
    jobs = {a["job"] for a in agents}
    assert "daily-stale" in jobs  # relay/retired DIŞLANMADI (Codex#176)
    assert _status(agents, "daily-stale") != "stale"  # ama STALE-alarm yok (Codex#5)
    assert _status(agents, "daily-ok") == "healthy"  # expected → normal sınıflama


def test_parse_wrap_jobs_skips_comments():
    # Codex#176: yorum-satırındaki klipper-cron-wrap job'ı expected-sayılmamalı (retired→yanlış-STALE önle)
    text = "0 1 * * * /x/klipper-cron-wrap.sh aktif-job /x/a.sh\n# 0 2 * * * /x/klipper-cron-wrap.sh retired-job /x/r.sh\n"
    jobs = ahr._parse_wrap_jobs(text)
    assert "aktif-job" in jobs
    assert "retired-job" not in jobs  # yorum-satırı atlandı


def test_expected_but_never_ran_is_stale(srv_db):
    # Codex#2: beklenen ama cron_outcomes'ta HİÇ-satırı yok → STALE (bozuk/kapalı ajan yakalanır)
    agents = ahr.agent_freshness(srv_db, expected=_EXP | {"never-ran-agent"})
    a = next(x for x in agents if x["job"] == "never-ran-agent")
    assert a["status"] == "stale"
    assert a["runs"] == 0
    assert a["age_h"] is None


def test_build_summary_separates_stale_and_sonfail(srv_db):
    agents = ahr.agent_freshness(srv_db, expected=_EXP)
    findings = {"discoveries_active_total": 5, "discoveries_bug_by_project": {}, "alerts_unresolved": {}, "cron_fails_7d": {}}
    rep = ahr.build_summary(agents, findings)
    assert "STALE" in rep
    assert "SON-FAIL" in rep
    assert "daily-stale" in rep  # stale bölümünde
