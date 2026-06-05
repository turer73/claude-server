"""social-renderhane-balance-alert.sh — outcome-contract uyumu.

Önce: script hiç OUTCOME marker basmıyordu -> geçici dış-API timeout'u (rc=1) cron-wrap'ta
CRITICAL 'outcome-undefined' oluyordu (aşırı-alarm). Şimdi: timeout/parse-hata -> partial
(warning), başarılı okuma -> pass. curl PATH'te gölgelenir (gerçek VPS/Telegram'a gitmez).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "automation" / "social-renderhane-balance-alert.sh"


def _fake_curl(bindir: Path, health_body: str) -> None:
    """Sahte curl: /api/health -> health_body; sendMessage (Telegram) -> sessiz boş."""
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "curl"
    fake.write_text(f'#!/bin/bash\nif printf "%s" "$*" | grep -q "sendMessage"; then\n  exit 0\nfi\nprintf \'%s\' {health_body!r}\n')
    fake.chmod(0o755)


def _run(tmp_path: Path, health_body: str) -> str:
    _fake_curl(tmp_path / "bin", health_body)
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            "RENDERHANE_BALANCE_THRESHOLD": "200",
            "TELEGRAM_BOT_TOKEN": "x",
            "TELEGRAM_CHAT_ID": "1",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    return r.stdout


def test_renderhane_healthy_balance_outcome_pass(tmp_path):
    out = _run(tmp_path, '{"renderhane_balance": 500}')
    assert "OUTCOME: pass" in out
    assert "balance=500" in out


def test_renderhane_api_timeout_is_partial_not_critical(tmp_path):
    """Boş yanıt (geçici timeout) -> partial (warning), CRITICAL DEĞİL."""
    out = _run(tmp_path, "")
    assert "OUTCOME: partial" in out
    assert "fail" not in out.lower().replace("partial", "")


def test_renderhane_parse_error_is_partial(tmp_path):
    """Beklenmedik payload (balance alanı yok) -> partial."""
    out = _run(tmp_path, '{"foo": 1}')
    assert "OUTCOME: partial" in out
