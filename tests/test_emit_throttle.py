"""Tests for app/core/emit_throttle.py (DB-persistent dedup/throttle helper)."""

from __future__ import annotations

import sqlite3

from app.core import emit_throttle as et


def _events_db(tmp_path):
    """test_events.py ile aynı minimal events tablosu."""
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


def _count(db, type_, source):
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM events WHERE type=? AND source=?", (type_, source)).fetchone()[0]
    con.close()
    return n


def test_first_emit_is_novel(monkeypatch, tmp_path):
    db = _events_db(tmp_path)
    monkeypatch.setenv("DB_PATH", db)
    r = et.emit_throttled(type="exception", source="exception:E:m:f", title="t", severity="error")
    assert r.emitted is True
    assert r.novel is True
    assert r.suppressed is False
    assert isinstance(r.event_id, int)
    assert _count(db, "exception", "exception:E:m:f") == 1


def test_second_within_window_suppressed(monkeypatch, tmp_path):
    db = _events_db(tmp_path)
    monkeypatch.setenv("DB_PATH", db)
    et.emit_throttled(type="exception", source="s1", title="t", window_seconds=3600)
    r2 = et.emit_throttled(type="exception", source="s1", title="t", window_seconds=3600)
    assert r2.suppressed is True
    assert r2.emitted is False
    assert r2.novel is False
    assert _count(db, "exception", "s1") == 1  # ikinci yazılmadı (dedup)


def test_distinct_sources_independent(monkeypatch, tmp_path):
    db = _events_db(tmp_path)
    monkeypatch.setenv("DB_PATH", db)
    a = et.emit_throttled(type="exception", source="A", title="t", window_seconds=3600)
    b = et.emit_throttled(type="exception", source="B", title="t", window_seconds=3600)
    assert a.emitted is True
    assert b.emitted is True
    assert a.novel is True  # farklı source → ikisi de ilk-kez
    assert b.novel is True


def test_reemit_after_window(monkeypatch, tmp_path):
    db = _events_db(tmp_path)
    monkeypatch.setenv("DB_PATH", db)
    first = et.emit_throttled(type="exception", source="s2", title="t", window_seconds=600)
    # önceki olayı pencere-dışına backdate et → sonraki çağrı yeniden emit etmeli
    con = sqlite3.connect(db)
    con.execute("UPDATE events SET timestamp=datetime('now','-2 hours') WHERE id=?", (first.event_id,))
    con.commit()
    con.close()
    r = et.emit_throttled(type="exception", source="s2", title="t", window_seconds=600)
    assert r.emitted is True
    assert r.suppressed is False
    assert r.novel is False  # önceki var → bilinen tekrar, novel değil
    assert r.prior_age_seconds is not None
    assert r.prior_age_seconds > 600
    assert _count(db, "exception", "s2") == 2


def test_payload_enriched_with_novel_and_throttle(monkeypatch, tmp_path):
    db = _events_db(tmp_path)
    monkeypatch.setenv("DB_PATH", db)
    r = et.emit_throttled(type="exception", source="s3", title="t", payload={"k": "v"}, window_seconds=600)
    con = sqlite3.connect(db)
    payload = con.execute("SELECT payload FROM events WHERE id=?", (r.event_id,)).fetchone()[0]
    con.close()
    assert '"novel": true' in payload
    assert '"throttle"' in payload
    assert '"window_s": 600' in payload
    assert '"k": "v"' in payload  # çağıranın payload'ı korundu


def test_db_error_fail_open(monkeypatch, tmp_path):
    # events tablosu YOK → stats-sorgusu hata → fail-open (suppress ETME), crash yok.
    monkeypatch.setenv("DB_PATH", str(tmp_path / "missing.db"))
    r = et.emit_throttled(type="exception", source="s4", title="t")
    assert r.suppressed is False  # sahte-suppress yok (event kaybetme)
    assert r.emitted is False  # yazılamadı (tablo yok) ama crash yok
    assert r.novel is False  # sorgu başarısız → novel iddia etme
