"""Tests for LIVESYS Faz 3.2 event backbone (app/core/events.py)."""

from __future__ import annotations

import sqlite3

from app.core import events as ev


def _events_db(tmp_path):
    p = tmp_path / "server.db"
    con = sqlite3.connect(p)
    con.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT DEFAULT (datetime('now')), type TEXT, source TEXT, "
        "severity TEXT DEFAULT 'info', title TEXT, detail TEXT, payload TEXT, "
        "notified INTEGER DEFAULT 0)"
    )
    con.commit()
    con.close()
    return str(p)


def test_emit_event_inserts_and_validates(monkeypatch, tmp_path):
    monkeypatch.setattr(ev, "DB_PATH", _events_db(tmp_path))
    eid = ev.emit_event("job-outcome", "cron:demo-reset", "demo-reset partial", severity="warn", detail="119/123")
    assert isinstance(eid, int)
    # geçersiz severity -> info'ya düşer; eksik alan -> None
    assert ev.emit_event("x", "s", "t", severity="bogus") is not None
    assert ev.emit_event("", "s", "t") is None
    assert ev.emit_event("x", "", "t") is None


def test_recent_events_severity_filter(monkeypatch, tmp_path):
    monkeypatch.setattr(ev, "DB_PATH", _events_db(tmp_path))
    ev.emit_event("a", "s", "info-evt", severity="info")
    ev.emit_event("b", "s", "warn-evt", severity="warn")
    ev.emit_event("c", "s", "crit-evt", severity="critical")
    assert len(ev.recent_events(24)) == 3
    warn_plus = ev.recent_events(24, min_severity="warn")
    titles = {e["title"] for e in warn_plus}
    assert titles == {"warn-evt", "crit-evt"}  # info hariç


def test_pending_notifications_and_mark(monkeypatch, tmp_path):
    monkeypatch.setattr(ev, "DB_PATH", _events_db(tmp_path))
    ev.emit_event("a", "s", "info", severity="info")  # bildirilmez
    w = ev.emit_event("b", "s", "warn", severity="warn")
    c = ev.emit_event("c", "s", "crit", severity="critical")
    pend = ev.pending_notifications()
    assert {e["id"] for e in pend} == {w, c}  # info yok
    assert ev.mark_notified([w, c]) == 2
    assert ev.pending_notifications() == []  # hepsi notified


def test_recent_events_empty_on_missing_db(monkeypatch, tmp_path):
    monkeypatch.setattr(ev, "DB_PATH", str(tmp_path / "none.db"))
    assert ev.recent_events(24) == []
    assert ev.pending_notifications() == []
    assert ev.emit_event("x", "s", "t") is None  # yazılamaz
