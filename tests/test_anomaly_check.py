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


# ---- operasyonel-zemin guard (FP-fix 2026-06-22) ----

# idle-cpu baseline (~1%) + outlier: z-yüksek AMA mutlak-değer floor'a göre değişken.
_IDLE = [1.0, 1.2, 0.8, 1.1, 0.9, 1.0, 1.3, 0.7, 1.0, 1.1, 0.9, 1.2]


def test_floor_suppresses_benign_high():
    # cpu 1→8: istatistiksel-anomali (z>>5) AMA %8 < %70 floor → operasyonel-önemsiz → ATLA (FP-fix)
    out = ac.detect_anomalies({"cpu_usage": [*_IDLE, 8.0]})
    assert out == []


def test_floor_allows_real_high():
    # cpu 1→95: z-yüksek VE %95 >= %70 floor → GERÇEK anomali, ateşler
    out = ac.detect_anomalies({"cpu_usage": [*_IDLE, 95.0]})
    assert len(out) == 1
    assert out[0]["direction"] == "yüksek"


def test_memory_floor_aligned_to_config_75():
    # #205-P2 (Codex): memory floor 80→75 (config alert_memory_percent=75 hizalama).
    # mem 19→76: %76 >= %75 floor → artık GEÇER (eski 80-floor'da bastırılırdı, static-alert ile tutarsızdı)
    assert ac.ANOMALY_FLOORS["memory_usage"][0] == 75.0
    out76 = ac.detect_anomalies({"memory_usage": [*_IDLE, 76.0]})
    assert len(out76) == 1  # 76 >= 75 floor → flagged (korelasyon-yolu artık görür)
    # mem 19→74: %74 < %75 floor → hâlâ bastır (operasyonel-önemsiz)
    assert ac.detect_anomalies({"memory_usage": [*_IDLE, 74.0]}) == []


def test_low_direction_suppressed_for_resource():
    # cpu 50→1: düşük-yön (z<0); resource-low benign (low_floor=None) → ATLA (crash≠düşük-cpu)
    base = [50.0, 52.0, 48.0, 51.0, 49.0, 50.0, 53.0, 47.0, 50.0, 51.0, 49.0, 52.0]
    out = ac.detect_anomalies({"cpu_usage": [*base, 1.0]})
    assert out == []


def test_unknown_metric_no_floor_zonly():
    # FLOORS'ta olmayan metrik → floor-yok (z-tek-başına, geriye-uyumlu)
    out = ac.detect_anomalies({"load_metric": [*_IDLE, 8.0]})
    assert len(out) == 1  # floor olmadığı için z-anomali ateşler


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


# ---- persistence-gate (transient-spike filtresi 2026-06-22) ----


def _metrics_with_ts(tmp_path: Path, rows: list[tuple[str, float]]) -> None:
    """events + metrics_history kur; rows = [(iso_ts, cpu_value)] (açık timestamp'li)."""
    con = sqlite3.connect(tmp_path / "server.db")
    con.execute(
        "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT DEFAULT (datetime('now')), type TEXT, source TEXT, severity TEXT DEFAULT 'info', "
        "title TEXT, detail TEXT, payload TEXT, notified INTEGER DEFAULT 0)"
    )
    con.execute(
        "CREATE TABLE metrics_history (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, "
        "cpu_usage REAL, memory_usage REAL, disk_usage REAL, temperature REAL, load_avg TEXT, network_io TEXT)"
    )
    for ts, cpu in rows:
        con.execute("INSERT INTO metrics_history (timestamp, cpu_usage) VALUES (?, ?)", (ts, cpu))
    con.commit()
    con.close()


def _recent_ts(minutes_ago: float) -> str:
    import datetime as _dt

    return (_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=minutes_ago)).isoformat()


def test_persistence_transient_high_suppressed(monkeypatch, tmp_path):
    """Floor-üstü tek-tick (transient) spike → persistence-gate eler (emitted=0, transient=1).
    Klipper 2026-06-22 mem-%91-1-tick senaryosu."""
    rows = [(_recent_ts(25 - i), v) for i, v in enumerate(_BASE)]  # ~13..25dk varyanslı baseline (MAD>0)
    rows.append((_recent_ts(1.0), 20.0))  # önceki tick: floor-altı
    rows.append((_recent_ts(0.0), 95.0))  # latest tick: floor-üstü ama TEK
    _metrics_with_ts(tmp_path, rows)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    monkeypatch.delenv("ANOMALY_CHECK_ENABLED", raising=False)
    s = ac.run_anomaly_check()
    assert s["anomalies"] == 1  # detect yakaladı
    assert s["emitted"] == 0  # ama persistence eledi
    assert s["transient"] == 1
    assert _drift_rows(tmp_path) == []


def test_persistence_sustained_high_emits(monkeypatch, tmp_path):
    """Floor-üstü çoklu-ardışık-tick (sürekli) → persistence GEÇİRİR (emitted=1)."""
    rows = [(_recent_ts(25 - i), v) for i, v in enumerate(_BASE)]
    rows.append((_recent_ts(1.0), 92.0))  # önceki tick: floor-üstü
    rows.append((_recent_ts(0.0), 95.0))  # latest tick: floor-üstü → 2-tick sürekli
    _metrics_with_ts(tmp_path, rows)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    monkeypatch.delenv("ANOMALY_CHECK_ENABLED", raising=False)
    s = ac.run_anomaly_check()
    assert s["anomalies"] == 1
    assert s["emitted"] == 1
    assert s["transient"] == 0


def test_persistence_worker_dup_not_fooled(monkeypatch, tmp_path):
    """Worker-duplikasyonu (2 satır aynı saniye) transient'i sürekli gibi GÖSTERMEMELİ —
    saniye-granülü GROUP BY tek-tick'e indirir → transient eler."""
    rows = [(_recent_ts(25 - i), v) for i, v in enumerate(_BASE)]
    rows.append((_recent_ts(1.0), 20.0))  # önceki tick düşük (1 satır)
    spike_ts = _recent_ts(0.0)
    rows.append((spike_ts, 95.0))  # spike tick — worker-1
    rows.append((spike_ts, 95.0))  # AYNI saniye — worker-2 (duplikasyon)
    _metrics_with_ts(tmp_path, rows)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    monkeypatch.delenv("ANOMALY_CHECK_ENABLED", raising=False)
    s = ac.run_anomaly_check()
    assert s["emitted"] == 0  # 2 dup-satır = 1 tick → hâlâ transient
    assert s["transient"] == 1


def test_persisted_beyond_floor_failopen_insufficient(monkeypatch, tmp_path):
    """Yeterli-tick yok (yeni-başladı) → fail-open True (page-kaçırma > fazladan-page)."""
    _metrics_with_ts(tmp_path, [(_recent_ts(0.0), 95.0)])  # tek tick (<persist=2)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    assert ac._persisted_beyond_floor("cpu_usage", 70.0, None, is_high=True) is True


def test_persisted_beyond_floor_unknown_metric_trivial_true(monkeypatch, tmp_path):
    """Floor-yok metrik (high=-inf) → persistence uygulanmaz, trivial True (z-only geriye-uyumlu)."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))
    assert ac._persisted_beyond_floor("load_metric", float("-inf"), float("inf"), is_high=True) is True


def test_run_failsafe_on_error(monkeypatch, tmp_path):
    _events_db(tmp_path).close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))

    def _boom(*a, **k):
        raise RuntimeError("detect patladı")

    monkeypatch.setattr(ac, "detect_anomalies", _boom)
    s = ac.run_anomaly_check(series={"cpu_usage": [*_BASE, 95.0]})  # except yakalar
    assert s["emitted"] == 0
