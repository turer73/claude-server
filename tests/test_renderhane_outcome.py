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


def _fake_curl_flaky(bindir: Path, counter: Path, fail_n: int, health_body: str) -> None:
    """Sahte curl: ilk `fail_n` /api/health çağrısı BOŞ (blip), sonrası health_body (retry-recovery)."""
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "curl"
    fake.write_text(
        "#!/bin/bash\n"
        'if printf "%s" "$*" | grep -q "sendMessage"; then exit 0; fi\n'
        f'C="{counter}"\n'
        'n=$(cat "$C" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$C"\n'
        f'if [ "$n" -le {fail_n} ]; then exit 0; fi\n'  # boş çıktı = empty response (blip)
        f"printf '%s' {health_body!r}\n"
    )
    fake.chmod(0o755)


def _run(tmp_path: Path, health_body: str, extra_env: dict | None = None) -> str:
    _fake_curl(tmp_path / "bin", health_body)
    return _run_bash(tmp_path, extra_env)


def _run_bash(tmp_path: Path, extra_env: dict | None = None) -> str:
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "RENDERHANE_BALANCE_THRESHOLD": "200",
        "TELEGRAM_BOT_TOKEN": "x",
        "TELEGRAM_CHAT_ID": "1",
        "RENDERHANE_RETRY_SLEEP": "0",  # test hızı (backoff sıfır)
        # State'i tmp'ye izole et — prod hook-state'e streak-file sızdırma + gate testlenebilir.
        "RENDERHANE_STATE_DIR": str(tmp_path / "state"),
    }
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
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
    """Boş yanıt (geçici timeout) -> partial (warning), CRITICAL DEĞİL.
    GATE=1 ile tek-örnek partial yüzeye çıkar (persistence-gate'i devre dışı bırak)."""
    out = _run(tmp_path, "", extra_env={"RENDERHANE_PARTIAL_GATE": "1"})
    assert "OUTCOME: partial" in out
    assert "fail" not in out.lower().replace("partial", "")


def test_renderhane_parse_error_is_partial(tmp_path):
    """Beklenmedik payload (balance alanı yok) -> partial (GATE=1 ile tek-örnek yüzeyde)."""
    out = _run(tmp_path, '{"foo": 1}', extra_env={"RENDERHANE_PARTIAL_GATE": "1"})
    assert "OUTCOME: partial" in out


def test_renderhane_isolated_partial_suppressed(tmp_path):
    """Persistence-gate (default GATE=2): TEK izole partial page ETMEZ -> OUTCOME: pass
    (bastırıldı), bir sonraki saatlik run düzeltir. ~1.7/gün blip-gürültüsünü keser."""
    out = _run(tmp_path, "")
    assert "OUTCOME: pass" in out
    assert "bastırıldı" in out
    assert "OUTCOME: partial" not in out


def test_renderhane_sustained_partial_pages(tmp_path):
    """Ardışık >=GATE partial (sürekli kesinti) -> partial (warn/page). State tmp'de
    kalıcı; 1. run bastırılır (streak=1), 2. run page'ler (streak=2)."""
    _fake_curl(tmp_path / "bin", "")  # her zaman boş = erişilemez
    out1 = _run_bash(tmp_path)
    assert "OUTCOME: pass" in out1  # 1. izole → bastırıldı
    out2 = _run_bash(tmp_path)
    assert "OUTCOME: partial" in out2  # 2. ardışık → page
    assert "ardışık" in out2


def test_renderhane_partial_streak_resets_on_success(tmp_path):
    """Başarılı balance-okuma streak'i sıfırlar — toparlanma sonrası tek blip yine bastırılır."""
    _fake_curl(tmp_path / "bin", "")
    _run_bash(tmp_path)  # streak=1
    # toparlandı: başarılı yanıt → reset
    _fake_curl(tmp_path / "bin", '{"renderhane_balance": 500}')
    assert "OUTCOME: pass" in _run_bash(tmp_path)
    # sonraki tek-blip yine streak=1 (reset oldu) → bastırılır, page yok
    _fake_curl(tmp_path / "bin", "")
    out = _run_bash(tmp_path)
    assert "OUTCOME: pass" in out
    assert "OUTCOME: partial" not in out


def test_renderhane_retry_recovers_to_pass(tmp_path):
    """In-run retry (klipper 2026-06-23): ilk 2 deneme BOŞ-blip, 3. başarılı -> PASS (sahte-partial-page YOK).
    Geçici Tailscale/VPS blip artık page üretmez (#205/#207/#209 disiplini)."""
    counter = tmp_path / "n.txt"
    _fake_curl_flaky(tmp_path / "bin", counter, fail_n=2, health_body='{"renderhane_balance": 500}')
    out = _run_bash(tmp_path)
    assert "OUTCOME: pass" in out  # 2 boş-deneme + 1 başarılı = retry kurtardı
    assert "balance=500" in out
    assert "partial" not in out  # sahte-partial üretilmedi


def test_renderhane_all_retries_empty_still_partial(tmp_path):
    """3 denemenin HEPSİ boş (gerçekten erişilemez) -> partial (retry kurtaramadı, doğru davranış).
    GATE=1 ile tek-örnek partial yüzeyde (persistence-gate ayrı testlerde)."""
    counter = tmp_path / "n.txt"
    _fake_curl_flaky(tmp_path / "bin", counter, fail_n=99, health_body='{"renderhane_balance": 500}')
    out = _run_bash(tmp_path, extra_env={"RENDERHANE_PARTIAL_GATE": "1"})
    assert "OUTCOME: partial" in out
    assert "3-denemede" in out  # 3 deneme yapıldığı görünür
