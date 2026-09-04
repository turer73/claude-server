"""note-poller.sh AUTONOMOUS_MODE=1 spawn-karar mantığı — bağımsız modül (2026-07-12).

Önceden note-poller.sh içinde `python3 -c "..."` heredoc'unda gömülüydü; kill-switch
(Faz-A §5) + audit-write (§10) eklenirken bağımsız/test-edilebilir modüle çıkarıldı
(embedded-heredoc'ta bash-quote-escaping kırılganlığı + pytest'ten import edilememe).

Kullanım: `printf '%s' "$new_notes" | python3 note_poller_decide.py "$HOOK_DB" "$HOOK_DEVICE" "$last_seen"`
stdin: notes JSON listesi. stdout: yeni spawned_max_id (tek satır). stderr: karar-özeti (log).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys

try:
    from comms_router import route  # bash cagrisi: sys.path[0]=automation/
except ImportError:  # pytest cagrisi: repo-root path'te
    from automation.comms_router import route

_AUTONOMOUS_CLAUDE = "/opt/linux-ai-server/automation/autonomous-claude.sh"

# URGENT-sınıfı anahtar kelimeler (öncelik-skorlama)
_URGENT_KEYWORDS = ("URGENT", "ACIL", "BREACH", "KVKK", "CVE", "SALDIRI", "INCIDENT")
_CLAIM_RELEASE_RE = re.compile(r"^(CLAIM|RELEASE):\s+\S+\s*[—–-]\s+\S")


def read_halt_flag(db_path: str) -> bool:
    """Faz-A §5 kill-switch: autonomous_comms_halt.active oku. FAIL-OPEN (bilinçli): okuma
    başarısız olursa (tablo-yok/DB-locked/eski-DB) eski-davranışa dön (spawn devam) — fail-CLOSED
    burada YANLIŞ-seçim olurdu: geçici-DB-hiccup TÜM otonom-hattı sessizce durdurup 3-haftalık-
    sessiz-ölüm sınıfında YENİ bir kör-nokta yaratırdı (2026-07-12 DeepSeek/OAuth olayından ders).
    Başarısızlık açıkça LOGLANIR (sessiz değil)."""
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            row = conn.execute("SELECT active FROM autonomous_comms_halt WHERE id=1").fetchone()
            return bool(row and row[0])
        finally:
            conn.close()
    except Exception as e:
        sys.stderr.write(f"kill-switch okuma basarisiz (fail-OPEN, spawn devam): {e}\n")
        return False


def write_audit(db_path: str, device: str, note_id: int, action: str, detail: str = "") -> None:
    """Faz-A §10 append-only audit-substrat. Tablo yoksa idempotent CREATE ile kendi-kurar
    (cross-language coupling'e güvenmez). Best-effort — yazım-hatası spawn-akışını bozmaz."""
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS autonomous_comms_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER, note_id INTEGER,
                device TEXT NOT NULL, action TEXT NOT NULL, detail TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')))"""
            )
            conn.execute(
                "INSERT INTO autonomous_comms_audit (note_id, device, action, detail) VALUES (?,?,?,?)",
                (note_id, device, action, detail[:300]),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        sys.stderr.write(f"audit-yazim basarisiz (best-effort, spawn-akisini bozmaz): {e}\n")


def read_budget(db_path: str):
    """Faz-C SS7 global butce (gunluk spawn sayaci). FAIL-OPEN: okuma/kurma hatasinda
    None doner (limit yok, eski davranis) -- halt-flag ile ayni gerekce (fail-CLOSED
    gecici-DB-hiccup'ta otonom-hatti sessizce durdururdu)."""
    import datetime

    today = datetime.date.today().isoformat()
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """CREATE TABLE IF NOT EXISTS autonomous_comms_budget (
                id INTEGER PRIMARY KEY CHECK (id=1),
                spawns_today INTEGER NOT NULL DEFAULT 0,
                daily_spawn_limit INTEGER NOT NULL DEFAULT 50,
                threads_created_today INTEGER NOT NULL DEFAULT 0,
                daily_thread_limit INTEGER NOT NULL DEFAULT 10,
                daily_token_limit INTEGER NOT NULL DEFAULT 200000,
                day TEXT NOT NULL DEFAULT '')"""
            )
            row = conn.execute("SELECT * FROM autonomous_comms_budget WHERE id=1").fetchone()
            if not row:
                conn.execute("INSERT INTO autonomous_comms_budget (id, day) VALUES (1, ?)", (today,))
                conn.commit()
                return {"spawns_today": 0, "daily_spawn_limit": 50, "day": today}
            if row["day"] != today:
                conn.execute(
                    "UPDATE autonomous_comms_budget SET spawns_today=0, threads_created_today=0, day=? WHERE id=1",
                    (today,),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM autonomous_comms_budget WHERE id=1").fetchone()
            return {
                "spawns_today": row["spawns_today"],
                "daily_spawn_limit": row["daily_spawn_limit"],
                "day": row["day"],
            }
        finally:
            conn.close()
    except Exception as e:
        sys.stderr.write(f"budget okuma basarisiz (fail-OPEN, limit yok): {e}\n")
        return None


def increment_budget(db_path: str) -> None:
    """SS7: spawn sonrasi gunluk sayaci artir (best-effort, spawn akisini bozmaz)."""
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            conn.execute("UPDATE autonomous_comms_budget SET spawns_today = spawns_today + 1 WHERE id=1")
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        sys.stderr.write(f"budget artirma basarisiz (best-effort): {e}\n")


def _score(n: dict) -> int:
    """Öncelik-skoru (yüksek = önce spawn). Ters-çevrilir (sort asc -> high-priority-first)."""
    title = (n.get("title") or "").upper()
    s = 0
    if any(k in title for k in _URGENT_KEYWORDS):
        s += 1000
    if "GOREV PAKETI" in title or "gorev_paketi" in (n.get("preview") or "").lower():
        s += 500
    if title.startswith("ACK"):
        s -= 100
    s += n["id"]
    return -s


def decide(notes: list[dict], db_path: str, device: str) -> dict:
    """Spawn-kararlarını hesapla + audit-yaz + (test-edilebilirlik için) spawn ETME —
    çağıran (main/test) `spawned` listesini kullanıp gerçek Popen'ı kendisi yapar."""
    halt = read_halt_flag(db_path)
    budget = read_budget(db_path)
    sorted_notes = sorted(notes, key=_score)

    spawned: list[dict] = []
    skipped_self: list[int] = []
    skipped_protocol: list[int] = []
    skipped_halt: list[int] = []
    deferred_rate_limit: list[int] = []
    deferred_budget: list[int] = []

    if halt:
        for n in sorted_notes:
            skipped_halt.append(n["id"])
            write_audit(db_path, device, n["id"], "skipped_halt", "autonomous_comms_halt.active=1")
    else:
        source_count: dict[str, int] = {}
        for n in sorted_notes:
            # DONGU KIR (Codex P2): klipper'in kendi 'URGENT: Threat #' notuna spawn ETME.
            if n["from_device"] == "klipper" and (n.get("title") or "").startswith("URGENT: Threat #"):
                skipped_self.append(n["id"])
                write_audit(db_path, device, n["id"], "skipped_self", "kendi-threat-notu")
                continue
            # disc#1289: CLAIM/RELEASE protokol-notu = iş-talebi DEĞİL, koordinasyon-işareti.
            title = n.get("title") or ""
            if n["from_device"] in ("surer", "klipper", "opencode") and _CLAIM_RELEASE_RE.match(title):
                skipped_protocol.append(n["id"])
                write_audit(db_path, device, n["id"], "skipped_protocol", "CLAIM/RELEASE-isareti")
                continue
            src = n["from_device"]
            cnt = source_count.get(src, 0)
            if cnt >= 3:
                deferred_rate_limit.append(n["id"])
                write_audit(db_path, device, n["id"], "deferred_rate_limit", f"source={src} cnt>=3")
                continue
            source_count[src] = cnt + 1
            if budget and budget["spawns_today"] >= budget["daily_spawn_limit"]:
                deferred_budget.append(n["id"])
                write_audit(db_path, device, n["id"], "deferred_budget", f"limit={budget['daily_spawn_limit']}")
                continue
            verdict = route(
                n.get("msg_type") or "legacy",
                int(n.get("hop_count") or 0),
                thread_state=n.get("thread_state") or "open",
                halt_active=False,
            )
            write_audit(db_path, device, n["id"], "route_verdict", f"verdict={verdict}")
            if budget:
                budget["spawns_today"] += 1
            spawned.append(n)

    return {
        "halt": halt,
        "spawned": spawned,
        "skipped_self": skipped_self,
        "skipped_protocol": skipped_protocol,
        "skipped_halt": skipped_halt,
        "deferred_rate_limit": deferred_rate_limit,
        "deferred_budget": deferred_budget,
    }


def spawn(n: dict, db_path: str, device: str) -> None:
    nid = n["id"]
    frm = n["from_device"]
    title = (n.get("title") or "")[:200]
    preview = (n.get("preview") or "")[:500]
    cmd = [_AUTONOMOUS_CLAUDE, str(nid), frm, title, preview]
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "ENFORCE_INTERACTIVE_CHECK": "1"},
    )
    write_audit(db_path, device, nid, "spawned", f"from={frm}")
    increment_budget(db_path)


def main() -> None:
    if len(sys.argv) < 3:
        sys.stderr.write("Kullanım: note_poller_decide.py <db_path> <device> [last_seen]\n")
        sys.exit(1)
    db_path = sys.argv[1]
    device = sys.argv[2]
    last_seen = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    try:
        raw_notes = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"stdin JSON-parse basarisiz (spawn atlanir, last_seen korunur): {e}\n")
        print(last_seen)
        return

    notes = []
    for n in raw_notes:
        if not isinstance(n, dict) or "id" not in n or "from_device" not in n:
            sys.stderr.write(f"gecersiz not (id/from_device eksik, atlanir): {n}\n")
            continue
        notes.append(n)

    result = decide(notes, db_path, device)
    for n in result["spawned"]:
        spawn(n, db_path, device)

    handled_ids = [n["id"] for n in result["spawned"]] + result["skipped_self"] + result["skipped_protocol"] + result["skipped_halt"] + result["deferred_budget"]
    spawned_max = max(handled_ids) if handled_ids else last_seen

    sys.stderr.write(
        f"halt={result['halt']} spawned (priority order): {[n['id'] for n in result['spawned']]} "
        f"skipped_self: {result['skipped_self']} skipped_protocol: {result['skipped_protocol']} "
        f"skipped_halt: {result['skipped_halt']} deferred: {result['deferred_rate_limit']}\n"
    )
    print(spawned_max)


if __name__ == "__main__":
    main()
