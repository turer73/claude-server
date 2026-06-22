"""Tests for app/core/drift_check.py (gap-8 deployed≠running SHA drift producer).

sha_drift: /health HTTP mock'lanır. run: gerçek emit_throttled + events-DB (dedup dahil).
config-drift KALDIRILDI (Codex #196: cron-wrap .env→os.environ no-op + main.py boot-audit redundant).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.core import drift_check as dc


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
    rows = con.execute("SELECT * FROM events WHERE type='drift'").fetchall()
    con.close()
    return rows


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._b = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._b

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> bool:
        return False


# ---- sha_drift ----


def test_sha_drift_stale_true(monkeypatch):
    monkeypatch.setattr(dc.urllib.request, "urlopen", lambda *a, **k: _Resp({"stale": True, "sha": "abc12345", "disk_sha": "def67890"}))
    d = dc.sha_drift()
    assert d is not None
    assert d["kind"] == "sha"
    assert d["running_sha"] == "abc12345"
    assert "restart" in d["detail"]


def test_sha_drift_not_stale_or_unknown_is_none(monkeypatch):
    monkeypatch.setattr(dc.urllib.request, "urlopen", lambda *a, **k: _Resp({"stale": False, "sha": "x"}))
    assert dc.sha_drift() is None  # stale=False → drift yok
    monkeypatch.setattr(dc.urllib.request, "urlopen", lambda *a, **k: _Resp({"stale": None, "sha": "x"}))
    assert dc.sha_drift() is None  # stale=None (belirlenemez) → drift İDDİA ETME


def test_sha_drift_unreachable_is_none(monkeypatch):
    def _boom(*a, **k):
        raise OSError("server down")

    monkeypatch.setattr(dc.urllib.request, "urlopen", _boom)
    assert dc.sha_drift() is None  # server-down → drift değil (liveness ayrı)


# ---- run_drift_check (gerçek emit_throttled + events-DB) ----


def test_run_emits_drift_warn(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", _events_db(tmp_path))
    monkeypatch.delenv("DRIFT_CHECK_ENABLED", raising=False)
    monkeypatch.setattr(dc, "sha_drift", lambda *a, **k: {"kind": "sha", "detail": "deployed≠running restart"})
    s = dc.run_drift_check()
    assert s["sha_drift"] == 1
    assert s["emitted"] == 1
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["severity"] == "warn"
    assert rows[0]["type"] == "drift"
    assert rows[0]["source"] == "drift:sha"


def test_run_no_drift_no_emit(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", _events_db(tmp_path))
    monkeypatch.setattr(dc, "sha_drift", lambda *a, **k: None)  # stale değil
    s = dc.run_drift_check()
    assert s["sha_drift"] == 0
    assert s["emitted"] == 0
    assert len(_rows(tmp_path)) == 0


def test_run_dedup_across_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", _events_db(tmp_path))
    monkeypatch.setattr(dc, "sha_drift", lambda *a, **k: {"kind": "sha", "detail": "restart gerekli"})
    s1 = dc.run_drift_check()
    s2 = dc.run_drift_check()  # aynı drift, pencere-içi
    assert s1["emitted"] == 1
    assert s2["emitted"] == 0
    assert s2["suppressed"] == 1
    assert len(_rows(tmp_path)) == 1  # persistent-drift re-emit YOK (dedup)


def test_run_disabled_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", _events_db(tmp_path))
    monkeypatch.setenv("DRIFT_CHECK_ENABLED", "0")
    monkeypatch.setattr(dc, "sha_drift", lambda *a, **k: {"kind": "sha", "detail": "x"})
    s = dc.run_drift_check()
    assert s["emitted"] == 0
    assert s["sha_drift"] == 0
    assert len(_rows(tmp_path)) == 0


def test_run_failsafe_on_internal_error(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", _events_db(tmp_path))

    def _boom(*a, **k):
        raise RuntimeError("sha_drift patladı")

    monkeypatch.setattr(dc, "sha_drift", _boom)
    s = dc.run_drift_check()  # except yakalamalı, crash YOK
    assert s["emitted"] == 0
