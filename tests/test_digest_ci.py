"""Tests for CI summary in app/core/digest.py — read, render, signal, staleness."""

from __future__ import annotations

import datetime as dt
import sqlite3

from app.core import digest as core_digest


def _make_coverage_db(tmp_path, runs):
    """Build a coverage.db-shaped sqlite file. `runs` is a list of run dicts
    with keys: id, timestamp, total, passed, failed, details."""
    import json

    path = tmp_path / "coverage.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE test_runs (id INTEGER PRIMARY KEY, timestamp TEXT, "
        "total_tests INTEGER, total_passed INTEGER, total_failed INTEGER, status TEXT, details TEXT)"
    )
    for r in runs:
        db.execute(
            "INSERT INTO test_runs (id, timestamp, total_tests, total_passed, total_failed, status, details) "
            "VALUES (?,?,?,?,?,?,?)",
            (r["id"], r["timestamp"], r["total"], r["passed"], r["failed"],
             "pass" if r["failed"] == 0 else "fail", json.dumps(r["details"])),
        )
    db.commit()
    db.close()
    return str(path)


def _run(run_id, timestamp, total, passed, failed, details):
    return {"id": run_id, "timestamp": timestamp, "total": total,
            "passed": passed, "failed": failed, "details": details}


def test_ci_health_empty_when_no_db(monkeypatch, tmp_path):
    monkeypatch.setattr(core_digest, "COVERAGE_DB_PATH", str(tmp_path / "missing.db"))
    assert core_digest.ci_health() == {}


def test_ci_health_fresh_clean_run(monkeypatch, tmp_path):
    today = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    path = _make_coverage_db(tmp_path, [_run(62, today, 2396, 2396, 0,
        {"panola": {"status": "pass", "passed": 843, "failed": 0},
         "klipper": {"status": "pass", "passed": 777, "failed": 0}})])
    monkeypatch.setattr(core_digest, "COVERAGE_DB_PATH", path)
    ci = core_digest.ci_health()
    assert ci["total"] == 2396
    assert ci["failed"] == 0
    assert ci["stale"] is False
    assert ci["failing_projects"] == []


def test_ci_health_stale_and_failing(monkeypatch, tmp_path):
    old = (dt.datetime.now().astimezone() - dt.timedelta(days=39)).isoformat(timespec="seconds")
    path = _make_coverage_db(tmp_path, [_run(20, old, 100, 95, 5,
        {"panola": {"status": "pass", "passed": 50, "failed": 0},
         "kuafor": {"status": "fail", "passed": 45, "failed": 5}})])
    monkeypatch.setattr(core_digest, "COVERAGE_DB_PATH", path)
    ci = core_digest.ci_health()
    assert ci["age_days"] >= 38
    assert ci["stale"] is True
    assert ci["failed"] == 5
    assert [p["project"] for p in ci["failing_projects"]] == ["kuafor"]
    assert ci["failing_projects"][0]["total"] == 50


def test_ci_trend_growth_no_regression(monkeypatch, tmp_path):
    today = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    path = _make_coverage_db(tmp_path, [
        _run(61, today, 760, 760, 0, {"panola": {"passed": 760, "failed": 0}}),
        _run(62, today, 766, 766, 0, {"panola": {"passed": 766, "failed": 0}}),
    ])
    monkeypatch.setattr(core_digest, "COVERAGE_DB_PATH", path)
    ci = core_digest.ci_health()
    assert ci["regressions"] == []
    assert ci["trend"] == [{"project": "panola", "kind": "delta", "from": 760, "to": 766, "delta": 6}]


def test_ci_trend_regression_and_drop(monkeypatch, tmp_path):
    today = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    path = _make_coverage_db(tmp_path, [
        _run(61, today, 150, 150, 0,
             {"panola": {"passed": 100, "failed": 0}, "old-proj": {"passed": 50, "failed": 0}}),
        _run(62, today, 90, 90, 0, {"panola": {"passed": 90, "failed": 0}}),  # panola dropped, old-proj vanished
    ])
    monkeypatch.setattr(core_digest, "COVERAGE_DB_PATH", path)
    ci = core_digest.ci_health()
    kinds = {r["project"]: r["kind"] for r in ci["regressions"]}
    assert kinds == {"panola": "delta", "old-proj": "dropped"}


def test_trend_tokens_formatting():
    toks = core_digest._trend_tokens([
        {"project": "a", "kind": "delta", "from": 10, "to": 16, "delta": 6},
        {"project": "b", "kind": "delta", "from": 10, "to": 7, "delta": -3},
        {"project": "c", "kind": "new", "to": 5},
        {"project": "d", "kind": "dropped", "from": 9},
    ])
    assert toks == ["↑a +6", "↓b -3", "+c(yeni)", "⊘d"]


def test_has_signal_ci_stale():
    base = {
        "memory": {"new_bugs": [], "unread_notes": []},
        "commits": {}, "cron": {"self_pentest": None}, "system": {"service": "active"},
        "ci": {"failed": 0, "stale": True},
    }
    assert core_digest.has_signal(base) is True


def test_has_signal_ci_failing():
    base = {
        "memory": {"new_bugs": [], "unread_notes": []},
        "commits": {}, "cron": {"self_pentest": None}, "system": {"service": "active"},
        "ci": {"failed": 3, "stale": False},
    }
    assert core_digest.has_signal(base) is True


def test_has_signal_ci_clean_fresh_no_signal():
    base = {
        "memory": {"new_bugs": [], "unread_notes": []},
        "commits": {}, "cron": {"self_pentest": None}, "system": {"service": "active"},
        "ci": {"failed": 0, "stale": False},
    }
    assert core_digest.has_signal(base) is False


def test_render_includes_ci_line():
    d = {
        "memory": {"open_bugs": [], "new_bugs": [], "unread_notes": []},
        "commits": {}, "cron": {"self_pentest": None},
        "system": {"service": "active", "disk_used_pct": "10%", "disk_avail": "9G",
                   "mem_used_mb": "100", "mem_total_mb": "8000"},
        "ci": {"started_at": "2026-04-23 00:00:00", "age_days": 39, "stale": True,
               "total": 100, "passed": 95, "failed": 5,
               "failing_projects": [{"project": "kuafor", "passed": 45, "total": 50}],
               "trend": [{"project": "kuafor", "kind": "delta", "from": 50, "to": 45, "delta": -5}],
               "regressions": [{"project": "kuafor", "kind": "delta", "from": 50, "to": 45, "delta": -5}],
               "open_failures": []},
    }
    text = core_digest.render_text(d)
    html = core_digest.render_html(d)
    assert "CI: son run 2026-04-23" in text
    assert "BAYAT" in text
    assert "kuafor" in text
    assert "↓kuafor -5" in text  # trend line rendered
    assert "<b>CI:</b>" in html
    assert "↓kuafor -5" in html


def test_render_handles_missing_ci():
    d = {
        "memory": {"open_bugs": [], "new_bugs": [], "unread_notes": []},
        "commits": {}, "cron": {"self_pentest": None},
        "system": {"service": "active", "disk_used_pct": "10%", "disk_avail": "9G",
                   "mem_used_mb": "100", "mem_total_mb": "8000"},
    }
    assert "CI:" not in core_digest.render_text(d)
    assert "CI:" not in core_digest.render_html(d)
