"""Tests for app/core/anomaly_check.py (gap-4 robust-statistical metric anomaly producer)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core import anomaly_check as ac

# ~20±2 civarı 13-örnek baseline + outlier latest (robust-z >> eşik).
_BASE = [20.0, 22.0, 19.0, 21.0, 20.0, 23.0, 18.0, 20.0, 21.0, 22.0, 19.0, 20.0, 21.0]


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


def _add_metrics_table(con: sqlite3.Connection, cpu_values: list[float]) -> None:
    con.execute(
        "CREATE TABLE metrics_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT DEFAULT (datetime('now')), cpu_usage REAL, memory_usage REAL, "
        "disk_usage REAL, temperature REAL, load_avg TEXT, network_io TEXT)"
    )
    for v in cpu_values:
        con.execute("INSERT INTO metrics_history (cpu_usage, memory_usage) VALUES (?, NULL)", (v,))
    con.commit()


def _drift_rows(tmp_path: Path):
    con = sqlite3.connect(tmp_path / "server.db")
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM events WHERE type='anomaly'").fetchall()
    con.close()
    return rows


# ---- robust_zscore ----


def test_robust_zscore_detects_outlier():
    z = ac.robust_zscore(_BASE, 95.0)
    assert z is not None
    assert z > ac.ANOMALY_MAD_THRESHOLD  # büyük pozitif sapma


def test_robust_zscore_flat_baseline_is_none():
    assert ac.robust_zscore([10.0, 10.0, 10.0], 50.0) is None  # MAD=0 → tespit-yok
    assert ac.robust_zscore([10.0], 50.0) is None  # <2 örnek → None


# ---- detect_anomalies ----


def test_detect_flags_outlier():
    out = ac.detect_anomalies({"cpu_usage": [*_BASE, 95.0]})
    assert len(out) == 1
    assert out[0]["metric"] == "cpu_usage"
    assert out[0]["direction"] == "yüksek"
    assert out[0]["robust_z"] >= ac.ANOMALY_MAD_THRESHOLD


def test_detect_normal_no_anomaly():
    out = ac.detect_anomalies({"cpu_usage": [*_BASE, 21.0]})  # latest normal
    assert out == []


def test_detect_skips_insufficient_samples():
    out = ac.detect_anomalies({"cpu_usage": [20.0, 95.0]})  # <min_samples
    assert out == []


# ---- _read_metric_series ----


def test_read_metric_series_collects_and_skips_none(monkeypatch, tmp_path):
    con = _events_db(tmp_path)
    _add_metrics_table(con, [10.0, 11.0, 12.0])
    con.close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    series = ac._read_metric_series()
    assert series["cpu_usage"] == [10.0, 11.0, 12.0]
    assert series["memory_usage"] == []  # hep NULL → atlandı


def test_read_metric_series_failsafe_no_table(monkeypatch, tmp_path):
    _events_db(tmp_path).close()  # metrics_history YOK
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    assert ac._read_metric_series() == {}  # sqlite hata → {} (fail-safe)


def test_read_metric_series_iso_timestamp_window(monkeypatch, tmp_path):
    """Codex #199: metrics_history ISO-T yazılır (monitor_agent isoformat); datetime(timestamp)
    ile TEMPORAL pencere. Eski leksik karşılaştırma 'T'>' ' → 48h-eski satırı da içeriyordu."""
    import datetime as _dt

    con = _events_db(tmp_path)
    con.execute(
        "CREATE TABLE metrics_history (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, "
        "cpu_usage REAL, memory_usage REAL, disk_usage REAL, temperature REAL, load_avg TEXT, network_io TEXT)"
    )
    now = _dt.datetime.now(_dt.UTC)
    old_iso = (now - _dt.timedelta(hours=48)).isoformat()  # 24h-pencere DIŞI
    recent_iso = (now - _dt.timedelta(hours=1)).isoformat()  # İÇ
    con.execute("INSERT INTO metrics_history (timestamp, cpu_usage) VALUES (?, ?)", (old_iso, 11.0))
    con.execute("INSERT INTO metrics_history (timestamp, cpu_usage) VALUES (?, ?)", (recent_iso, 22.0))
    con.commit()
    con.close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    series = ac._read_metric_series(hours=24)
    assert series["cpu_usage"] == [22.0]  # yalnız recent (old 48h ISO-T pencere-dışı, leksik-değil)


# ---- run_anomaly_check (gerçek emit_throttled + events-DB) ----


def test_run_emits_anomaly_warn(monkeypatch, tmp_path):
    _events_db(tmp_path).close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    monkeypatch.delenv("ANOMALY_CHECK_ENABLED", raising=False)
    s = ac.run_anomaly_check(series={"cpu_usage": [*_BASE, 95.0]})
    assert s["anomalies"] == 1
    assert s["emitted"] == 1
    rows = _drift_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["severity"] == "warn"
    assert rows[0]["source"] == "anomaly:cpu_usage"
    assert rows[0]["type"] == "anomaly"


def test_run_dedup_across_runs(monkeypatch, tmp_path):
    _events_db(tmp_path).close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    series = {"cpu_usage": [*_BASE, 95.0]}
    s1 = ac.run_anomaly_check(series=series)
    s2 = ac.run_anomaly_check(series=series)  # aynı anomali, pencere-içi
    assert s1["emitted"] == 1
    assert s2["emitted"] == 0
    assert s2["suppressed"] == 1
    assert len(_drift_rows(tmp_path)) == 1


def test_run_reads_db_when_no_series(monkeypatch, tmp_path):
    con = _events_db(tmp_path)
    _add_metrics_table(con, [*_BASE, 95.0])  # anomalili cpu serisi DB'de
    con.close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    s = ac.run_anomaly_check()  # series=None → DB-okur
    assert s["anomalies"] == 1
    assert s["emitted"] == 1


def test_run_disabled_gate(monkeypatch, tmp_path):
    _events_db(tmp_path).close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    monkeypatch.setenv("ANOMALY_CHECK_ENABLED", "0")
    s = ac.run_anomaly_check(series={"cpu_usage": [*_BASE, 95.0]})
    assert s["emitted"] == 0
    assert s["anomalies"] == 0


def test_run_failsafe_on_error(monkeypatch, tmp_path):
    _events_db(tmp_path).close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))

    def _boom(*a, **k):
        raise RuntimeError("detect patladı")

    monkeypatch.setattr(ac, "detect_anomalies", _boom)
    s = ac.run_anomaly_check(series={"cpu_usage": [*_BASE, 95.0]})  # except yakalar
    assert s["emitted"] == 0
