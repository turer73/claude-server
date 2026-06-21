"""agent_watchdog (gap-7) birim-testleri.

FP-onleme kilidi (klipper #100115 — yanlis-pozitif "felaketten beter"): mesru-proc
(pytest/ruff/...) ASLA kill; net-runaway kill-aday; allowlist-busy notify/ignore;
heartbeat-stall tespit; auto-kill default-OFF -> dry_run.
"""

from __future__ import annotations

import datetime
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
    runaway = _snap(cpu=99.0, age=20.0, cmd="python -c scan_source", name="python")
    legit = _snap(cpu=100.0, age=20.0, cmd="python -m pytest", name="python")
    monkeypatch.setattr(aw, "snapshot_processes", lambda *a, **k: [runaway, legit])
    monkeypatch.setattr(aw, "_autokill_enabled", lambda: False)  # notify-only
    monkeypatch.setattr(aw, "_verify_and_kill", lambda snap, dry_run: {"result": "dry_run-intent"})
    captured: list[dict[str, object]] = []

    def _fake_emit(**kw: object) -> int:
        captured.append(kw)
        return 1

    monkeypatch.setattr("app.core.events.emit_event", _fake_emit)
    summary = aw.run_watchdog(hook_state_dir=str(tmp_path))
    assert summary["runaways"] == 1  # legit -> ignore, sadece runaway sayildi
    assert summary["killed"] == 0  # autokill OFF -> dry_run
    assert any(c["severity"] == "critical" for c in captured)  # runaway critical emit


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
