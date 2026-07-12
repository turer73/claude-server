"""note_poller_decide.py testleri — Faz-A SS5 kill-switch + SS10 audit-yazımı
(docs/autonomous-comms-design.md). Gerçek autonomous-claude.sh spawn'ı TETİKLENMEZ —
subprocess.Popen monkeypatch'lenir (spawn() Popen çağırır ama child hiçbir-şey yapmadan
biter, gerçek claude-oturumu asla başlamaz).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from automation.note_poller_decide import decide, read_halt_flag, spawn, write_audit


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE autonomous_comms_halt (id INTEGER PRIMARY KEY CHECK (id=1), active INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO autonomous_comms_halt (id, active) VALUES (1, 0)")
    conn.commit()
    conn.close()
    return path


def _set_halt(db_path: str, active: int) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE autonomous_comms_halt SET active=? WHERE id=1", (active,))
    conn.commit()
    conn.close()


def _audit_rows(db_path: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT note_id, device, action, detail FROM autonomous_comms_audit ORDER BY id").fetchall()
    conn.close()
    return rows


NOTES = [
    {"id": 1, "from_device": "surer", "title": "Merhaba test", "preview": "selam"},
    {"id": 2, "from_device": "surer", "title": "URGENT: incident var", "preview": "acil"},
    {"id": 3, "from_device": "klipper", "title": "URGENT: Threat #5", "preview": "kendi-threat"},
    {"id": 4, "from_device": "surer", "title": "CLAIM: repo — is-tanimi x", "preview": "claim"},
]


def test_read_halt_flag_false_by_default(db):
    assert read_halt_flag(db) is False


def test_read_halt_flag_true_when_active(db):
    _set_halt(db, 1)
    assert read_halt_flag(db) is True


def test_read_halt_flag_fail_open_when_table_missing(tmp_path):
    """SS5: halt-tablo yoksa (eski-DB/deploy-once-not-yet) FAIL-OPEN — spawn devam eder,
    fail-CLOSED sessiz-tam-durdurma YANLIS-secim olurdu (2026-07-12 OAuth-olayindan ders)."""
    path = str(tmp_path / "no_halt_table.db")
    sqlite3.connect(path).close()
    assert read_halt_flag(path) is False


def test_decide_normal_categorizes_correctly(db):
    result = decide(NOTES, db, "klipper-test")
    assert result["halt"] is False
    assert [n["id"] for n in result["spawned"]] == [2, 1]  # URGENT-once, sonra normal
    assert result["skipped_self"] == [3]
    assert result["skipped_protocol"] == [4]
    assert result["skipped_halt"] == []
    assert result["deferred_rate_limit"] == []


def test_decide_halt_active_skips_all_spawns(db):
    """SS5 kill-switch EN-KRITIK-davranis: active=1 -> HICBIR not spawn edilmez."""
    _set_halt(db, 1)
    result = decide(NOTES, db, "klipper-test")
    assert result["halt"] is True
    assert result["spawned"] == []
    assert sorted(result["skipped_halt"]) == [1, 2, 3, 4]


def test_decide_writes_audit_rows_for_skip_categories(db):
    """decide() SKIP-kategorilerini (self/protocol/halt/rate-limit) audit'ler; 'spawned' satiri
    ayri (spawn() cagirildiginda yazilir — bkz test_spawn_invokes_autonomous_claude_with_correct_args)."""
    decide(NOTES, db, "klipper-test")
    rows = _audit_rows(db)
    actions = {(note_id, action) for note_id, _, action, _ in rows}
    assert (3, "skipped_self") in actions
    assert (4, "skipped_protocol") in actions
    assert len(rows) == 2  # yalniz 2 skip-kararı; 1/2 spawn-listesinde, henuz spawn() cagirilmadi


def test_decide_halt_writes_skipped_halt_audit_for_all(db):
    _set_halt(db, 1)
    decide(NOTES, db, "klipper-test")
    rows = _audit_rows(db)
    assert len(rows) == 4
    assert all(action == "skipped_halt" for _, _, action, _ in rows)


def test_decide_rate_limit_defers_after_3_same_source(db):
    notes = [{"id": i, "from_device": "surer", "title": f"n{i}", "preview": ""} for i in range(1, 6)]
    result = decide(notes, db, "klipper-test")
    assert len(result["spawned"]) == 3
    assert len(result["deferred_rate_limit"]) == 2


def test_write_audit_creates_table_if_missing(tmp_path):
    """SS10: audit-tablo yoksa idempotent CREATE ile kendi-kurar (cross-language coupling yok)."""
    path = str(tmp_path / "fresh.db")
    sqlite3.connect(path).close()
    write_audit(path, "klipper-test", 42, "spawned", "test")
    rows = _audit_rows(path)
    assert rows == [(42, "klipper-test", "spawned", "test")]


def test_spawn_invokes_autonomous_claude_with_correct_args(db, monkeypatch):
    """spawn() gercek autonomous-claude.sh'i cagirir (Popen) — burada MOCK'lanir, gercek
    claude-oturumu asla baslamaz. Cagri-argumanlari + audit-yazimi dogrulanir."""
    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr("automation.note_poller_decide.subprocess.Popen", FakePopen)
    spawn({"id": 7, "from_device": "surer", "title": "test-note", "preview": "onizleme"}, db, "klipper-test")

    assert captured["cmd"][0] == "/opt/linux-ai-server/automation/autonomous-claude.sh"
    assert captured["cmd"][1:] == ["7", "surer", "test-note", "onizleme"]
    assert captured["kwargs"]["env"]["ENFORCE_INTERACTIVE_CHECK"] == "1"
    rows = _audit_rows(db)
    assert rows == [(7, "klipper-test", "spawned", "from=surer")]


def test_main_argv_bounds_check_exits_nonzero(db):
    """#1316: sys.argv[1]/[2]'ye korunmasiz erisim yerine acik hata + exit(1)."""
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, str(repo_root / "automation" / "note_poller_decide.py"), db],
        input="[]",
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=10,
    )
    assert proc.returncode == 1
    assert "Kullanım" in proc.stderr


def test_main_malformed_json_fails_safe(db):
    """#1317: gecersiz JSON crash yerine last_seen'i stdout'a yazip sessizce doner (fail-safe)."""
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, str(repo_root / "automation" / "note_poller_decide.py"), db, "klipper-test", "42"],
        input="{not valid json",
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=10,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "42"
    assert "JSON-parse basarisiz" in proc.stderr


def test_main_skips_notes_missing_required_keys(db):
    """#1315: 'id'/'from_device' eksik not KeyError ile spawn-dongusunu cokertmez, atlanip loglanir.
    halt=1 KULLANILIR ki gecerli not spawn()'a ulasip gercek autonomous-claude.sh'i tetiklemesin
    (test_main_cli_end_to_end_with_halt_never_spawns ile ayni guvenlik-deseni)."""
    _set_halt(db, 1)
    repo_root = Path(__file__).resolve().parents[1]
    notes = [{"id": 1, "from_device": "surer", "title": "gecerli", "preview": ""}, {"title": "id-eksik"}, {"id": 2}]
    proc = subprocess.run(
        [sys.executable, str(repo_root / "automation" / "note_poller_decide.py"), db, "klipper-test", "0"],
        input=json.dumps(notes),
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=10,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "1"
    assert "gecersiz not" in proc.stderr
    assert "halt=True" in proc.stderr


def test_main_cli_end_to_end_with_halt_never_spawns(db):
    """note-poller.sh'nin gercekten cagirdigi CLI-arayuzunu (argv+stdin+stdout) subprocess
    ile uctan-uca test eder. halt=1 KULLANILIR ki gercek autonomous-claude.sh ASLA
    tetiklenmesin (bu test gercek bir claude-oturumu baslatmamali)."""
    _set_halt(db, 1)
    repo_root = Path(__file__).resolve().parents[1]  # tests/ -> repo-kok (CI'da farkli path'te checkout, sabit-kodlanmaz)
    proc = subprocess.run(
        [sys.executable, str(repo_root / "automation" / "note_poller_decide.py"), db, "klipper-test", "0"],
        input=json.dumps(NOTES),
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=10,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "4"  # spawned_max_id = max(handled_ids) = 4
    assert "halt=True" in proc.stderr
    rows = _audit_rows(db)
    assert len(rows) == 4
    assert all(action == "skipped_halt" for _, _, action, _ in rows)
