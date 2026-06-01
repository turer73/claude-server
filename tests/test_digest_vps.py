"""Tests for VPS metrics in app/core/digest.py — read, render, signal."""

from __future__ import annotations

import sqlite3

from app.core import digest as core_digest


def _make_vps_db(tmp_path, rows):
    """Create a server.db-shaped sqlite file with vps_metrics_history rows."""
    path = tmp_path / "server.db"
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE vps_metrics_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, online INTEGER, cpu_usage REAL, memory_usage REAL,
            disk_usage REAL, containers_total INTEGER, containers_up INTEGER
        )"""
    )
    db.executemany(
        "INSERT INTO vps_metrics_history (timestamp, online, cpu_usage, memory_usage, disk_usage, containers_total, containers_up) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    db.commit()
    db.close()
    return str(path)


def test_vps_health_empty_when_no_db(monkeypatch, tmp_path):
    monkeypatch.setattr(core_digest, "_server_db_path", lambda: str(tmp_path / "missing.db"))
    assert core_digest.vps_health() == {}


def test_vps_health_returns_latest(monkeypatch, tmp_path):
    path = _make_vps_db(
        tmp_path,
        [
            ("2026-06-01T10:00:00+00:00", 1, 10.0, 20.0, 15.0, 20, 20),
            ("2026-06-01T11:00:00+00:00", 1, 32.5, 35.6, 20.0, 20, 18),  # latest
        ],
    )
    monkeypatch.setattr(core_digest, "_server_db_path", lambda: path)
    v = core_digest.vps_health()
    assert v["online"] is True
    assert v["cpu"] == 32.5
    assert v["containers_up"] == 18  # most recent row wins


def test_has_signal_vps_offline():
    base = {
        "memory": {"new_bugs": [], "unread_notes": []},
        "commits": {},
        "cron": {"self_pentest": None},
        "system": {"service": "active"},
        "vps": {"online": False},
    }
    assert core_digest.has_signal(base) is True


def test_has_signal_vps_disk_critical():
    base = {
        "memory": {"new_bugs": [], "unread_notes": []},
        "commits": {},
        "cron": {"self_pentest": None},
        "system": {"service": "active"},
        "vps": {"online": True, "cpu": 5, "mem": 10, "disk": 95},
    }
    assert core_digest.has_signal(base) is True


def test_has_signal_vps_healthy_no_signal():
    base = {
        "memory": {"new_bugs": [], "unread_notes": []},
        "commits": {},
        "cron": {"self_pentest": None},
        "system": {"service": "active"},
        "vps": {"online": True, "cpu": 30, "mem": 35, "disk": 20},
    }
    assert core_digest.has_signal(base) is False


def test_render_includes_vps_line():
    d = {
        "memory": {"open_bugs": [], "new_bugs": [], "unread_notes": []},
        "commits": {},
        "cron": {"self_pentest": None},
        "system": {
            "service": "active", "disk_used_pct": "10%", "disk_avail": "9G",
            "mem_used_mb": "100", "mem_total_mb": "8000",
        },
        "vps": {"online": True, "cpu": 32.5, "mem": 35.6, "disk": 20.0, "containers_total": 20, "containers_up": 18},
    }
    text = core_digest.render_text(d)
    html = core_digest.render_html(d)
    assert "VPS:" in text and "18/20 container" in text
    assert "VPS:" in html and "18/20 container" in html


def test_render_handles_missing_vps():
    """gather() pre-this-change (or VPS unconfigured) → no 'vps' key must not crash."""
    d = {
        "memory": {"open_bugs": [], "new_bugs": [], "unread_notes": []},
        "commits": {},
        "cron": {"self_pentest": None},
        "system": {
            "service": "active", "disk_used_pct": "10%", "disk_avail": "9G",
            "mem_used_mb": "100", "mem_total_mb": "8000",
        },
    }
    assert "VPS" not in core_digest.render_text(d)
    assert "VPS" not in core_digest.render_html(d)
