"""Tests for app/core/correlation_check.py (gap-5 cross-source event korelasyon producer)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core import correlation_check as cc


def _events_db(tmp_path: Path) -> sqlite3.Connection:
    p = tmp_path / "server.db"
    con = sqlite3.connect(p)
    con.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT DEFAULT (datetime('now')), type TEXT, source TEXT, "
        "severity TEXT DEFAULT 'info', title TEXT, detail TEXT, payload TEXT, "
        "notified INTEGER DEFAULT 0)"
    )
    con.commit()
    return con


def _ev(source: str, type_: str = "anomaly") -> dict:
    return {"id": 1, "timestamp": "2026-06-22T10:00", "type": type_, "source": source, "severity": "warn", "title": "x"}


# ---- correlate (saf) ----


def test_correlate_single_source_no_incident():
    # Tek kaynak (yalın tekrar) → incident DEĞİL (korelasyon cross-source ister)
    evs = [_ev("anomaly:cpu_usage"), _ev("anomaly:cpu_usage")]
    assert cc.correlate(evs) is None


def test_correlate_two_sources_incident():
    evs = [_ev("anomaly:cpu_usage", "anomaly"), _ev("drift:sha", "drift")]
    inc = cc.correlate(evs)
    assert inc is not None
    assert inc["sources"] == ["anomaly:cpu_usage", "drift:sha"]
    assert sorted(inc["types"]) == ["anomaly", "drift"]
    assert inc["event_count"] == 2


def test_correlate_empty_none():
    assert cc.correlate([]) is None


def test_correlate_fingerprint_stable_and_order_independent():
    # Aynı kaynak-kümesi (sıra farklı) → AYNI fingerprint (dedup tutarlı)
    a = cc.correlate([_ev("drift:sha"), _ev("anomaly:cpu")])
    b = cc.correlate([_ev("anomaly:cpu"), _ev("drift:sha")])
    assert a is not None
    assert b is not None
    assert a["fingerprint"] == b["fingerprint"]
    # Farklı küme → farklı fingerprint
    c = cc.correlate([_ev("drift:sha"), _ev("anomaly:memory")])
    assert c is not None
    assert c["fingerprint"] != a["fingerprint"]


# ---- _read_signal_events (gürültü-hariç) ----


def test_read_signal_events_excludes_noise_and_incident(tmp_path, monkeypatch):
    con = _events_db(tmp_path)
    H = "datetime('now','-2 minutes')"
    # Sinyal-tipleri + watchdog → DAHİL; code-review/job-outcome + type=incident → HARİÇ
    con.execute(f"INSERT INTO events (timestamp,type,source,title) VALUES ({H},'anomaly','anomaly:cpu','x')")
    con.execute(f"INSERT INTO events (timestamp,type,source,title) VALUES ({H},'drift','drift:sha','x')")
    con.execute(f"INSERT INTO events (timestamp,type,source,title) VALUES ({H},'alert','watchdog:proc:foo','x')")
    con.execute(f"INSERT INTO events (timestamp,type,source,title) VALUES ({H},'alert','code-review:app/x.py','x')")  # gürültü
    con.execute(f"INSERT INTO events (timestamp,type,source,title) VALUES ({H},'job-outcome','cron:demo','x')")  # rutin
    con.execute(f"INSERT INTO events (timestamp,type,source,title) VALUES ({H},'incident','incident:abc','x')")  # recursive
    con.commit()
    con.close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    evs = cc._read_signal_events()
    srcs = sorted(e["source"] for e in evs)
    assert srcs == ["anomaly:cpu", "drift:sha", "watchdog:proc:foo"]  # gürültü+incident yok


def test_read_signal_events_failsafe_missing_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "yok.db"))
    assert cc._read_signal_events() == []  # tablo yok → fail-safe []


# ---- run_correlation_check (gerçek emit_throttled + events-DB) ----


def _incidents(tmp_path: Path):
    con = sqlite3.connect(tmp_path / "server.db")
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM events WHERE type='incident'").fetchall()
    con.close()
    return rows


def test_run_emits_incident_warn(tmp_path, monkeypatch):
    _events_db(tmp_path).close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    evs = [_ev("anomaly:cpu", "anomaly"), _ev("drift:sha", "drift")]
    s = cc.run_correlation_check(events=evs)
    assert s["incident"] == 1
    assert s["emitted"] == 1
    rows = _incidents(tmp_path)
    assert len(rows) == 1
    assert rows[0]["severity"] == "warn"
    assert rows[0]["source"].startswith("incident:")
    assert "2 ilişkili sinyal" in rows[0]["title"]


def test_run_no_incident_single_source(tmp_path, monkeypatch):
    _events_db(tmp_path).close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    s = cc.run_correlation_check(events=[_ev("anomaly:cpu")])
    assert s["incident"] == 0
    assert s["emitted"] == 0
    assert _incidents(tmp_path) == []


def test_run_dedups_same_incident(tmp_path, monkeypatch):
    _events_db(tmp_path).close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    evs = [_ev("anomaly:cpu", "anomaly"), _ev("drift:sha", "drift")]
    s1 = cc.run_correlation_check(events=evs)
    s2 = cc.run_correlation_check(events=evs)  # aynı kaynak-kümesi → suppress
    assert s1["emitted"] == 1
    assert s2["emitted"] == 0
    assert s2["suppressed"] == 1
    assert len(_incidents(tmp_path)) == 1  # tek satır


def test_run_disabled_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRELATION_CHECK_ENABLED", "0")
    s = cc.run_correlation_check(events=[_ev("a:1"), _ev("b:2")])
    assert s == {"signals": 0, "incident": 0, "emitted": 0, "suppressed": 0}


def test_run_failsafe_on_internal_error(monkeypatch):
    # correlate içte patlasa bile run fail-safe (summary döner, exception propagate ETMEZ)
    monkeypatch.setattr(cc, "correlate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    s = cc.run_correlation_check(events=[_ev("a:1"), _ev("b:2")])
    assert s["emitted"] == 0  # patladı ama summary döndü
