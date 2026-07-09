"""Tests for Cross-Source Consolidation Agent."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation import cross_source_consolidation


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
    """Geçici ci_lesson_learned tablosu oluştur."""
    db_path = tmp_path / "test_server.db"
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE ci_lesson_learned (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_name TEXT NOT NULL,
            raw_error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
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


def _insert_ci_lesson(db_path: str, test_name: str, raw_error: str) -> None:
    """Test ci_lesson ekle."""
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO ci_lesson_learned (test_name, raw_error) VALUES (?, ?)",
        (test_name, raw_error),
    )
    con.commit()
    con.close()


def test_no_items_returns_empty(memory_db: str, server_db: str) -> None:
    """Veri yok → öğe yok."""
    items = cross_source_consolidation.collect_learning_items(db_path=memory_db, server_db=server_db)
    assert items is not None
    assert len(items) == 0


def test_collects_discoveries(memory_db: str, server_db: str) -> None:
    """Discoveries toplanır."""
    _insert_discovery(memory_db, "Test Discovery", "Test details")

    items = cross_source_consolidation.collect_learning_items(db_path=memory_db, server_db=server_db)
    assert items is not None
    assert len(items) == 1
    assert items[0]["source"] == "discovery"
    assert "Test Discovery" in items[0]["title"]


def test_collects_thoughts(memory_db: str, server_db: str) -> None:
    """Deep thoughts toplanır."""
    _insert_thought(memory_db, "cron:fail", "concerned", "Cron fail oldu", is_deep=1)

    items = cross_source_consolidation.collect_learning_items(db_path=memory_db, server_db=server_db)
    assert items is not None
    assert len(items) == 1
    assert items[0]["source"] == "thought"
    assert "cron:fail" in items[0]["title"]


def test_collects_ci_lessons(memory_db: str, server_db: str) -> None:
    """CI lessons toplanır."""
    _insert_ci_lesson(server_db, "test_example", "AssertionError")

    items = cross_source_consolidation.collect_learning_items(db_path=memory_db, server_db=server_db)
    assert items is not None
    assert len(items) == 1
    assert items[0]["source"] == "ci_lesson"
    assert "test_example" in items[0]["title"]


def test_cosine_identical_vectors() -> None:
    """Özdeş vektörler → cosine 1.0."""
    v = [1.0, 2.0, 3.0]
    assert cross_source_consolidation.cosine(v, v) == 1.0


def test_cosine_orthogonal_vectors() -> None:
    """Dik vektörler → cosine 0.0."""
    v1 = [1.0, 0.0]
    v2 = [0.0, 1.0]
    assert cross_source_consolidation.cosine(v1, v2) == 0.0


def test_cosine_empty_vectors() -> None:
    """Boş vektörler → cosine 0.0."""
    assert cross_source_consolidation.cosine([], []) == 0.0
    assert cross_source_consolidation.cosine([1.0], []) == 0.0


def test_cluster_empty() -> None:
    """Boş liste → küme yok."""
    clusters = cross_source_consolidation.cluster([], [])
    assert len(clusters) == 0


def test_cluster_single_item() -> None:
    """Tek öğe → küme yok (MIN_CLUSTER=2)."""
    items = [{"id": "1", "content": "test"}]
    vectors = [[1.0, 2.0]]
    clusters = cross_source_consolidation.cluster(items, vectors)
    assert len(clusters) == 0


def test_cluster_similar_items() -> None:
    """Benzer öğeler → küme oluşur."""
    items = [
        {"id": "1", "content": "test"},
        {"id": "2", "content": "test"},
    ]
    vectors = [[1.0, 0.0], [1.0, 0.0]]
    clusters = cross_source_consolidation.cluster(items, vectors, threshold=0.9)
    assert len(clusters) == 1
    assert len(clusters[0]) == 2
