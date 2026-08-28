"""DB-bağımsız arıza eskalasyonu.

Bir alarm yolu, izlediği bileşene BAĞLI OLMAMALIDIR. 2026-08-18 03:37 → 08-26 06:51
arası `server.db`'ye 8 gün 3 saat boyunca 55.855 kez
`sqlite3.DatabaseError: file is not a database` atıldı ve **hiç alarm çıkmadı**:
eskalasyon `alerts` tablosuna yazıyordu, yani haber verecek kanal da arızanın
içindeydi. Kaybedilen: metrics ~45k, events ~93k (%98), audit_log ~18k satır.

Bu modül bu yüzden SQLite'a HİÇ dokunmaz. İki bacağı var:
  1. journald'a CRITICAL + ayrı bir düz-metin log (log ayrı LV'de, data/ ile aynı
     diskte olsa bile SQLite'tan bağımsız).
  2. `automation/telegram-alert.sh --kind generic` — DB'ye hiç bakmayan helper.

Hiçbir fonksiyon exception sızdırmaz: burası zaten hata yolu, alarm mekanizmasının
kendisi ikinci bir arıza üretmemeli.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from app.core.config import read_env_var

logger = logging.getLogger(__name__)

_REPO = "/opt/linux-ai-server"
DEFAULT_SCRIPT = f"{_REPO}/automation/telegram-alert.sh"
DEFAULT_STATE = f"{_REPO}/data/hook-state/db-health-alarm.json"
DEFAULT_LOG = "/var/log/linux-ai-server/db-health.log"

# Aynı arıza sürerken Telegram'ı boğmamak için: ilk hata ANINDA gider, sonra
# arıza devam ettikçe en fazla bu aralıkla tekrarlanır. 55.855 hatanın 55.855
# mesaja dönüşmesi alarmı gürültüye çevirirdi (bkz. db-integrity 164x körelmesi).
DEFAULT_REPEAT_SEC = 1800

# Telegram helper'ı beklerken DB yazma yolunu kilitlemeyelim: gönderim ayrı
# task'ta koşar, bu süre sadece o task'ın üst sınırı.
_SEND_TIMEOUT_SEC = 30

# create_task referansı tutulmazsa GC task'ı yarıda toplayabilir (asyncio tuzağı).
_pending: set[asyncio.Task[Any]] = set()


def _enabled() -> bool:
    """Kill-switch, varsayılan AÇIK (sessiz kesinti tekrar etmesin)."""
    return (read_env_var("DB_HEALTH_ALARM_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")


def _script_path() -> str:
    return read_env_var("DB_ALARM_SCRIPT") or DEFAULT_SCRIPT


def _state_path() -> str:
    return read_env_var("DB_ALARM_STATE") or DEFAULT_STATE


def _log_path() -> str:
    return read_env_var("DB_ALARM_LOG") or DEFAULT_LOG


def _repeat_sec() -> int:
    raw = (read_env_var("DB_ALARM_REPEAT_SEC") or "").strip()
    try:
        return max(0, int(raw)) if raw else DEFAULT_REPEAT_SEC
    except ValueError:
        return DEFAULT_REPEAT_SEC


def _read_state() -> dict[str, Any]:
    try:
        with open(_state_path()) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except OSError as exc:  # durum yazılamıyorsa bile alarm yolu çalışmalı
        logger.warning("db-health-alarm durum dosyası yazılamadı (%s): %s", path, exc)


def _append_log(line: str) -> None:
    path = _log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(line.rstrip("\n") + "\n")
    except OSError as exc:
        logger.warning("db-health-alarm log yazılamadı (%s): %s", path, exc)


async def _send_telegram(text: str) -> None:
    """telegram-alert.sh generic — DB'ye dokunmayan tek gönderim yolu."""
    script = _script_path()
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            script,
            "--kind",
            "generic",
            "--text",
            text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.error("db-health-alarm: telegram helper başlatılamadı (%s): %s", script, exc)
        return

    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=_SEND_TIMEOUT_SEC)
    except TimeoutError:
        logger.error("db-health-alarm: telegram helper %ss içinde bitmedi, iptal", _SEND_TIMEOUT_SEC)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return

    rc = proc.returncode or 0
    if rc != 0:
        # rc=2 (creds yok) dahil: sessizce yutma, journald'da iz kalsın.
        logger.error(
            "db-health-alarm: telegram gönderimi başarısız rc=%s stderr=%s",
            rc,
            err.decode("utf-8", errors="replace").strip()[:300],
        )


def _spawn(coro: Any) -> None:
    """Gönderimi arka plana al — DB yazma yolu Telegram'ı BEKLEMEZ."""
    try:
        task = asyncio.ensure_future(coro)
    except RuntimeError:  # çalışan loop yok (senkron test/CLI bağlamı)
        coro.close()
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def report_db_failure(context: str, error: BaseException) -> None:
    """DB bağlantısı kurtarılamadı — bant-dışı eskale et. ASLA raise etmez."""
    try:
        now = time.time()
        summary = f"{type(error).__name__}: {error}"
        line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] FAIL context={context} {summary}"

        # journald: DB'den tamamen bağımsız, her koşulda kalır.
        logger.critical("DB ARIZASI (bant-disi alarm) context=%s hata=%s", context, summary)
        _append_log(line)

        if not _enabled():
            return

        state = _read_state()
        last = float(state.get("last_sent", 0) or 0)
        repeat = _repeat_sec()
        first_of_episode = not state.get("active")
        if not first_of_episode and repeat and (now - last) < repeat:
            state["suppressed"] = int(state.get("suppressed", 0)) + 1
            _write_state(state)
            return

        suppressed = int(state.get("suppressed", 0))
        extra = f"\nBastirilan tekrar: {suppressed}" if suppressed else ""
        text = (
            "<b>🛑 KLIPPER — server.db yazilamiyor</b>\n\n"
            f"<b>Baglam:</b> {context}\n"
            f"<b>Hata:</b> <code>{summary[:300]}</code>\n\n"
            "Kalici baglanti zehirlendi ve yeniden baglanma DA basarisiz oldu.\n"
            "Bu alarm DB'den bagimsiz yoldan gonderildi.\n\n"
            "<i>Tani:</i>\n"
            "<pre>sqlite3 /opt/linux-ai-server/data/server.db 'PRAGMA integrity_check;'\n"
            "journalctl -u linux-ai-server --since '1 hour ago' | grep -c 'file is not a database'</pre>"
            f"{extra}"
        )
        _spawn(_send_telegram(text))
        _write_state({"active": True, "last_sent": now, "suppressed": 0, "context": context})
    except Exception as exc:  # alarm yolu ikinci bir ariza uretmemeli
        logger.error("db-health-alarm: report_db_failure kendi hatasi: %s", exc)


def report_db_recovered(context: str) -> None:
    """Bağlantı yeniden kuruldu. Yalnız açık bir arıza epizodu varsa haber verir."""
    try:
        state = _read_state()
        if not state.get("active"):
            return  # alarm gitmemişti, "düzeldi" mesajı da gitmesin

        suppressed = int(state.get("suppressed", 0))
        _append_log(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] RECOVERED context={context}")
        logger.warning("DB baglantisi yeniden kuruldu (bant-disi alarm kapaniyor) context=%s", context)
        _write_state({"active": False, "last_sent": state.get("last_sent", 0), "suppressed": 0})

        if not _enabled():
            return
        _spawn(
            _send_telegram(
                "<b>✅ KLIPPER — server.db yazimi geri geldi</b>\n\n"
                f"<b>Baglam:</b> {context}\n"
                f"<b>Bastirilan tekrar:</b> {suppressed}\n\n"
                "<i>Yine de integrity_check ile dogrula — yazim geri gelmis olabilir "
                "ama bozulma kalici olabilir.</i>"
            )
        )
    except Exception as exc:
        logger.error("db-health-alarm: report_db_recovered kendi hatasi: %s", exc)
