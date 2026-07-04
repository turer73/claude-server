"""G4 PR-C — held_not_delivered ∀-parametrize invariant (tasarım §2c, strateji #100387).

UNIFORM invariant: status='held' not HİÇBİR kayıtlı-yüzeyden teslim/sayım/spawn edilmez.
Yalnız INVOCATION kategoriye-göre değişir (category-dispatch harness'ları):
golden-held-fixture seed → entry-point'i GERÇEK-yolundan invoke → held-ABSENT assert
+ aktif-not-PRESENT assert (boş-çıktı yalancı-yeşilini önler — yüzey gerçekten çalıştı).

Concern-taşıyan ama harness'sız kayıt → parametrize üretir → assert-yok → FAIL
(tasarım: path-coverage zorunlu; yeni yüzey registry'ye eklenince harness da yazılmalı).

Shell-yüzeyler gerçek-subprocess (bash+sqlite3 gerekir) — Windows-lokal SKIP, CI-otoriter.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.entrypoints.registry import NOTE_DELIVERY, by_concern

pytestmark = pytest.mark.g4_invariant  # ana-suite deselect eder; g4-invariant job'u kosar (non-required v1)

REPO_ROOT = Path(__file__).resolve().parents[1]
HELD_MARK = "HELD-CANARY-7f3a"
ACTIVE_MARK = "AKTIF-CANARY-9b2c"
_SHELL_MISSING = [t for t in ("bash", "sqlite3") if shutil.which(t) is None]


def _seed_golden_db(tmp_path: Path) -> Path:
    """Tek golden-fixture: tam-şema claude_memory.db — held + aktif not yan-yana."""
    db = tmp_path / "claude_memory.db"
    con = sqlite3.connect(db)
    con.executescript(
        f"""
        CREATE TABLE memories (id INTEGER PRIMARY KEY, type TEXT, name TEXT, description TEXT,
                               content TEXT, active INT DEFAULT 1, read_count INT DEFAULT 0,
                               created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, session_num INT, date TEXT, device_name TEXT,
                               platform TEXT, summary TEXT, created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE tasks_log (id INTEGER PRIMARY KEY, project TEXT, task TEXT, status TEXT,
                                device_name TEXT, created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE discoveries (id INTEGER PRIMARY KEY, project TEXT, type TEXT, title TEXT, details TEXT,
                                  status TEXT, device_name TEXT, rationale TEXT, read_count INT DEFAULT 0,
                                  created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE devices (name TEXT, platform TEXT, hostname TEXT, tailscale_ip TEXT,
                              claude_version TEXT, last_seen TEXT);
        INSERT INTO devices VALUES ('klipper', 'linux', 'klipper', '100.84.251.49', '2.x', datetime('now'));
        CREATE TABLE device_projects (device_name TEXT, project TEXT, local_path TEXT, last_activity TEXT);
        CREATE TABLE notes (id INTEGER PRIMARY KEY, from_device TEXT, to_device TEXT, title TEXT,
                            content TEXT, read INT DEFAULT 0, read_by TEXT DEFAULT '',
                            status TEXT DEFAULT 'active', created_at TEXT DEFAULT (datetime('now')));
        INSERT INTO notes (from_device, to_device, title, content, read, status)
               VALUES ('surer', 'klipper', '{ACTIVE_MARK}', 'aktif icerik', 0, 'active');
        INSERT INTO notes (from_device, to_device, title, content, read, status)
               VALUES ('surer', 'klipper', '{HELD_MARK}', 'held icerik', 0, 'held');
        """
    )
    con.commit()
    con.close()
    return db


def _assert_delivery(text: str, surface: str) -> None:
    """Çift-yönlü: held YOK + aktif VAR (yüzey-çalıştı kanıtı)."""
    assert HELD_MARK not in text, f"{surface}: HELD not teslim edildi (#1222 ihlali)"
    assert ACTIVE_MARK in text, f"{surface}: aktif-not görünmüyor — yüzey hiç çalışmadı mı? (yalancı-yeşil koruması)"


def _run_shell(cmd: list[str], env: dict[str, str], timeout: int = 30) -> str:
    import os

    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env={**os.environ, **env}, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


# ── category-dispatch harness'ları (ep.id → invoke) ─────────────────────────────


def _h_api_notes_list(db: Path, tmp_path: Path) -> None:
    import app.api.memory as mem
    from app.api.memory.notes import list_notes

    _orig = mem.DB_PATH
    mem.DB_PATH = str(db)
    try:
        rows = asyncio.run(list_notes(device="klipper", unread_only=True))
    finally:
        mem.DB_PATH = _orig
    _assert_delivery(json.dumps(rows, ensure_ascii=False), "api:notes-list")


def _h_api_onboard(db: Path, tmp_path: Path) -> None:
    import app.api.memory as mem
    from app.api.memory.onboard import get_onboard_prompt

    _orig = mem.DB_PATH
    mem.DB_PATH = str(db)
    try:
        out = asyncio.run(get_onboard_prompt(device_name="klipper"))
    finally:
        mem.DB_PATH = _orig
    _assert_delivery(json.dumps(out, ensure_ascii=False), "api:onboard")


def _h_api_dashboard(db: Path, tmp_path: Path) -> None:
    """Sayaç-yüzeyi: held unread_notes'a SAYILMAZ (marker-metin değil sayı-invariantı)."""
    import app.api.memory as mem
    from app.api.memory.dashboard import _dashboard_query

    _orig = mem.DB_PATH
    mem.DB_PATH = str(db)
    try:
        result = _dashboard_query()
    finally:
        mem.DB_PATH = _orig
    assert result["stats"]["unread_notes"] == 1, "api:dashboard: held sayaca sızdı (fixture: 1-aktif + 1-held)"


def _h_cron_digest(db: Path, tmp_path: Path) -> None:
    import app.core.digest.sources as sources

    _orig = sources.DB_PATH
    sources.DB_PATH = str(db)
    try:
        delta = sources.memory_delta(24)
    finally:
        sources.DB_PATH = _orig
    _assert_delivery(json.dumps(delta["unread_notes"], ensure_ascii=False), "cron:digest")


def _h_hook_stop_check_inbox(db: Path, tmp_path: Path) -> None:
    import os
    import sys

    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "hooks" / "stop-check-inbox.py")],
        input="{}",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "HOOK_DB": str(db),
            "HOOK_DEVICE": "klipper",
            "HOOK_LOG_DIR": str(tmp_path / "logs"),
            "PYTHONIOENCODING": "utf-8",
        },
        timeout=60,
    )
    _assert_delivery((r.stdout or "") + (r.stderr or ""), "hook:stop-check-inbox")


def _h_hook_session_start(db: Path, tmp_path: Path) -> None:
    """held AKTİF-listede yok; ONAY-GÖRÜNÜMÜ ('Onay Bekleyen HELD Dispatch') MEŞRUdur —
    #1222 §4: insana onay-hatırlatması teslim-DEĞİL (ilk-CI-koşum FP'siydi, invariant inceltildi)."""
    out = _run_shell(
        ["bash", str(REPO_ROOT / "scripts" / "hooks" / "session-start.sh")],
        {
            "HOOK_DB": str(db),
            "HOOK_DEVICE": "klipper",
            "HOOK_SERVER_DB": str(tmp_path / "bos-server.db"),
            "HOOK_LOG_DIR": str(tmp_path / "logs"),
        },
    )
    approval_marker = "Onay Bekleyen HELD Dispatch"
    delivery_part, _sep, approval_part = out.partition(approval_marker)
    _assert_delivery(delivery_part, "hook:session-start")
    # Pozitif-kontrol: held-fixture'lı DB'de onay-hatırlatması GÖRÜNMELİ (o da bir invariant).
    assert _sep, "hook:session-start: held varken onay-görünümü bölümü hiç gelmedi"
    assert HELD_MARK in approval_part, "hook:session-start: onay-bölümünde held-başlığı yok"


def _h_hook_user_prompt(db: Path, tmp_path: Path) -> None:
    """State-diff'li hook: ilk-fire baseline yazar sessiz-çıkar (ilk-CI-koşum dersi) →
    2-fire: baseline → YENİ aktif+held ekle → ikinci-fire yeni-aktifi surface-eder, held'i ETMEZ.
    (STATE_DIR, HOOK_LOG_DIR'den türetilir → tmp-izole.)"""
    env = {"HOOK_DB": str(db), "HOOK_DEVICE": "klipper", "HOOK_LOG_DIR": str(tmp_path / "logs")}
    script = str(REPO_ROOT / "scripts" / "hooks" / "user-prompt-messages.sh")
    _run_shell(["bash", script], env)  # fire-1: baseline (sessiz)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO notes (from_device, to_device, title, content, read, status) "
        f"VALUES ('surer', 'klipper', 'YENI-{ACTIVE_MARK}', 'yeni aktif', 0, 'active')"
    )
    con.execute(
        "INSERT INTO notes (from_device, to_device, title, content, read, status) "
        f"VALUES ('surer', 'klipper', 'YENI-{HELD_MARK}', 'yeni held', 0, 'held')"
    )
    con.commit()
    con.close()
    out = _run_shell(["bash", script], env)  # fire-2: diff-surface
    _assert_delivery(out, "hook:user-prompt-messages")


def _h_cron_note_poller(db: Path, tmp_path: Path) -> None:
    """EN-KRİTİK (#1222): held pending_notes.json'a girmemeli = spawn-tetiklemez.
    Poller sonsuz-döngü → timeout-kill; ilk poll_once pending'i yazar."""
    pending = tmp_path / "pending-notes.json"
    try:
        _run_shell(
            ["bash", str(REPO_ROOT / "automation" / "note-poller.sh")],
            {
                "HOOK_DB": str(db),
                "HOOK_DEVICE": "klipper",
                "PENDING_FILE": str(pending),
                "STATE_FILE": str(tmp_path / "poller-state.json"),
                "LOG_FILE": str(tmp_path / "poller.log"),
                "POLL_INTERVAL": "1",
            },
            timeout=8,
        )
    except subprocess.TimeoutExpired:
        pass  # beklenen: daemon timeout'la kesilir; ilk-tick çoktan koştu
    assert pending.exists(), "cron:note-poller: pending-dosyası hiç yazılmadı — poller çalışmadı mı?"
    _assert_delivery(pending.read_text(encoding="utf-8"), "cron:note-poller")


def _h_cron_daily_summary(db: Path, tmp_path: Path) -> None:
    """Sayaç-yüzeyi: DEFERRED_NOTES held-saymaz. İçerik localhost:8420'ye POST edilir —
    mini-HTTP-yakalayıcı ile gerçek-yoldan alınır (fixture: 1-aktif-unread → '| 1 |')."""
    import http.server
    import threading

    captured: list[bytes] = []

    class _Capture(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — stdlib API adı
            captured.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):  # sessiz
            pass

    try:
        srv = http.server.HTTPServer(("127.0.0.1", 8420), _Capture)
    except OSError:
        pytest.skip("port 8420 dolu (lokal canlı-server) — CI'da koşar")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    # set -euo: awk log-dosyası-yokken command-substitution'da öldürüyor (ilk-CI-koşum dersi) → boş-log.
    (logs / "autonomous-claude.log").write_text("", encoding="utf-8")
    # POST-bloğu MEMORY_API_KEY'i env-dosyasından okur (2. CI-koşum dersi: /opt/.env yok → FileNotFoundError).
    env_file = tmp_path / "test.env"
    env_file.write_text("MEMORY_API_KEY=test-key\n", encoding="utf-8")
    try:
        script_out = _run_shell(
            ["bash", str(REPO_ROOT / "automation" / "autonomous-daily-summary.sh")],
            {"HOOK_DB": str(db), "HOOK_LOG_DIR": str(logs), "HOOK_ENV_FILE": str(env_file)},
            timeout=60,
        )
    finally:
        srv.shutdown()
    body = b"\n".join(captured).decode("utf-8", errors="replace")
    assert body, f"cron:autonomous-daily-summary: memory-POST yakalanamadı — script-çıktısı: {script_out[-500:]}"
    assert "| Hala unread (deferred) | 1 |" in body, (
        f"cron:autonomous-daily-summary: DEFERRED_NOTES held-saydı (1 beklenirdi): {body[:400]}"
    )


def _h_cli_agent_feed(db: Path, tmp_path: Path) -> None:
    out = _run_shell(
        ["bash", str(REPO_ROOT / "scripts" / "agent-feed.sh")],
        {"AGENT_FEED_MEM_DB": str(db), "AGENT_FEED_SRV_DB": str(tmp_path / "bos-server.db"), "HOOK_DEVICE": "klipper"},
    )
    _assert_delivery(out, "cli:agent-feed")


def _h_cli_claude_memory(db: Path, tmp_path: Path) -> None:
    out = _run_shell(
        ["bash", str(REPO_ROOT / "scripts" / "claude-memory.sh"), "notes", "unread"],
        {"CLAUDE_MEMORY_DB": str(db)},
    )
    _assert_delivery(out, "cli:claude-memory")


_HARNESS = {
    "api:notes-list": _h_api_notes_list,
    "api:onboard": _h_api_onboard,
    "api:dashboard": _h_api_dashboard,
    "cron:digest": _h_cron_digest,
    "hook:stop-check-inbox": _h_hook_stop_check_inbox,
    "hook:session-start": _h_hook_session_start,
    "hook:user-prompt-messages": _h_hook_user_prompt,
    "cron:note-poller": _h_cron_note_poller,
    "cron:autonomous-daily-summary": _h_cron_daily_summary,
    "cli:agent-feed": _h_cli_agent_feed,
    "cli:claude-memory": _h_cli_claude_memory,
}

_SHELL_EPS = {
    "hook:session-start",
    "hook:user-prompt-messages",
    "cron:note-poller",
    "cron:autonomous-daily-summary",
    "cli:agent-feed",
    "cli:claude-memory",
}


@pytest.mark.parametrize("ep", by_concern(NOTE_DELIVERY), ids=lambda e: e.id)
def test_held_not_delivered(ep, tmp_path):
    if ep.id in _SHELL_EPS and _SHELL_MISSING:
        pytest.skip(f"CI-otoriter; lokalde eksik: {_SHELL_MISSING}")
    harness = _HARNESS.get(ep.id)
    # Tasarım §2c: concern-taşıyan ama harness'sız kayıt = path-coverage eksik → FAIL.
    assert harness is not None, f"{ep.id}: invariant-harness YOK — yeni yüzeyle birlikte harness da yazılmalı"
    harness(_seed_golden_db(tmp_path), tmp_path)
