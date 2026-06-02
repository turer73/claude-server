"""Tests for cron outcome-contract in app/core/digest.py — read, latest-per-job,
window, render, signal. (LIVESYS Faz 1; writes come from klipper-cron-wrap.sh.)"""

from __future__ import annotations

import sqlite3

from app.core import digest as core_digest


def _make_cron_db(tmp_path, rows):
    """Create a server.db-shaped sqlite file with cron_outcomes rows.

    Each row: (job, result, rc, source, detail, age) where `age` is a sqlite
    datetime modifier like '-1 hour' / '-2 days' (relative to now).
    """
    path = tmp_path / "server.db"
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE cron_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            job TEXT NOT NULL, result TEXT NOT NULL, rc INTEGER,
            source TEXT NOT NULL, detail TEXT, attempt_no INTEGER NOT NULL DEFAULT 1
        )"""
    )
    for job, result, rc, source, detail, age in rows:
        db.execute(
            "INSERT INTO cron_outcomes (timestamp, job, result, rc, source, detail) VALUES (datetime('now', ?), ?, ?, ?, ?, ?)",
            (age, job, result, rc, source, detail),
        )
    db.commit()
    db.close()
    return str(path)


def test_cron_outcomes_empty_when_no_db(monkeypatch, tmp_path):
    monkeypatch.setattr(core_digest, "_server_db_path", lambda: str(tmp_path / "missing.db"))
    assert core_digest.cron_outcomes_health() == {}


def test_cron_outcomes_latest_per_job_and_bad(monkeypatch, tmp_path):
    path = _make_cron_db(
        tmp_path,
        [
            ("demo-reset", "fail", 1, "predicate", "earlier", "-3 hours"),
            ("demo-reset", "pass", 0, "predicate", "123/123", "-1 hour"),  # latest demo wins
            ("test-runner", "partial", 0, "predicate", "passed=3600 failed=34", "-2 hours"),
        ],
    )
    monkeypatch.setattr(core_digest, "_server_db_path", lambda: path)
    out = core_digest.cron_outcomes_health()
    by_job = {j["job"]: j for j in out["jobs"]}
    assert by_job["demo-reset"]["result"] == "pass"  # MAX(id) per job
    assert by_job["test-runner"]["result"] == "partial"
    assert [j["job"] for j in out["bad"]] == ["test-runner"]


def test_cron_outcomes_excludes_stale(monkeypatch, tmp_path):
    path = _make_cron_db(
        tmp_path,
        [("backup", "fail", 1, "predicate", "old run", "-2 days")],  # outside 24h window
    )
    monkeypatch.setattr(core_digest, "_server_db_path", lambda: path)
    out = core_digest.cron_outcomes_health()
    assert out["jobs"] == []
    assert out["bad"] == []


def test_has_signal_cron_bad():
    base = {
        "memory": {"new_bugs": [], "unread_notes": []},
        "commits": {},
        "cron": {"self_pentest": None},
        "cron_jobs": {"jobs": [{"job": "x"}], "bad": [{"job": "x", "result": "partial"}]},
        "system": {"service": "active"},
    }
    assert core_digest.has_signal(base) is True


def test_has_signal_cron_all_pass_no_signal():
    base = {
        "memory": {"new_bugs": [], "unread_notes": []},
        "commits": {},
        "cron": {"self_pentest": None},
        "cron_jobs": {"jobs": [{"job": "x", "result": "pass"}], "bad": []},
        "system": {"service": "active"},
        "vps": {},
        "ci": {},
    }
    assert core_digest.has_signal(base) is False


_SYS = {"service": "active", "disk_used_pct": "10%", "disk_avail": "9G", "mem_used_mb": "100", "mem_total_mb": "8000"}


def test_render_includes_bad_cron_jobs():
    d = {
        "memory": {"open_bugs": [], "new_bugs": [], "unread_notes": []},
        "commits": {},
        "cron": {"self_pentest": None},
        "cron_jobs": {
            "jobs": [
                {"job": "test-runner", "result": "pass", "rc": 0, "source": "predicate", "detail": "passed=3634"},
                {"job": "demo-reset", "result": "partial", "rc": 0, "source": "predicate", "detail": "119/123 (4 fail)"},
            ],
            "bad": [{"job": "demo-reset", "result": "partial", "rc": 0, "source": "predicate", "detail": "119/123 (4 fail)"}],
        },
        "system": _SYS,
    }
    text = core_digest.render_text(d)
    html = core_digest.render_html(d)
    assert "demo-reset" in text
    assert "partial" in text
    assert "demo-reset" in html
    assert "partial" in html


def test_render_all_pass_cron_summary():
    d = {
        "memory": {"open_bugs": [], "new_bugs": [], "unread_notes": []},
        "commits": {},
        "cron": {"self_pentest": None},
        "cron_jobs": {"jobs": [{"job": "test-runner", "result": "pass", "rc": 0, "source": "predicate", "detail": "ok"}], "bad": []},
        "system": _SYS,
    }
    assert "hepsi pass" in core_digest.render_text(d)
    assert "1 iş pass" in core_digest.render_html(d)


def test_render_handles_missing_cron_jobs():
    d = {
        "memory": {"open_bugs": [], "new_bugs": [], "unread_notes": []},
        "commits": {},
        "cron": {"self_pentest": None},
        "system": _SYS,
    }
    # cron_jobs yoksa render patlamamali, cron satiri da basmamali
    assert "Cron iş" not in core_digest.render_text(d)
