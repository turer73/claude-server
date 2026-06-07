"""cosession koordinasyon sistemi testleri.

/proc-bagimli yardimcilar (_pid_alive/_find_claude_pid/_tty) monkeypatch'lenir;
geri kalan mantik (registry, collision-guard karari, pasif/urgent mesaj teslimi,
self-exclusion) gercek SQLite (tmp) uzerinde dogrulanir.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_MOD = pathlib.Path(__file__).resolve().parents[1] / "scripts/hooks/cosession.py"
_spec = importlib.util.spec_from_file_location("cosession", _MOD)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)

OPT = cs._SHARED_PREFIX


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setattr(cs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cs, "_find_claude_pid", lambda: 4242)
    monkeypatch.setattr(cs, "_tty", lambda pid: "pts/self")
    return cs


def _add_session(sid, tty, cwd, pid=999):
    con = cs._db()
    con.execute(
        "INSERT INTO session_registry (session_id, claude_pid, tty, cwd, "
        "git_branch, started_at, last_seen, last_event, status) "
        "VALUES (?,?,?,?,?,?,?, 'seed', 'active')",
        (sid, pid, tty, cwd, "master", cs._now(), cs._now()),
    )
    con.commit()
    con.close()


# ───────────────────────────── registry ─────────────────────────────
def test_heartbeat_registers(env):
    cs.cmd_heartbeat({"session_id": "s1", "cwd": OPT})
    con = cs._db()
    row = con.execute("SELECT claude_pid, cwd, status FROM session_registry WHERE session_id='s1'").fetchone()
    con.close()
    assert row == (4242, OPT, "active")


def test_live_others_excludes_self(env):
    cs.cmd_heartbeat({"session_id": "self", "cwd": OPT})
    _add_session("other", "pts/1", OPT)
    con = cs._db()
    others = cs._live_others(con, "self")
    con.close()
    assert [o[0] for o in others] == ["other"]


def test_prune_marks_dead(env, monkeypatch):
    _add_session("zombie", "pts/9", OPT, pid=123)
    monkeypatch.setattr(cs, "_pid_alive", lambda pid: False)
    con = cs._db()
    cs._prune(con)
    status = con.execute("SELECT status FROM session_registry WHERE session_id='zombie'").fetchone()[0]
    con.close()
    assert status == "dead"


# ─────────────────────────── collision guard ───────────────────────────
def test_guard_asks_on_shared_opt(env, capsys):
    _add_session("other", "pts/1", OPT)
    rc = cs.cmd_guard({"session_id": "me", "cwd": OPT, "tool_input": {"command": "git switch foo"}})
    out = capsys.readouterr().out
    assert rc == 0
    decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
    assert decision == "ask"


def test_guard_silent_outside_opt(env, capsys):
    # Aktor /opt DISINDA — baska repodaki git-dal islemi /opt landmine'ini etkilemez
    _add_session("other", "pts/1", OPT)
    cs.cmd_guard(
        {
            "session_id": "me",
            "cwd": "/data/projects/kuafor",
            "tool_input": {"command": "git checkout main"},
        }
    )
    assert capsys.readouterr().out.strip() == ""


def test_guard_silent_when_alone(env, capsys):
    cs.cmd_guard({"session_id": "me", "cwd": OPT, "tool_input": {"command": "git switch foo"}})
    assert capsys.readouterr().out.strip() == ""


def test_guard_ignores_non_branch_git(env, capsys):
    _add_session("other", "pts/1", OPT)
    cs.cmd_guard({"session_id": "me", "cwd": OPT, "tool_input": {"command": "git status"}})
    assert capsys.readouterr().out.strip() == ""


# ─────────────────────────── messaging ───────────────────────────
def test_passive_message_delivery_and_self_exclusion(env, capsys, monkeypatch):
    cs.cmd_send(["--from", "pts/0", "--to", "all", "merhaba"])
    capsys.readouterr()  # send ciktisini yut

    # Gonderen (pts/0) kendi mesajini GORMEZ
    monkeypatch.setattr(cs, "_my_tty", lambda: "pts/0")
    cs.cmd_prompt_inject({"session_id": "s0", "cwd": OPT})
    assert capsys.readouterr().out.strip() == ""

    # Alici (pts/1) mesaji GORUR
    monkeypatch.setattr(cs, "_my_tty", lambda: "pts/1")
    cs.cmd_prompt_inject({"session_id": "s1", "cwd": OPT})
    assert "merhaba" in capsys.readouterr().out


def test_passive_message_does_not_block_stop(env, capsys, monkeypatch):
    cs.cmd_send(["--from", "pts/0", "--to", "all", "pasif"])
    capsys.readouterr()
    monkeypatch.setattr(cs, "_my_tty", lambda: "pts/1")
    rc = cs.cmd_stop_check({"session_id": "s1", "stop_hook_active": False})
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_urgent_message_blocks_stop(env, capsys, monkeypatch):
    cs.cmd_send(["--from", "pts/0", "--to", "all", "--urgent", "acil"])
    capsys.readouterr()
    monkeypatch.setattr(cs, "_my_tty", lambda: "pts/1")
    cs.cmd_stop_check({"session_id": "s1", "stop_hook_active": False})
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "acil" in out["reason"]


def test_urgent_respects_stop_hook_active(env, capsys, monkeypatch):
    cs.cmd_send(["--from", "pts/0", "--to", "all", "--urgent", "acil"])
    capsys.readouterr()
    monkeypatch.setattr(cs, "_my_tty", lambda: "pts/1")
    # Loop koruma: zaten Stop-hook block dongusundeyse tekrar block etme
    rc = cs.cmd_stop_check({"session_id": "s1", "stop_hook_active": True})
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


# ───────── broadcast: 3+ alici BAGIMSIZ tuketmeli (Codex P2 regresyon) ─────────
def test_broadcast_passive_reaches_every_recipient(env, capsys, monkeypatch):
    cs.cmd_send(["--from", "pts/0", "--to", "all", "duyuru"])
    capsys.readouterr()

    # pts/1 gorur
    monkeypatch.setattr(cs, "_my_tty", lambda: "pts/1")
    cs.cmd_prompt_inject({"session_id": "s1", "cwd": OPT})
    assert "duyuru" in capsys.readouterr().out

    # pts/2 DE gorur (ilk alici global tuketmedi)
    monkeypatch.setattr(cs, "_my_tty", lambda: "pts/2")
    cs.cmd_prompt_inject({"session_id": "s2", "cwd": OPT})
    assert "duyuru" in capsys.readouterr().out

    # pts/1 TEKRAR gormez (alici-bazli idempotent)
    monkeypatch.setattr(cs, "_my_tty", lambda: "pts/1")
    cs.cmd_prompt_inject({"session_id": "s1", "cwd": OPT})
    assert capsys.readouterr().out.strip() == ""


def test_broadcast_urgent_blocks_every_recipient(env, capsys, monkeypatch):
    cs.cmd_send(["--from", "pts/0", "--to", "all", "--urgent", "acil-duyuru"])
    capsys.readouterr()

    # pts/1 block
    monkeypatch.setattr(cs, "_my_tty", lambda: "pts/1")
    cs.cmd_stop_check({"session_id": "s1", "stop_hook_active": False})
    assert json.loads(capsys.readouterr().out)["decision"] == "block"

    # pts/2 DE block (ilk alici global processed yapmadi)
    monkeypatch.setattr(cs, "_my_tty", lambda: "pts/2")
    cs.cmd_stop_check({"session_id": "s2", "stop_hook_active": False})
    assert json.loads(capsys.readouterr().out)["decision"] == "block"

    # pts/1 TEKRAR block etmez (zaten isledi)
    monkeypatch.setattr(cs, "_my_tty", lambda: "pts/1")
    rc = cs.cmd_stop_check({"session_id": "s1", "stop_hook_active": False})
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


# ───────── migrasyon: eski bool durumunu koru (Codex P2) ─────────
def test_migration_preserves_consumed_state(env, capsys, monkeypatch):
    import sqlite3

    # Eski sema + zaten islenmis (processed=1) urgent mesaj
    con = sqlite3.connect(cs.DB_PATH)
    con.execute(
        "CREATE TABLE session_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "from_sid TEXT, to_sid TEXT, urgent INTEGER DEFAULT 0, content TEXT, "
        "created_at TEXT, delivered_passive INTEGER DEFAULT 0, processed INTEGER DEFAULT 0)"
    )
    con.execute(
        "INSERT INTO session_messages (from_sid,to_sid,urgent,content,created_at,"
        "delivered_passive,processed) VALUES ('pts/0','all',1,'eski',?,0,1)",
        (cs._now(),),
    )
    con.commit()
    con.close()

    # cmd_stop_check -> _db() migrasyonu calistirir; backfill processed_by='*'
    monkeypatch.setattr(cs, "_my_tty", lambda: "pts/1")
    rc = cs.cmd_stop_check({"session_id": "s1", "stop_hook_active": False})
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""  # tekrar block ETMEZ


# ───────── guard: fiili git dizini (cd / git -C) + worktree muafiyeti (Codex P2) ─────────
def test_guard_fires_on_cd_into_opt(env, capsys):
    _add_session("other", "pts/1", OPT)
    cs.cmd_guard(
        {
            "session_id": "me",
            "cwd": "/data/projects/kuafor",
            "tool_input": {"command": f"cd {OPT} && git switch foo"},
        }
    )
    out = capsys.readouterr().out
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_guard_fires_on_git_c_into_opt(env, capsys):
    _add_session("other", "pts/1", OPT)
    cs.cmd_guard(
        {
            "session_id": "me",
            "cwd": "/tmp",
            "tool_input": {"command": f"git -C {OPT} switch foo"},
        }
    )
    out = capsys.readouterr().out
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_guard_detects_cd_after_newline(env, capsys):
    # cok-satirli komut: cd ayri satirda (newline ayraci) -> yine de tespit et
    _add_session("other", "pts/1", OPT)
    cs.cmd_guard(
        {
            "session_id": "me",
            "cwd": "/tmp",
            "tool_input": {"command": f"echo ok\ncd {OPT}\ngit pull"},
        }
    )
    out = capsys.readouterr().out
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_guard_fires_on_git_pull(env, capsys):
    # git pull / pull --rebase de HEAD'i kaydirir -> paylasilan /opt'ta guard'lanmali
    _add_session("other", "pts/1", OPT)
    cs.cmd_guard({"session_id": "me", "cwd": OPT, "tool_input": {"command": "git pull --rebase"}})
    out = capsys.readouterr().out
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_guard_silent_in_worktree(env, capsys):
    # worktree bagimsiz HEAD -> paylasilan ANA checkout collision'i degil
    wt = cs._WORKTREES_PREFIX + "/feat-x"
    _add_session("other", "pts/1", OPT)
    cs.cmd_guard({"session_id": "me", "cwd": wt, "tool_input": {"command": "git switch foo"}})
    assert capsys.readouterr().out.strip() == ""
