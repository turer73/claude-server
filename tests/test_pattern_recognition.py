"""Tests for Pattern Recognition Agent."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone  # noqa: F401
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation import pattern_recognition


@pytest.fixture
def memory_db(tmp_path: Path) -> str:
    """Geçici thoughts tablosu oluştur."""
    db_path = tmp_path / "test_memory.db"
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE thoughts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            focus TEXT NOT NULL,
            emotion TEXT NOT NULL,
            content TEXT NOT NULL,
            source_data TEXT,
            is_deep INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    con.commit()
    con.close()
    return str(db_path)


def _insert_thought(db_path: str, focus: str, emotion: str, content: str, hours_ago: int = 0) -> None:
    """Test thought ekle."""
    con = sqlite3.connect(db_path)
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    con.execute(
        "INSERT INTO thoughts (timestamp, focus, emotion, content) VALUES (?, ?, ?, ?)",
        (ts, focus, emotion, content),
    )
    con.commit()
    con.close()


def test_no_patterns_below_threshold(memory_db: str) -> None:
    """Eşik altı tekrar → pattern yok."""
    for i in range(4):
        _insert_thought(memory_db, "idle", "calm", f"Test {i}")

    patterns = pattern_recognition.analyze_patterns(hours=24, threshold=5, db_path=memory_db)
    assert len(patterns) == 0


def test_pattern_detected_at_threshold(memory_db: str) -> None:
    """Eşik değerde tekrar → pattern tespit edilir."""
    for i in range(5):
        _insert_thought(memory_db, "cron:fail", "concerned", f"Cron failed {i}")

    patterns = pattern_recognition.analyze_patterns(hours=24, threshold=5, db_path=memory_db)
    assert len(patterns) == 1
    assert patterns[0]["focus"] == "cron:fail"
    assert patterns[0]["count"] == 5
    assert "concerned" in patterns[0]["emotions"]


def test_multiple_patterns(memory_db: str) -> None:
    """Birden fazla farklı focus → birden fazla pattern."""
    for i in range(3):
        _insert_thought(memory_db, "cron:fail", "concerned", f"Fail {i}")
        _insert_thought(memory_db, "alert:critical", "concerned", f"Alert {i}")
        _insert_thought(memory_db, "idle", "calm", f"Idle {i}")

    patterns = pattern_recognition.analyze_patterns(hours=24, threshold=3, db_path=memory_db)
    assert len(patterns) == 3
    assert {p["focus"] for p in patterns} == {"cron:fail", "alert:critical", "idle"}


def test_old_thoughts_excluded(memory_db: str) -> None:
    """24h dışındaki düşünceler hariç tutulur."""
    for i in range(3):
        _insert_thought(memory_db, "old:focus", "calm", f"Old {i}", hours_ago=48)
    for i in range(3):
        _insert_thought(memory_db, "new:focus", "calm", f"New {i}", hours_ago=1)

    patterns = pattern_recognition.analyze_patterns(hours=24, threshold=3, db_path=memory_db)
    assert len(patterns) == 1
    assert patterns[0]["focus"] == "new:focus"


def test_emotion_distribution(memory_db: str) -> None:
    """Duygu dağılımı doğru hesaplanır."""
    for i in range(3):
        _insert_thought(memory_db, "cron:fail", "concerned", f"Concerned {i}")
    for i in range(2):
        _insert_thought(memory_db, "cron:fail", "restless", f"Restless {i}")

    patterns = pattern_recognition.analyze_patterns(hours=24, threshold=5, db_path=memory_db)
    assert len(patterns) == 1
    assert patterns[0]["emotions"]["concerned"] == 3
    assert patterns[0]["emotions"]["restless"] == 2


def test_format_pattern_summary_empty() -> None:
    """Boş pattern listesi → okunabilir mesaj."""
    summary = pattern_recognition.format_pattern_summary([])
    assert "Tekrar eden pattern yok" in summary


def test_format_pattern_summary_with_patterns() -> None:
    """Pattern listesi → formatlı özet."""
    patterns = [
        {
            "focus": "cron:fail",
            "count": 8,
            "emotions": {"concerned": 5, "restless": 3},
            "samples": ["Sample content 1", "Sample content 2"],
        }
    ]
    summary = pattern_recognition.format_pattern_summary(patterns)
    assert "cron:fail" in summary
    assert "8 kez" in summary
    assert "concerned:5" in summary
