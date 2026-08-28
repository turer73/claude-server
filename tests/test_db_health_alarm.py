"""db_health_alarm — alarm yolu izledigi DB'ye BAGLI OLMAMALI.

2026-08-18..08-26: server.db 8 gun 3 saat yazamadi, 55.855 hata atildi, HIC alarm
cikmadi — cunku eskalasyon `alerts` tablosuna yaziyordu, yani haber verecek kanal
da arizanin icindeydi. Bu modul o yuzden SQLite'a hic dokunmaz.

Kontrat:
  - Ilk hata ANINDA gider; ayni epizot surerken pencere icinde bastirilir
    (55.855 hata = 55.855 mesaj degil; bkz db-integrity 164x korelmesi).
  - "Duzeldi" mesaji YALNIZ acik bir epizot varsa gider.
  - Hicbir fonksiyon exception sizdirmaz: burasi zaten hata yolu.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.core import db_health_alarm as alarm


def _stub_script(tmp_path: Path) -> tuple[Path, Path]:
    """telegram-alert.sh yerine cagrilari dosyaya yazan sahte script."""
    calls = tmp_path / "calls.txt"
    script = tmp_path / "stub.sh"
    # Mesaj cok satirli; cagri-basina TEK satir yaz ki satir sayimi = cagri sayisi olsun.
    # Tek printf sart: es zamanli cagrilarda parcali yazim ayni satira karisiyordu.
    script.write_text(f'#!/bin/bash\nline=$(printf "%s" "$*" | tr "\\n" " ")\nprintf "CALL %s\\n" "$line" >> {calls}\nexit 0\n')
    script.chmod(0o755)
    return script, calls


def _wire(monkeypatch: Any, tmp_path: Path) -> Path:
    script, calls = _stub_script(tmp_path)
    monkeypatch.setenv("DB_ALARM_SCRIPT", str(script))
    monkeypatch.setenv("DB_ALARM_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("DB_ALARM_LOG", str(tmp_path / "db-health.log"))
    monkeypatch.setenv("DB_HEALTH_ALARM_ENABLED", "1")
    return calls


async def _drain() -> None:
    """Arka plana atilan gonderim task'larinin bitmesini bekle."""
    for _ in range(50):
        if not alarm._pending:
            return
        await asyncio.gather(*list(alarm._pending), return_exceptions=True)
    return


async def test_first_failure_sends_immediately(monkeypatch: Any, tmp_path: Path) -> None:
    calls = _wire(monkeypatch, tmp_path)
    alarm.report_db_failure("execute", OSError("disk gitti"))
    await _drain()

    assert calls.exists(), "ilk hata ANINDA gitmeli"
    assert "disk gitti" in calls.read_text()
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["active"] is True


async def test_repeat_within_window_is_suppressed(monkeypatch: Any, tmp_path: Path) -> None:
    """Ayni epizot surerken pencere icinde tekrar gonderilmez, ama SAYILIR."""
    calls = _wire(monkeypatch, tmp_path)
    monkeypatch.setenv("DB_ALARM_REPEAT_SEC", "3600")

    for _ in range(50):
        alarm.report_db_failure("execute", OSError("ayni ariza"))
    await _drain()

    assert len(calls.read_text().strip().splitlines()) == 1, "50 hata icin 1 mesaj"
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["suppressed"] == 49


async def test_repeat_after_window_sends_again(monkeypatch: Any, tmp_path: Path) -> None:
    calls = _wire(monkeypatch, tmp_path)
    monkeypatch.setenv("DB_ALARM_REPEAT_SEC", "0")  # pencere yok -> her hata gider

    alarm.report_db_failure("execute", OSError("bir"))
    alarm.report_db_failure("execute", OSError("iki"))
    await _drain()

    assert len(calls.read_text().strip().splitlines()) == 2


async def test_recovery_only_when_episode_open(monkeypatch: Any, tmp_path: Path) -> None:
    calls = _wire(monkeypatch, tmp_path)

    # Acik epizot yokken "duzeldi" mesaji gitmemeli (gurultu).
    alarm.report_db_recovered("execute")
    await _drain()
    assert not calls.exists()

    alarm.report_db_failure("execute", OSError("ariza"))
    await _drain()
    alarm.report_db_recovered("execute")
    await _drain()

    lines = calls.read_text().strip().splitlines()
    assert len(lines) == 2, "arizadan sonra tam bir 'duzeldi' mesaji"
    assert "geri geldi" in lines[1]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["active"] is False


async def test_kill_switch_blocks_send_but_keeps_log(monkeypatch: Any, tmp_path: Path) -> None:
    """Kapaliyken Telegram gitmez ama kayit YINE tutulur (sessizlik olmasin)."""
    calls = _wire(monkeypatch, tmp_path)
    monkeypatch.setenv("DB_HEALTH_ALARM_ENABLED", "0")

    alarm.report_db_failure("execute", OSError("kapali"))
    await _drain()

    assert not calls.exists()
    assert "kapali" in (tmp_path / "db-health.log").read_text()


async def test_never_raises_when_script_missing(monkeypatch: Any, tmp_path: Path) -> None:
    _wire(monkeypatch, tmp_path)
    monkeypatch.setenv("DB_ALARM_SCRIPT", str(tmp_path / "yok" / "hic-yok.sh"))

    alarm.report_db_failure("execute", OSError("ariza"))  # raise etmemeli
    await _drain()


async def test_never_raises_when_state_unwritable(monkeypatch: Any, tmp_path: Path) -> None:
    _wire(monkeypatch, tmp_path)
    # Yazilamayan yol: var olan bir DOSYA'nin altina dizin acilamaz.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("DB_ALARM_STATE", str(blocker / "state.json"))
    monkeypatch.setenv("DB_ALARM_LOG", str(blocker / "db.log"))

    alarm.report_db_failure("execute", OSError("ariza"))  # raise etmemeli
    await _drain()


async def test_alarm_never_touches_sqlite(monkeypatch: Any, tmp_path: Path) -> None:
    """Regresyon kilidi: bu modul SQLite'a dokunursa ayni sessiz kesinti tekrar eder."""
    import sqlite3

    _wire(monkeypatch, tmp_path)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("db_health_alarm SQLite'a dokunmamali")

    monkeypatch.setattr(sqlite3, "connect", boom)
    alarm.report_db_failure("execute", OSError("ariza"))
    alarm.report_db_recovered("execute")
    await _drain()
