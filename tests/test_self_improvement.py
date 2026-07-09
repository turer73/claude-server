"""Tests for Self-Improvement Agent."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation import self_improvement


@pytest.fixture
def memory_db(tmp_path: Path) -> str:
    """Geçici thoughts + discoveries tabloları oluştur."""
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
    con.execute(
        """
        CREATE TABLE discoveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            details TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    con.commit()
    con.close()
    return str(db_path)


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


def _insert_discovery(db_path: str, title: str, details: str) -> None:
    """Test discovery ekle."""
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO discoveries (project, type, title, details, status) VALUES ('test', 'learning', ?, ?, 'active')",
        (title, details),
    )
    con.commit()
    con.close()


def _insert_thought(db_path: str, focus: str, emotion: str, content: str, is_deep: int = 1) -> None:
    """Test thought ekle."""
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO thoughts (timestamp, focus, emotion, content, is_deep) VALUES (datetime('now'), ?, ?, ?, ?)",
        (focus, emotion, content, is_deep),
    )
    con.commit()
    con.close()


def _insert_remediation(db_path: str, source: str, success: int) -> None:
    """Test remediation ekle."""
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO remediation_log (alert_source, severity, mode, action, command, executed, success) "
        "VALUES (?, 'critical', 'auto', 'test-action', 'test-cmd', 1, ?)",
        (source, success),
    )
    con.commit()
    con.close()


def test_no_signals_returns_empty(memory_db: str, server_db: str) -> None:
    """Sinyal yok → boş dict."""
    signals = self_improvement.collect_improvement_signals(db_path=memory_db, server_db=server_db)
    assert signals is not None
    assert len(signals["patterns"]) == 0
    assert len(signals["low_success_playbooks"]) == 0
    assert len(signals["low_confidence"]) == 0


def test_collects_patterns(memory_db: str, server_db: str) -> None:
    """Pattern discovery'leri toplanır."""
    _insert_discovery(memory_db, "Tekrar Eden Pattern — 2026-07-09", "cron:fail 8 kez")

    signals = self_improvement.collect_improvement_signals(db_path=memory_db, server_db=server_db)
    assert signals is not None
    assert len(signals["patterns"]) == 1
    assert "Tekrar Eden Pattern" in signals["patterns"][0]["title"]


def test_collects_low_success_playbooks(memory_db: str, server_db: str) -> None:
    """Düşük başarı oranlı playbook'lar toplanır."""
    for i in range(5):
        _insert_remediation(server_db, "cpu_critical", 0)

    signals = self_improvement.collect_improvement_signals(db_path=memory_db, server_db=server_db)
    assert signals is not None
    assert len(signals["low_success_playbooks"]) == 1
    assert signals["low_success_playbooks"][0]["alert_source"] == "cpu_critical"


def test_collects_deep_thoughts(memory_db: str, server_db: str) -> None:
    """Deep thoughts toplanır."""
    _insert_thought(memory_db, "cron:fail", "concerned", "Cron fail oldu", is_deep=1)

    signals = self_improvement.collect_improvement_signals(db_path=memory_db, server_db=server_db)
    assert signals is not None
    assert len(signals["low_confidence"]) == 1
    assert "cron:fail" in signals["low_confidence"][0]["focus"]


def test_db_failure_returns_none(tmp_path: Path) -> None:
    """DB okuma hatası → None döner."""
    nonexistent = tmp_path / "nonexistent.db"
    signals = self_improvement.collect_improvement_signals(db_path=str(nonexistent), server_db=str(nonexistent))
    assert signals is None


def test_generate_suggestions_no_api_key() -> None:
    """API key yok → None döner."""
    signals = {"patterns": [], "low_success_playbooks": [], "low_confidence": []}
    suggestions = self_improvement.generate_improvement_suggestions(signals, ikey="")
    assert suggestions is None


def test_generate_suggestions_no_signals() -> None:
    """Sinyal yok → boş liste."""
    signals = {"patterns": [], "low_success_playbooks": [], "low_confidence": []}
    suggestions = self_improvement.generate_improvement_suggestions(signals, ikey="test-key")
    assert suggestions is not None
    assert len(suggestions) == 0
