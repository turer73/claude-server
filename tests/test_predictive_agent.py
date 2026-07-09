"""Tests for Predictive Agent."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation import predictive_agent


@pytest.fixture
def server_db(tmp_path: Path) -> str:
    """Geçici metrics_history tablosu oluştur."""
    db_path = tmp_path / "test_server.db"
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE metrics_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            cpu_usage REAL,
            memory_usage REAL,
            disk_usage REAL,
            temperature REAL,
            load_avg TEXT,
            network_io TEXT
        )
        """
    )
    con.commit()
    con.close()
    return str(db_path)


def _insert_metric(db_path: str, cpu: float, memory: float, disk: float, hours_ago: int = 0) -> None:
    """Test metrik kaydı ekle."""
    con = sqlite3.connect(db_path)
    ts = (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute(
        "INSERT INTO metrics_history (timestamp, cpu_usage, memory_usage, disk_usage) VALUES (?, ?, ?, ?)",
        (ts, cpu, memory, disk),
    )
    con.commit()
    con.close()


def test_no_metrics_returns_empty(server_db: str) -> None:
    """Veri yok → trend yok."""
    trends = predictive_agent.analyze_metrics(days=7, db_path=server_db)
    assert trends is not None
    assert len(trends) == 0


def test_stable_metrics_no_trend(server_db: str) -> None:
    """Sabit metrikler → trend yok."""
    for i in range(10):
        _insert_metric(server_db, cpu=50.0, memory=60.0, disk=70.0, hours_ago=i * 24)

    trends = predictive_agent.analyze_metrics(days=7, db_path=server_db)
    assert trends is not None
    assert len(trends) == 0


def test_increasing_disk_trend_detected(server_db: str) -> None:
    """Artan disk kullanımı → trend tespit edilir."""
    for i in range(10):
        _insert_metric(server_db, cpu=50.0, memory=60.0, disk=60.0 + i * 2.0, hours_ago=(9 - i) * 24)

    trends = predictive_agent.analyze_metrics(days=7, db_path=server_db)
    assert trends is not None
    assert len(trends) == 1
    assert trends[0]["metric"] == "disk_usage"
    assert trends[0]["current_value"] > 60.0
    assert trends[0]["trend_slope"] > 0


def test_increasing_cpu_trend_detected(server_db: str) -> None:
    """Artan CPU kullanımı → trend tespit edilir."""
    for i in range(10):
        _insert_metric(server_db, cpu=50.0 + i * 3.0, memory=60.0, disk=70.0, hours_ago=(9 - i) * 24)

    trends = predictive_agent.analyze_metrics(days=7, db_path=server_db)
    assert trends is not None
    assert any(t["metric"] == "cpu_usage" for t in trends)


def test_increasing_memory_trend_detected(server_db: str) -> None:
    """Artan memory kullanımı → trend tespit edilir."""
    for i in range(10):
        _insert_metric(server_db, cpu=50.0, memory=50.0 + i * 3.0, disk=70.0, hours_ago=(9 - i) * 24)

    trends = predictive_agent.analyze_metrics(days=7, db_path=server_db)
    assert trends is not None
    assert any(t["metric"] == "memory_usage" for t in trends)


def test_db_failure_returns_none(tmp_path: Path) -> None:
    """DB okuma hatası → None döner."""
    nonexistent = tmp_path / "nonexistent.db"
    trends = predictive_agent.analyze_metrics(days=7, db_path=str(nonexistent))
    assert trends is None


def test_format_trend_summary_empty() -> None:
    """Boş trend listesi → okunabilir mesaj."""
    summary = predictive_agent.format_trend_summary([])
    assert "Proaktif uyarı yok" in summary


def test_format_trend_summary_with_trends() -> None:
    """Trend listesi → formatlı özet."""
    trends = [
        {
            "metric": "disk_usage",
            "current_value": 75.0,
            "trend_slope": 1.5,
            "days_to_threshold": 6.67,
            "threshold": 85.0,
        }
    ]
    summary = predictive_agent.format_trend_summary(trends)
    assert "disk_usage" in summary
    assert "75.0" in summary
    assert "85" in summary


def test_linear_regression_basic() -> None:
    """Basit linear regression testi."""
    x = [0.0, 1.0, 2.0, 3.0, 4.0]
    y = [0.0, 2.0, 4.0, 6.0, 8.0]
    slope, intercept = predictive_agent._linear_regression(x, y)
    assert abs(slope - 2.0) < 0.01
    assert abs(intercept - 0.0) < 0.01


def test_linear_regression_empty() -> None:
    """Boş veri → slope=0."""
    slope, intercept = predictive_agent._linear_regression([], [])
    assert slope == 0.0
    assert intercept == 0.0


def test_linear_regression_single_point() -> None:
    """Tek nokta → slope=0."""
    slope, intercept = predictive_agent._linear_regression([1.0], [5.0])
    assert slope == 0.0
    assert intercept == 5.0
