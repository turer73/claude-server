"""Tests for app/core/log_novelty.py (gap-3 Drain3 log-novelty producer).

Gerçek Drain3 (deterministik) + inject-lines (journalctl mock'lanmaz, lines param) + temp
FilePersistence state + gerçek events-DB. journalctl Linux-only → read_journal_lines ayrı
(Linux-verify klipper).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core import log_novelty as ln


def _events_db(tmp_path: Path) -> str:
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


def _rows(tmp_path: Path):
    con = sqlite3.connect(tmp_path / "server.db")
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM events WHERE type='log-novelty'").fetchall()
    con.close()
    return rows


# ---- saf birim ----


def test_is_interesting():
    assert ln.is_interesting("ERROR database connection failed")
    assert ln.is_interesting("Traceback (most recent call last):")
    assert ln.is_interesting("job FAILED exit 1")
    assert ln.is_interesting("CRITICAL kernel panic")
    assert not ln.is_interesting("INFO request completed 200 ok")
    assert not ln.is_interesting("user alice logged in successfully")


def test_redact_scrubs_pii():
    r = ln.redact("auth failed user=secret@example.com ip=192.168.1.50 id=123456789")
    assert "secret@example.com" not in r
    assert "192.168.1.50" not in r
    assert "123456789" not in r
    assert "<email>" in r
    assert "<ip>" in r


# ---- entegrasyon (gerçek Drain3) ----


def test_run_emits_novel_warn_and_filters_info(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", _events_db(tmp_path))
    monkeypatch.delenv("LOG_NOVELTY_ENABLED", raising=False)  # default ON
    state = str(tmp_path / "d3.bin")
    lines = [
        "ERROR database connection refused host db1",
        "INFO request completed 200 ok",  # error-ish DEĞİL → minelenmez
        "CRITICAL disk space exhausted partition root",
    ]
    s = ln.run_log_novelty(state_path=state, lines=lines)
    assert s["scanned"] == 3
    assert s["novel"] == 2  # 2 error-ish novel template (INFO filtrelendi)
    assert s["emitted"] == 2
    rows = _rows(tmp_path)
    assert len(rows) == 2
    assert all(r["severity"] == "warn" for r in rows)
    assert all(r["source"].startswith("log-novelty:") for r in rows)
    assert all(r["type"] == "log-novelty" for r in rows)


def test_cross_run_dedup_via_persisted_state(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", _events_db(tmp_path))
    state = str(tmp_path / "d3.bin")
    lines = ["ERROR disk full on device sda1"]
    s1 = ln.run_log_novelty(state_path=state, lines=lines)
    s2 = ln.run_log_novelty(state_path=state, lines=lines)  # AYNI satır, state persist
    assert s1["novel"] == 1
    assert s1["emitted"] == 1
    assert s2["novel"] == 0  # cross-run persist → tekrar-novel DEĞİL (save_state çalıştı)
    assert s2["emitted"] == 0
    assert len(_rows(tmp_path)) == 1  # ikinci tur events'e yazmadı


def test_kvkk_raw_pii_not_stored(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", _events_db(tmp_path))
    state = str(tmp_path / "d3.bin")
    # ilk-occurrence (cluster_created) Drain3-generalize DEĞİL → explicit redact şart
    ln.run_log_novelty(state_path=state, lines=["ERROR auth failed user secret-bob@example.com ip 10.0.0.5"])
    rows = _rows(tmp_path)
    assert len(rows) == 1
    blob = f"{rows[0]['title']} {rows[0]['detail']} {rows[0]['payload']}"
    assert "secret-bob@example.com" not in blob  # email maskelendi
    assert "10.0.0.5" not in blob  # ip maskelendi
    assert "<email>" in blob


def test_per_run_cap_suppresses_and_counts(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", _events_db(tmp_path))
    state = str(tmp_path / "d3.bin")
    # 3 yapısal-farklı error-satır (Drain3 ayrı küme) → max_emit=2 → 1 cap'lenir
    lines = [
        "ERROR alpha database unreachable",
        "CRITICAL beta memory exhausted now",
        "FATAL gamma kernel oops detected",
    ]
    s = ln.run_log_novelty(state_path=state, lines=lines, max_emit=2)
    assert s["novel"] == 3
    assert s["emitted"] == 2  # cap
    assert s["suppressed_cap"] == 1
    assert len(_rows(tmp_path)) == 2  # yalnız 2 emit (cap)


def test_disabled_gate_no_scan(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", _events_db(tmp_path))
    monkeypatch.setenv("LOG_NOVELTY_ENABLED", "0")
    s = ln.run_log_novelty(state_path=str(tmp_path / "d3.bin"), lines=["ERROR something broke badly"])
    assert s["scanned"] == 0
    assert s["emitted"] == 0
    assert len(_rows(tmp_path)) == 0


# ---- read_journal_lines (journalctl subprocess; Linux-only → mock'lanır) ----


def test_read_journal_lines_parses_and_rc_failsafe(monkeypatch):
    class _Ok:
        returncode = 0
        stdout = "line one\n\nERROR two\n"  # boş satır atlanmalı

    monkeypatch.setattr(ln.subprocess, "run", lambda *a, **k: _Ok())
    assert ln.read_journal_lines(since_min=5) == ["line one", "ERROR two"]

    class _Fail:
        returncode = 1
        stdout = "irrelevant"

    monkeypatch.setattr(ln.subprocess, "run", lambda *a, **k: _Fail())
    assert ln.read_journal_lines() == []  # rc!=0 → [] (fail-safe)


def test_read_journal_lines_oserror_returns_empty(monkeypatch):
    def _boom(*a, **k):
        raise OSError("journalctl yok (non-Linux)")

    monkeypatch.setattr(ln.subprocess, "run", _boom)
    assert ln.read_journal_lines() == []  # hata/non-Linux → [] (cron-bozmaz)


def test_run_failsafe_on_internal_error(monkeypatch, tmp_path):
    """Drain3/iç-hata cron'u ÇÖKERTMEZ (fail-safe except + logger.exception)."""
    monkeypatch.setenv("DB_PATH", _events_db(tmp_path))

    def _boom(*a, **k):
        raise RuntimeError("drain3 patladı")

    monkeypatch.setattr(ln, "_build_miner", _boom)
    s = ln.run_log_novelty(state_path=str(tmp_path / "d3.bin"), lines=["ERROR x broke"])
    assert s["scanned"] == 1  # scan oldu, sonra miner patladı → except yakaladı (crash yok)
    assert s["emitted"] == 0
    assert s["novel"] == 0
