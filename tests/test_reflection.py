"""Tests for Reflection Agent."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone  # noqa: F401
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation import reflection


@pytest.fixture
def server_db(tmp_path: Path) -> str:
    """Geçici remediation_log tablosu oluştur."""
    db_path = tmp_path / "test_server.db"
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE remediation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            alert_source TEXT NOT NULL,
            severity TEXT NOT NULL,
            mode TEXT NOT NULL,
            action TEXT NOT NULL,
            command TEXT NOT NULL,
            executed INTEGER NOT NULL,
            result TEXT,
            success INTEGER,
            verify_status TEXT,
            provenance TEXT
        )
        """
    )
    con.commit()
    con.close()
    return str(db_path)


def _insert_remediation(db_path: str, source: str, action: str, success: bool, days_ago: int = 0) -> None:
    """Test remediation kaydı ekle."""
    con = sqlite3.connect(db_path)
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    con.execute(
        "INSERT INTO remediation_log (timestamp, alert_source, severity, mode, action, command, executed, success) "
        "VALUES (?, ?, 'critical', 'auto', ?, 'test-cmd', 1, ?)",
        (ts, source, action, 1 if success else 0),
    )
    con.commit()
    con.close()


def test_no_playbooks_below_min_attempts(server_db: str) -> None:
    """Min attempts altı → analiz yok."""
    for i in range(2):
        _insert_remediation(server_db, "cpu_critical", f"Action {i}", True)

    playbooks = reflection.analyze_playbooks(days=30, min_attempts=3, db_path=server_db)
    assert len(playbooks) == 0


def test_playbook_analyzed_at_min_attempts(server_db: str) -> None:
    """Min attempts değerde → analiz yapılır."""
    for i in range(3):
        _insert_remediation(server_db, "cpu_critical", f"Action {i}", True)

    playbooks = reflection.analyze_playbooks(days=30, min_attempts=3, db_path=server_db)
    assert len(playbooks) == 1
    assert playbooks[0]["alert_source"] == "cpu_critical"
    assert playbooks[0]["total"] == 3
    assert playbooks[0]["success_rate"] == 1.0


def test_low_success_rate_detected(server_db: str) -> None:
    """Düşük başarı oranı → öneri üretilir."""
    for i in range(4):
        _insert_remediation(server_db, "memory_critical", f"Action {i}", False)
    for i in range(1):
        _insert_remediation(server_db, "memory_critical", f"Action {i+4}", True)

    playbooks = reflection.analyze_playbooks(days=30, min_attempts=3, db_path=server_db)
    recommendations = reflection.identify_recommendations(playbooks)

    assert len(recommendations) == 1
    assert recommendations[0]["issue"] == "low_success_rate"
    assert recommendations[0]["success_rate"] == 0.2


def test_high_success_rate_detected(server_db: str) -> None:
    """Yüksek başarı oranı → öneri üretilir."""
    for i in range(5):
        _insert_remediation(server_db, "disk_critical", f"Action {i}", True)

    playbooks = reflection.analyze_playbooks(days=30, min_attempts=5, db_path=server_db)
    recommendations = reflection.identify_recommendations(playbooks)

    assert len(recommendations) == 1
    assert recommendations[0]["issue"] == "high_success_rate"
    assert recommendations[0]["success_rate"] == 1.0


def test_old_remediations_excluded(server_db: str) -> None:
    """30 gün dışındaki kayıtlar hariç tutulur."""
    for i in range(3):
        _insert_remediation(server_db, "old:source", f"Old {i}", True, days_ago=31)
    for i in range(3):
        _insert_remediation(server_db, "new:source", f"New {i}", True, days_ago=1)

    playbooks = reflection.analyze_playbooks(days=30, min_attempts=3, db_path=server_db)
    assert len(playbooks) == 1
    assert playbooks[0]["alert_source"] == "new:source"


def test_format_recommendation_summary_empty() -> None:
    """Boş öneri listesi → okunabilir mesaj."""
    summary = reflection.format_recommendation_summary([])
    assert "Öneri yok" in summary


def test_format_recommendation_summary_with_recommendations() -> None:
    """Öneri listesi → formatlı özet."""
    recommendations = [
        {
            "alert_source": "cpu_critical",
            "issue": "low_success_rate",
            "success_rate": 0.2,
            "total": 10,
            "recommendation": "Düşük başarı, analiz gerekli.",
        }
    ]
    summary = reflection.format_recommendation_summary(recommendations)
    assert "cpu_critical" in summary
    assert "%20" in summary
    assert "Düşük başarı" in summary
