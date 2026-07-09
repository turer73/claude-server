"""Tests for Meta-Cognition Agent."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation import meta_cognition


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
    ts = (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute(
        "INSERT INTO thoughts (timestamp, focus, emotion, content) VALUES (?, ?, ?, ?)",
        (ts, focus, emotion, content),
    )
    con.commit()
    con.close()


def test_no_thoughts_returns_zero_confidence(memory_db: str) -> None:
    """Thought yok → confidence 0.0."""
    quality = meta_cognition.analyze_thought_quality(hours=24, db_path=memory_db)
    assert quality is not None
    assert quality["confidence_score"] == 0.0
    assert quality["total_thoughts"] == 0


def test_high_idle_calm_ratio_reduces_confidence(memory_db: str) -> None:
    """Yüksek idle/calm oranı → düşük confidence."""
    for i in range(20):
        _insert_thought(memory_db, "idle", "calm", "Her şey sakin", hours_ago=i)

    quality = meta_cognition.analyze_thought_quality(hours=24, db_path=memory_db)
    assert quality is not None
    assert quality["idle_calm_ratio"] > 0.8
    assert quality["confidence_score"] < 1.0
    assert any("High idle/calm" in issue for issue in quality["issues"])


def test_low_focus_diversity_reduces_confidence(memory_db: str) -> None:
    """Düşük focus çeşitliliği → düşük confidence."""
    for i in range(20):
        _insert_thought(memory_db, "cron:fail", "concerned", "Cron fail oldu", hours_ago=i)

    quality = meta_cognition.analyze_thought_quality(hours=24, db_path=memory_db)
    assert quality is not None
    assert quality["unique_focuses"] < 3
    assert any("Low focus diversity" in issue for issue in quality["issues"])


def test_emotion_monotony_reduces_confidence(memory_db: str) -> None:
    """Tek duygu → düşük confidence."""
    for i in range(20):
        _insert_thought(memory_db, f"focus:{i}", "concerned", "İçerik", hours_ago=i)

    quality = meta_cognition.analyze_thought_quality(hours=24, db_path=memory_db)
    assert quality is not None
    assert any("Emotion monotony" in issue for issue in quality["issues"])


def test_high_confidence_diverse_thoughts(memory_db: str) -> None:
    """Çeşitli düşünceler → yüksek confidence."""
    focuses = ["cron:fail", "alert:critical", "spawn:poison", "metric:cpu", "idle"]
    emotions = ["concerned", "restless", "busy", "calm"]

    for i in range(20):
        focus = focuses[i % len(focuses)]
        emotion = emotions[i % len(emotions)]
        _insert_thought(memory_db, focus, emotion, "Uzun içerik metni burada", hours_ago=i)

    quality = meta_cognition.analyze_thought_quality(hours=24, db_path=memory_db)
    assert quality is not None
    assert quality["confidence_score"] > 0.5


def test_db_failure_returns_none(tmp_path: Path) -> None:
    """DB okuma hatası → None döner."""
    nonexistent = tmp_path / "nonexistent.db"
    quality = meta_cognition.analyze_thought_quality(hours=24, db_path=str(nonexistent))
    assert quality is None


def test_format_quality_summary_empty() -> None:
    """Boş quality → okunabilir mesaj."""
    quality = {
        "confidence_score": 0.0,
        "total_thoughts": 0,
        "unique_focuses": 0,
        "issues": ["No thoughts in time window"],
    }
    summary = meta_cognition.format_quality_summary(quality)
    assert "0.00" in summary
    assert "No thoughts" in summary


def test_format_quality_summary_with_issues() -> None:
    """Sorunlu quality → formatlı özet."""
    quality = {
        "confidence_score": 0.4,
        "total_thoughts": 50,
        "unique_focuses": 2,
        "issues": ["High idle/calm ratio", "Low focus diversity"],
    }
    summary = meta_cognition.format_quality_summary(quality)
    assert "0.40" in summary
    assert "High idle/calm" in summary
    assert "Low focus" in summary
