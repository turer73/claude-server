"""agent_watchdog (gap-7) birim-testleri.

FP-onleme kilidi (klipper #100115 — yanlis-pozitif "felaketten beter"): mesru-proc
(pytest/ruff/...) ASLA kill; net-runaway kill-aday; allowlist-busy notify/ignore;
heartbeat-stall tespit; auto-kill default-OFF -> dry_run.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest

from app.core import agent_watchdog as aw
from app.core.agent_watchdog import (
    ProcSnapshot,
    check_heartbeat_stalls,
    classify,
    is_allowlisted,
)


def _snap(cpu: float = 95.0, age: float = 20.0, cmd: str = "python -c scan", name: str = "python") -> ProcSnapshot:
    return ProcSnapshot(pid=1234, name=name, cmdline=cmd, cpu_pct=cpu, age_minutes=age)


def _events_db(tmp_path: Path) -> str:
    """test_events.py ile aynı minimal events tablosu (emit_throttled buraya yazar)."""
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


def test_is_allowlisted() -> None:
    assert is_allowlisted("python -m pytest tests/")
    assert is_allowlisted("ruff check .")
    assert is_allowlisted("", name="rsync")
    assert not is_allowlisted("python -c scan_source", name="python")


def test_classify_ignore_low_cpu() -> None:
    assert classify(_snap(cpu=50.0, age=30.0)).action == "ignore"


def test_classify_ignore_short_age() -> None:
    # %99 ama 2dk -> gecici (pytest/build) -> ignore (comert-esik, klipper #100115)
    assert classify(_snap(cpu=99.0, age=2.0)).action == "ignore"


def test_classify_kill_net_runaway() -> None:
    v = classify(_snap(cpu=99.0, age=20.0, cmd="python -c scan_source", name="python"))
    assert v.action == "kill"
    assert v.runaway
    assert not v.allowlisted


def test_classify_allowlist_never_kill() -> None:
    # pytest %100 20dk -> allowlist, sure<warn(30) -> ignore (KILL YOK)
    assert classify(_snap(cpu=100.0, age=20.0, cmd="python -m pytest", name="python")).action == "ignore"
    # pytest %100 40dk -> allowlist + sure>=warn -> notify (yine KILL YOK)
    v = classify(_snap(cpu=100.0, age=40.0, cmd="python -m pytest", name="python"))
    assert v.action == "notify"
    assert v.allowlisted


def test_check_heartbeat_stalls(tmp_path: Path) -> None:
    (tmp_path / "fresh.json").write_text('{"ts": "2026-06-21T19:00:00+00:00"}', encoding="utf-8")
    (tmp_path / "stale.json").write_text('{"ts": "2026-06-21T18:00:00+00:00"}', encoding="utf-8")
    now = datetime.datetime.fromisoformat("2026-06-21T19:05:00+00:00").timestamp()
    stalls = check_heartbeat_stalls(tmp_path, max_age_minutes=10.0, now_ts=now)
    assert {s.agent for s in stalls} == {"stale"}  # fresh=5dk taze, stale=65dk bayat


def test_check_heartbeat_stalls_missing_dir(tmp_path: Path) -> None:
    assert check_heartbeat_stalls(tmp_path / "yok", now_ts=0.0) == []


def test_run_watchdog_emits_and_respects_autokill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", _events_db(tmp_path))  # emit_throttled gerçek events-tablosuna yazar
    hb = tmp_path / "hb"
    hb.mkdir()  # boş hook-state -> stall yok, yalnız runaway
    runaway = _snap(cpu=99.0, age=20.0, cmd="python -c scan_source", name="python")
    legit = _snap(cpu=100.0, age=20.0, cmd="python -m pytest", name="python")
    monkeypatch.setattr(aw, "snapshot_processes", lambda *a, **k: [runaway, legit])
    monkeypatch.setattr(aw, "_autokill_enabled", lambda: False)  # notify-only
    monkeypatch.setattr(aw, "_verify_and_kill", lambda snap, dry_run: {"result": "dry_run-intent"})
    summary = aw.run_watchdog(hook_state_dir=str(hb))
    assert summary["runaways"] == 1  # legit -> ignore, sadece runaway sayildi
    assert summary["killed"] == 0  # autokill OFF -> dry_run
    assert summary["emitted"] == 1
    # runaway critical olayı events-spine'a düştü mü
    con = sqlite3.connect(tmp_path / "server.db")
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM events WHERE type='agent-health'").fetchall()
    con.close()
    assert len(rows) == 1
    assert rows[0]["severity"] == "critical"
    assert rows[0]["source"] == "watchdog:proc:python"


def test_run_watchdog_dedups_across_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aynı runaway iki ardışık cron-turunda: 1. EMIT, 2. SUPPRESS (DB-throttle, klipper #100128).

    Eski davranış her */3'te re-emit'ti (flood). emit_throttled WATCHDOG_DEDUP_WINDOW
    içinde aynı (type, source)'u bastırır → events'e tek satır.
    """
    monkeypatch.setenv("DB_PATH", _events_db(tmp_path))
    hb = tmp_path / "hb"
    hb.mkdir()
    runaway = _snap(cpu=99.0, age=20.0, cmd="python -c scan_source", name="python")
    monkeypatch.setattr(aw, "snapshot_processes", lambda *a, **k: [runaway])
    monkeypatch.setattr(aw, "_autokill_enabled", lambda: False)
    monkeypatch.setattr(aw, "_verify_and_kill", lambda snap, dry_run: {"result": "dry_run-intent"})
    s1 = aw.run_watchdog(hook_state_dir=str(hb))
    s2 = aw.run_watchdog(hook_state_dir=str(hb))
    assert s1["emitted"] == 1
    assert s2["emitted"] == 0  # pencere-içi -> re-emit YOK
    assert s2["suppressed"] == 1
    con = sqlite3.connect(tmp_path / "server.db")
    n = con.execute("SELECT COUNT(*) FROM events WHERE type='agent-health'").fetchone()[0]
    con.close()
    assert n == 1  # ikinci tur events'e yazmadı (dedup)


def test_run_watchdog_heartbeat_stall_emits_and_dedups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_watchdog heartbeat-stall dalı: emit + cross-run dedup (runaway dalından ayrı kanıt)."""
    monkeypatch.setenv("DB_PATH", _events_db(tmp_path))
    hb = tmp_path / "hb"
    hb.mkdir()
    (hb / "code-review.json").write_text('{"ts": "2020-01-01T00:00:00+00:00"}', encoding="utf-8")  # bayat
    monkeypatch.setattr(aw, "snapshot_processes", lambda *a, **k: [])  # runaway yok, yalnız stall
    s1 = aw.run_watchdog(hook_state_dir=str(hb))
    s2 = aw.run_watchdog(hook_state_dir=str(hb))
    assert s1["stalls"] == 1
    assert s1["emitted"] == 1
    assert s2["emitted"] == 0  # pencere-içi dedup
    assert s2["suppressed"] == 1
    con = sqlite3.connect(tmp_path / "server.db")
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM events WHERE type='agent-health'").fetchall()
    con.close()
    assert len(rows) == 1
    assert rows[0]["severity"] == "warn"
    assert rows[0]["source"] == "watchdog:heartbeat:code-review"


def test_heartbeat_stall_skips_non_dict_json(tmp_path):
    """hook-state'te heartbeat-OLMAYAN json (ör. pending-notes.json=LIST) stall-taramayı ÇÖKERTMEMELI
    (klipper: cron-wire canlı tetikledi; data.get('ts') AttributeError tüm-taramayı düşürüyordu)."""
    import json as _json

    from app.core import agent_watchdog as w

    (tmp_path / "pending-notes.json").write_text(_json.dumps([1, 2, 3]))  # LIST (dict değil)
    (tmp_path / "bad.json").write_text("{not json")  # bozuk
    (tmp_path / "last-code-review.json").write_text(_json.dumps({"ts": "2020-01-01T00:00:00"}))  # bayat heartbeat
    stalls = w.check_heartbeat_stalls(str(tmp_path))  # ÇÖKMEMELI
    agents = {s.agent for s in stalls}
    assert "last-code-review" in agents  # gerçek bayat-heartbeat yakalandı
    assert "pending-notes" not in agents  # list-json atlandı (çökmedi)
