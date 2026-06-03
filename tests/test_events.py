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


def test_payload_non_json_native_does_not_crash(monkeypatch, tmp_path):
    # datetime/bytes/Path gibi JSON-native olmayan payload emit_event'i crash
    # ETMEMELI (best-effort sözleşmesi). default=str ile serialize edilmeli.
    import datetime as _dt

    monkeypatch.setattr(ev, "DB_PATH", _events_db(tmp_path))
    eid = ev.emit_event(
        "job-outcome", "cron:x", "ts payload", severity="warn",
        payload={"ts": _dt.datetime(2026, 6, 3, 5, 0), "raw": b"\x00\x01"},
    )
    assert isinstance(eid, int)  # crash yok, satır yazıldı
    import sqlite3 as _sq

    con = _sq.connect(ev.DB_PATH)
    row = con.execute("SELECT payload FROM events WHERE id=?", (eid,)).fetchone()
    con.close()
    assert "2026-06-03" in row[0]  # datetime str'e serialize oldu


def test_db_path_default_matches_app_init():
    # events.py emit/read, main.py'ın schema kurduğu AYNI fallback'i kullanmalı;
    # aksi halde DB_PATH-set-olmayan ortamda events sessizce drop olur (Codex #18 P2).
    from app.db.database import DEFAULT_DB_PATH

    assert ev.DB_PATH == DEFAULT_DB_PATH


def test_severity_alias_warning_is_notifyable(monkeypatch, tmp_path):
    # Mevcut alert vocabulary'si "warning"/"error" -> kanonik warn/critical olmalı;
    # aksi halde pending_notifications (warn/critical) bunları sessizce eler.
    monkeypatch.setattr(ev, "DB_PATH", _events_db(tmp_path))
    ev.emit_event("alert", "devops_agent", "esik asimi", severity="warning")
    ev.emit_event("alert", "alert-check", "kritik", severity="error")
    pend = ev.pending_notifications()
    sevs = sorted(e["severity"] for e in pend)
    assert sevs == ["critical", "warn"]  # warning->warn, error->critical; ikisi de bildirilir


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
