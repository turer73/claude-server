#!/usr/bin/env python3
"""Co-session koordinasyon — birden fazla canli Claude oturumunun birbirinden
haberdar olmasi + carpisma-onleme + oturum-kapsamli mesajlasma.

DURUST SINIR (abartma yok):
  - Bosta bekleyen bir oturuma DISARIDAN turn enjekte etmek MUMKUN DEGIL.
    Teslim daima alici oturumun KENDI aktivite noktalarinda olur:
    SessionStart / UserPromptSubmit / PostToolUse / Stop.
  - Canlilik = claude PID'i /proc'ta var + son heartbeat yakin. kill -9 olan
    oturum, bir sonraki prune'da 'dead' isaretlenir (aninda degil).
  - Mevcut surer<->klipper `notes` akisina DOKUNMAZ; ayri tablo kullanir.

Alt komutlar (hepsi stdin'den Claude Code hook JSON'u okur, hata->exit 0):
  heartbeat      generic — sadece registry'yi gunceller
  banner         SessionStart — heartbeat + prune + co-session/mesaj banneri (stdout)
  prompt-inject  UserPromptSubmit — heartbeat + bekleyen PASIF mesajlari enjekte
  stop-check     Stop — heartbeat + bekleyen URGENT mesaj varsa block JSON
  guard          PreToolUse(Bash) — /opt git-dal islemi + baska canli oturum -> 'ask'
  end            SessionEnd — oturumu 'dead' isaretle
  prune          olu PID'leri 'dead' isaretle
  list           registry + mesaj ozeti (insan-okur, CLI status)
  send           oturum-mesaji yaz (CLI: --to SID|all [--urgent] --from X "text")
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta

DB_PATH = os.environ.get("HOOK_DB", "/opt/linux-ai-server/data/claude_memory.db")
LOG_DIR = os.environ.get("HOOK_LOG_DIR", "/opt/linux-ai-server/data/hook-logs")
LIVE_WINDOW_MIN = int(os.environ.get("COSESSION_LIVE_MIN", "240"))  # heartbeat tazelik

# /opt git-dal islemleri: HEAD'i kaydiran / calisma-agacini degistiren komutlar
_GIT_BRANCH_OPS = re.compile(
    r"git\s+(checkout|switch|reset(\s+--hard)?|rebase|merge|cherry-pick|"
    r"stash\s+(pop|apply|drop)|branch\s+-[a-zA-Z]*[Dd])",
)
_SHARED_PREFIX = "/opt/linux-ai-server"


def _log(msg: str) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "hooks.log"), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [cosession] {msg}\n")
    except Exception:
        pass


def _utcnow() -> datetime:
    # naive-UTC: SQLite datetime('now') ile tutarli + strptime(naive) ile karsilastirilir
    return datetime.now(UTC).replace(tzinfo=None)


def _now() -> str:
    return _utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _read_input() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


# ───────────────────────── process/identity helpers ─────────────────────────
def _comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


def _ppid(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        # format: pid (comm) state ppid ...  — comm parantezli, bosluk icerebilir
        after = data[data.rfind(")") + 1 :].split()
        return int(after[1])
    except (OSError, ValueError, IndexError):
        return 0


def _find_claude_pid() -> int:
    """Hook child process'inden yukari yuruyerek sahip 'claude' PID'ini bul."""
    pid = os.getppid()
    for _ in range(15):
        if pid <= 1:
            break
        if _comm(pid) == "claude":
            return pid
        pid = _ppid(pid)
    return os.getppid()


def _pid_alive(pid: int) -> bool:
    return bool(pid) and _comm(pid) == "claude"


def _tty(pid: int) -> str:
    try:
        out = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        return out or "?"
    except Exception:
        return "?"


def _my_tty() -> str:
    """Bu hook/CLI cagrisinin sahibi claude oturumunun tty'si — mesaj adresleme
    anahtari (session_id CLI'dan turetilemez, tty her iki yerde de turetilebilir).
    COSESSION_FORCE_TTY: yalniz test/simulasyon icin override."""
    forced = os.environ.get("COSESSION_FORCE_TTY")
    return forced if forced else _tty(_find_claude_pid())


def _git_branch(cwd: str) -> str:
    if not cwd or not os.path.isdir(cwd):
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


# ───────────────────────────── db / schema ──────────────────────────────────
def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=8)
    con.execute("PRAGMA busy_timeout=8000")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS session_registry (
          session_id  TEXT PRIMARY KEY,
          claude_pid  INTEGER,
          tty         TEXT,
          cwd         TEXT,
          git_branch  TEXT,
          started_at  TEXT,
          last_seen   TEXT,
          last_event  TEXT,
          status      TEXT DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS session_messages (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          from_sid      TEXT,
          to_sid        TEXT,            -- belirli tty VEYA 'all' (broadcast)
          urgent        INTEGER DEFAULT 0,
          content       TEXT,
          created_at    TEXT,
          -- broadcast'ta her alici BAGIMSIZ tuketir: tek-bool yerine tty-listesi
          delivered_to  TEXT DEFAULT '',  -- pasif gosterilen tty'ler (CSV)
          processed_by  TEXT DEFAULT ''   -- urgent islenen tty'ler (CSV)
        );
        """
    )
    # Eski sema (delivered_passive/processed bool) -> yeni tty-listesi migrasyonu
    cols = {r[1] for r in con.execute("PRAGMA table_info(session_messages)")}
    if "delivered_to" not in cols:
        con.execute("ALTER TABLE session_messages ADD COLUMN delivered_to TEXT DEFAULT ''")
    if "processed_by" not in cols:
        con.execute("ALTER TABLE session_messages ADD COLUMN processed_by TEXT DEFAULT ''")
    con.commit()
    return con


def _consumed(csv: str, tty: str) -> bool:
    return tty in (csv or "").split(",")


def _mark(csv: str, tty: str) -> str:
    parts = [x for x in (csv or "").split(",") if x]
    if tty not in parts:
        parts.append(tty)
    return ",".join(parts)


def _heartbeat(con: sqlite3.Connection, data: dict, event: str) -> str:
    """Registry'yi guncelle/ekle. Donen: session_id."""
    sid = (data.get("session_id") or "unknown")[:64]
    cwd = data.get("cwd") or ""
    pid = _find_claude_pid()
    now = _now()
    row = con.execute(
        "SELECT session_id, started_at FROM session_registry WHERE session_id=?",
        (sid,),
    ).fetchone()
    if row:
        con.execute(
            "UPDATE session_registry SET claude_pid=?, cwd=?, git_branch=?, last_seen=?, last_event=?, status='active' WHERE session_id=?",
            (pid, cwd, _git_branch(cwd), now, event, sid),
        )
    else:
        con.execute(
            "INSERT INTO session_registry "
            "(session_id, claude_pid, tty, cwd, git_branch, started_at, "
            " last_seen, last_event, status) "
            "VALUES (?,?,?,?,?,?,?,?, 'active')",
            (sid, pid, _tty(pid), cwd, _git_branch(cwd), now, now, event),
        )
    con.commit()
    return sid


def _prune(con: sqlite3.Connection) -> None:
    """PID'i artik canli olmayan oturumlari 'dead' isaretle."""
    rows = con.execute("SELECT session_id, claude_pid FROM session_registry WHERE status='active'").fetchall()
    dead = [sid for sid, pid in rows if not _pid_alive(pid or 0)]
    for sid in dead:
        con.execute("UPDATE session_registry SET status='dead' WHERE session_id=?", (sid,))
    if dead:
        con.commit()
        _log(f"pruned dead sessions: {dead}")


def _live_others(con: sqlite3.Connection, sid: str) -> list:
    cutoff = (_utcnow() - timedelta(minutes=LIVE_WINDOW_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    rows = con.execute(
        "SELECT session_id, claude_pid, tty, cwd, git_branch, last_seen, last_event "
        "FROM session_registry WHERE status='active' AND session_id!=? "
        "AND last_seen>=? ORDER BY last_seen DESC",
        (sid, cutoff),
    ).fetchall()
    # PID dogrulamasi (registry status gecikebilir)
    return [r for r in rows if _pid_alive(r[1] or 0)]


def _ago(ts: str) -> str:
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        secs = (_utcnow() - dt).total_seconds()
        if secs < 90:
            return f"{int(secs)}sn once"
        if secs < 5400:
            return f"{int(secs // 60)}dk once"
        return f"{int(secs // 3600)}sa once"
    except Exception:
        return ts or "?"


# ───────────────────────────── subcommands ──────────────────────────────────
def cmd_heartbeat(data: dict, event: str = "heartbeat") -> int:
    try:
        con = _db()
        _heartbeat(con, data, event)
        con.close()
    except Exception as e:
        _log(f"heartbeat error: {e}")
    return 0


def cmd_banner(data: dict) -> int:
    try:
        con = _db()
        sid = _heartbeat(con, data, "SessionStart")
        _prune(con)
        others = _live_others(con, sid)
        mytty = _my_tty()
        cand = con.execute(
            "SELECT delivered_to, processed_by FROM session_messages WHERE (to_sid=? OR to_sid='all') AND from_sid!=?",
            (mytty, mytty),
        ).fetchall()
        # benim tarafimdan henuz tuketilmemis (ne pasif gosterilmis ne islenmis)
        msgs = sum(1 for dv, pr in cand if not _consumed(dv, mytty) and not _consumed(pr, mytty))
        con.close()
    except Exception as e:
        _log(f"banner error: {e}")
        return 0

    if not others and not msgs:
        return 0  # tek oturum / mesaj yok -> sessiz

    out = ["=== OTURUM KOORDINASYONU (cosession) ==="]
    if others:
        out.append(f"⚠️  {len(others) + 1} canli Claude oturumu acik (sen + {len(others)}):")
        for s, pid, tty, cwd, br, seen, ev in others:
            out.append(f"  • {tty} [pid {pid}] cwd={cwd or '?'} branch={br or '?'} — son aktif {_ago(seen)} ({ev})")
        out.append("")
        out.append(
            "CARPISMA KURALI: ayni anda /opt/linux-ai-server'da branch "
            "checkout/reset/rebase YAPMAYIN (PreToolUse guard onay isteyecek). "
            'Mesaj: bash /opt/linux-ai-server/scripts/claude-sessions.sh msg "..."'
        )
    if msgs:
        out.append(f"📬 {msgs} okunmamis oturum-mesaji var (prompt'unda gosterilecek; acil olanlar otomatik islenir).")
    print("\n".join(out))
    return 0


def cmd_prompt_inject(data: dict) -> int:
    try:
        con = _db()
        _heartbeat(con, data, "UserPromptSubmit")
        mytty = _my_tty()
        cand = con.execute(
            "SELECT id, from_sid, urgent, content, created_at, delivered_to "
            "FROM session_messages WHERE (to_sid=? OR to_sid='all') AND from_sid!=? "
            "ORDER BY id LIMIT 30",
            (mytty, mytty),
        ).fetchall()
        # broadcast: her alici BAGIMSIZ — yalniz BENIM tty pasif-gormediyse goster
        rows = [r for r in cand if not _consumed(r[5], mytty)][:8]
        for mid, _frm, _urg, _content, _ts, dv in rows:
            con.execute(
                "UPDATE session_messages SET delivered_to=? WHERE id=?",
                (_mark(dv, mytty), mid),
            )
        if rows:
            con.commit()
        con.close()
    except Exception as e:
        _log(f"prompt-inject error: {e}")
        return 0

    if not rows:
        return 0
    out = ["=== COSESSION — Diger Oturumdan Mesaj ==="]
    for mid, frm, urg, content, ts, _dv in rows:
        tag = "🔴ACIL " if urg else ""
        out.append(f"[#{mid} | {ts}] {tag}{frm}:")
        out.append(f"  {(content or '').strip()[:500]}")
    out.append("")
    out.append('Yanit: bash /opt/linux-ai-server/scripts/claude-sessions.sh msg "..."')
    print("\n".join(out))
    return 0


def cmd_stop_check(data: dict) -> int:
    # Loop koruma: zaten bir Stop-hook block dongusundeyse dokunma
    if data.get("stop_hook_active"):
        return 0
    try:
        con = _db()
        _heartbeat(con, data, "Stop")
        mytty = _my_tty()
        cand = con.execute(
            "SELECT id, from_sid, content, created_at, processed_by FROM session_messages "
            "WHERE (to_sid=? OR to_sid='all') AND from_sid!=? AND urgent=1 ORDER BY id",
            (mytty, mytty),
        ).fetchall()
        # broadcast urgent: her alici BAGIMSIZ isler — yalniz BENIM tty islemediyse
        rows = [r for r in cand if not _consumed(r[4], mytty)]
        for mid, _frm, _content, _ts, pb in rows:
            con.execute(
                "UPDATE session_messages SET processed_by=? WHERE id=?",
                (_mark(pb, mytty), mid),
            )
        if rows:
            con.commit()
        con.close()
    except Exception as e:
        _log(f"stop-check error: {e}")
        return 0

    if not rows:
        return 0
    lines = [
        f"=== {len(rows)} ACIL OTURUM-MESAJI — ISLEM GEREKLI ===",
        "Baska bir canli Claude oturumu sana acil mesaj gonderdi. "
        "Turn'unu kapatmadan once degerlendir (otonom; kullanici prompt'u gerekmez):",
        "",
    ]
    for mid, frm, content, ts, _pb in rows:
        lines.append(f"[#{mid} | {ts}] {frm}: {(content or '').strip()[:400]}")
    lines.append("")
    lines.append('Yanitlamak icin: bash /opt/linux-ai-server/scripts/claude-sessions.sh msg "..."')
    print(json.dumps({"decision": "block", "reason": "\n".join(lines)}, ensure_ascii=False))
    _log(f"stop block: urgent msgs {[r[0] for r in rows]} -> {mytty}")
    return 0


def cmd_guard(data: dict) -> int:
    """PreToolUse(Bash): /opt git-dal islemi + baska canli oturum /opt'ta -> 'ask'."""
    cmd = (data.get("tool_input") or {}).get("command", "")
    if not cmd or not _GIT_BRANCH_OPS.search(cmd):
        return 0
    # Sadece komut PAYLASILAN /opt deposunda calisiyorsa anlamli (baska repodaki
    # git-dal islemi /opt landmine'ini etkilemez). Aktor cwd /opt'ta degilse cik.
    actor_cwd = data.get("cwd") or ""
    if not actor_cwd.startswith(_SHARED_PREFIX):
        return 0
    try:
        con = _db()
        sid = _heartbeat(con, data, "PreToolUse")
        _prune(con)
        others = [r for r in _live_others(con, sid) if (r[3] or "").startswith(_SHARED_PREFIX)]
        con.close()
    except Exception as e:
        _log(f"guard error: {e}")
        return 0
    if not others:
        return 0  # tek basina /opt'ta -> serbest

    who = "; ".join(f"{r[2]}[pid {r[1]}] branch={r[4] or '?'} (aktif {_ago(r[5])})" for r in others)
    reason = (
        f"COSESSION CARPISMA RISKI: {len(others)} baska canli oturum da "
        f"/opt/linux-ai-server'da calisiyor ({who}). Bu komut HEAD'i/calisma-agacini "
        f"kaydirir ve diger oturum(lar)i bozabilir (detached-HEAD + servis "
        f"restart-landmine). Devam etmeden once kullaniciyla teyit et; gerekiyorsa "
        f"branch isini worktree'de yap (/opt daima master kalmali)."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )
    _log(f"guard ASK: sid={sid} cmd={cmd[:80]!r} others={[r[0] for r in others]}")
    return 0


def cmd_end(data: dict) -> int:
    try:
        con = _db()
        sid = (data.get("session_id") or "")[:64]
        if sid:
            con.execute(
                "UPDATE session_registry SET status='dead', last_seen=?, last_event='SessionEnd' WHERE session_id=?",
                (_now(), sid),
            )
            con.commit()
        con.close()
    except Exception as e:
        _log(f"end error: {e}")
    return 0


def cmd_prune(_data: dict) -> int:
    try:
        con = _db()
        _prune(con)
        con.close()
    except Exception as e:
        _log(f"prune error: {e}")
    return 0


def cmd_list(_data: dict) -> int:
    try:
        con = _db()
        _prune(con)
        rows = con.execute(
            "SELECT session_id, claude_pid, tty, cwd, git_branch, last_seen, "
            "last_event, status FROM session_registry ORDER BY last_seen DESC LIMIT 30"
        ).fetchall()
        pend = con.execute(
            "SELECT id, from_sid, to_sid, urgent, content, created_at, "
            "delivered_to, processed_by FROM session_messages "
            "ORDER BY id DESC LIMIT 15"
        ).fetchall()
        con.close()
    except Exception as e:
        print(f"hata: {e}")
        return 1
    live = [r for r in rows if r[7] == "active" and _pid_alive(r[1] or 0)]
    print(f"=== CANLI OTURUMLAR ({len(live)}) ===")
    for s, pid, tty, cwd, br, seen, ev, st in rows:
        alive = st == "active" and _pid_alive(pid or 0)
        flag = "🟢" if alive else "⚫"
        print(f"{flag} {tty:8} pid={pid} branch={br or '?':22} cwd={cwd or '?'}")
        print(f"     son: {_ago(seen)} ({ev})  sid={s[:18]}  durum={'canli' if alive else st}")
    if pend:
        print(f"\n=== SON OTURUM-MESAJLARI ({len(pend)}) ===")
        for mid, frm, to, urg, content, ts, dv, pb in pend:
            tag = "🔴ACIL" if urg else "pasif"
            seen = (dv or "") + ("|" + pb if pb else "")
            print(f"#{mid} [{tag}] {frm[:10]}->{to[:10]} tuketen=[{seen}]: {(content or '')[:50]}")
    return 0


def cmd_send(argv: list) -> int:
    """CLI: send [--to SID|all] [--urgent] [--from X] "text" """
    to_sid, urgent, from_sid, text = "all", 0, None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--to" and i + 1 < len(argv):
            to_sid = argv[i + 1]
            i += 2
        elif a == "--urgent":
            urgent = 1
            i += 1
        elif a == "--from" and i + 1 < len(argv):
            from_sid = argv[i + 1]
            i += 2
        else:
            text = a if text is None else f"{text} {a}"
            i += 1
    if not text:
        print('kullanim: send [--to pts/N|all] [--urgent] [--from X] "mesaj"')
        return 1
    if not from_sid:
        from_sid = _my_tty()  # gonderenin tty'si (kendine teslim edilmez)
    try:
        con = _db()
        cur = con.execute(
            "INSERT INTO session_messages (from_sid, to_sid, urgent, content, created_at) VALUES (?,?,?,?,?)",
            (from_sid, to_sid, urgent, text, _now()),
        )
        con.commit()
        mid = cur.lastrowid
        con.close()
    except Exception as e:
        print(f"hata: {e}")
        return 1
    kind = "ACIL (otonom)" if urgent else "pasif"
    print(f"gonderildi: msg #{mid} -> {to_sid} [{kind}]")
    return 0


_HANDLERS = {
    "heartbeat": lambda d: cmd_heartbeat(d),
    "banner": cmd_banner,
    "prompt-inject": cmd_prompt_inject,
    "stop-check": cmd_stop_check,
    "guard": cmd_guard,
    "end": cmd_end,
    "prune": cmd_prune,
    "list": cmd_list,
}


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "heartbeat"
    if cmd == "send":
        return cmd_send(sys.argv[2:])
    if cmd == "list":
        return cmd_list({})
    handler = _HANDLERS.get(cmd)
    if handler is None:
        print(f"bilinmeyen komut: {cmd}", file=sys.stderr)
        return 0
    return handler(_read_input())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # hicbir kosulda oturumu bozma
        _log(f"fatal: {e}")
        sys.exit(0)
