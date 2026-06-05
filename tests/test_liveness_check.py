"""META-MONITOR (automation/liveness-check.sh) — bekçileri-izle.

dead component -> DIRECT Telegram (dead-man's switch) + spine kaydı + edge-detection.
LIVENESS_RESULT enjekte edilir; curl + emit-event gölgelenir.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "automation" / "liveness-check.sh"


def _fake_bins(bindir: Path, capture: Path, curl_http: str = "200") -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    curl = bindir / "curl"
    # args'ı capture'a logla + -w %{http_code} için stdout'a http kodu yaz (TG_OK belirler).
    curl.write_text(f'#!/bin/bash\nprintf "CURL %s\\n" "$*" >> {str(capture)!r}\nprintf "%s" {curl_http!r}\n')
    curl.chmod(0o755)
    emit = bindir / "emit-event.sh"
    emit.write_text(f'#!/bin/bash\nprintf "EMIT %s\\n" "$*" >> {str(capture)!r}\n')
    emit.chmod(0o755)


def _run(tmp_path: Path, result_json: str, prev_state: str | None = None, curl_http: str = "200") -> tuple[str, str]:
    capture = tmp_path / "cap.log"
    _fake_bins(tmp_path / "bin", capture, curl_http)
    state = tmp_path / "state"
    if prev_state is not None:
        state.write_text(prev_state)
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            "LIVENESS_APP_DIR": str(tmp_path),
            "NOTIFY_ENV_FILE": "/dev/null",
            "EMIT_EVENT": str(tmp_path / "bin" / "emit-event.sh"),
            "LIVENESS_STATE": str(state),
            "LIVENESS_LOG": str(tmp_path / "lv.log"),
            "LIVENESS_RESULT": result_json,
            "TELEGRAM_BOT_TOKEN": "x",
            "TELEGRAM_CHAT_ID": "1",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    cap = capture.read_text() if capture.exists() else ""
    return r.stdout, cap


def test_dead_component_direct_telegram_and_spine(tmp_path):
    """Ölü component -> DIRECT Telegram (sendMessage) + spine emit."""
    out, cap = _run(tmp_path, '{"dead":[{"source":"notify-cron"}],"stale":[]}')
    assert "OUTCOME: fail" in out
    assert "sendMessage" in cap  # DIRECT Telegram (dead-man's switch)
    assert "notify-cron" in cap
    assert "EMIT" in cap  # spine kaydı da yapıldı


def test_all_alive_no_alert(tmp_path):
    """Hiç dead yok -> Telegram/emit YOK, OUTCOME pass."""
    out, cap = _run(tmp_path, '{"dead":[],"stale":[]}')
    assert "OUTCOME: pass" in out
    assert "sendMessage" not in cap
    assert "EMIT" not in cap


def test_same_dead_set_no_repeat_alarm(tmp_path):
    """Aynı dead-set tekrar -> tekrar-alarm YOK (edge-detection)."""
    out, cap = _run(tmp_path, '{"dead":[{"source":"notify-cron"}]}', prev_state="notify-cron")
    assert "OUTCOME: partial" in out
    assert "sendMessage" not in cap  # tekrar bildirim yok


def test_recovery_clears_state(tmp_path):
    """Önceki dead vardı, şimdi temiz -> recovered (state temizlenir, alarm yok)."""
    out, cap = _run(tmp_path, '{"dead":[],"stale":[]}', prev_state="notify-cron")
    assert "OUTCOME: pass" in out
    assert "sendMessage" not in cap


def test_check_all_empty_is_fail(tmp_path):
    """check_all boş dönerse OUTCOME:fail (sessiz-pass değil)."""
    out, _ = _run(tmp_path, "")
    assert "OUTCOME: fail" in out


def test_dead_set_persisted_only_on_successful_alert(tmp_path):
    """Codex P1: DIRECT-Telegram başarılı (200) -> state yazılır."""
    state = tmp_path / "state"
    _run(tmp_path, '{"dead":[{"source":"notify-cron"}]}', curl_http="200")
    assert state.read_text().strip() == "notify-cron"


def test_dead_set_not_persisted_on_failed_alert(tmp_path):
    """Codex P1: DIRECT-Telegram başarısız (500) -> state YAZILMAZ (sonraki run retry)."""
    state = tmp_path / "state"
    _run(tmp_path, '{"dead":[{"source":"notify-cron"}]}', curl_http="500")
    assert not state.exists() or state.read_text().strip() == ""
