#!/usr/bin/env python3
"""Stop hook — turn bitiminde unread klipper notlarini kontrol et.

Akis:
  - Eger okunmamis not yoksa: exit 0 (turn normal sekilde biter)
  - Eger okunmamis not varsa: JSON {"decision": "block", "reason": "..."}
    yazip exit 0. Claude bir sonraki iterasyonda reason metnini gorur ve
    notlari isler. Bu, kullanici prompt'u olmadan otonom inbox processing
    saglar.

Loop prevention:
  - `stop_hook_active` flag (Claude Code tarafindan iletilir) true ise
    hook hicbir sey yapmaz — sonsuz block dongusuni engeller.
  - Ayrica `HOOK_MIN_TURNS` env (default 2) altinda turn sayisi varsa
    yine geri durur.

Hook output Claude Code Stop event icin:
  - JSON stdout: `decision`, `reason` fields
  - Exit 0: normal
  - Exit 2: blocking error (kullanmiyoruz)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

DB_PATH = os.environ.get("HOOK_DB", "/opt/linux-ai-server/data/claude_memory.db")
DEVICE = os.environ.get("HOOK_DEVICE", "klipper")
LOG_DIR = Path(os.environ.get("HOOK_LOG_DIR", "/opt/linux-ai-server/data/hook-logs"))


def log(msg: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "hooks.log").open("a", encoding="utf-8") as f:
            from datetime import datetime

            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [stop-check-inbox] {msg}\n")
    except Exception:
        pass


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw else {}
    except Exception as e:
        log(f"input parse error: {e}")
        return 0

    # Loop prevention: Claude Code'un kendisi flag set ediyor
    if data.get("stop_hook_active"):
        log("stop_hook_active=true, skip (loop prevention)")
        return 0

    # Unread notes query — klipper-targeted VEYA broadcast (to_device IS NULL)
    # Broadcast'lar herkese acik notlardir; klipper'in da gormesi gerekir.
    try:
        con = sqlite3.connect(DB_PATH)
        # PER-DEVICE okunmamış (#647): read_by varsa bu device'a göre filtrele; yoksa legacy.
        # Kolon-guard: read_by henüz yoksa eski global sorgu (sessiz-fail önlenir).
        cols = [r[1] for r in con.execute("PRAGMA table_info(notes)").fetchall()]
        if "read_by" in cols:
            where = "(to_device=? OR to_device IS NULL) AND read=0 AND (read_by IS NULL OR read_by NOT LIKE ?)"
            qparams = (DEVICE, f"%|{DEVICE}|%")
        else:
            where = "(to_device=? OR to_device IS NULL) AND read=0"
            qparams = (DEVICE,)
        cur = con.execute(
            f"SELECT id, from_device, title, substr(content, 1, 400) FROM notes WHERE {where} ORDER BY id",
            qparams,
        )
        rows = cur.fetchall()
        con.close()
    except Exception as e:
        log(f"db query error: {e}")
        return 0

    if not rows:
        return 0

    # Build reason text — Claude bir sonraki iterasyonda gorecek
    lines = [
        f"=== {len(rows)} OKUNMAMIS NOT — ISLEM GEREKLI ===",
        "",
        "Turn'unu kapatmadan once gelen yeni notlari islemen gerekiyor. Bu otonom inbox kontrolu (Stop hook); kullanici prompt'u gerekmez.",
        "",
    ]
    for nid, frm, title, preview in rows:
        title_clean = (title or "").replace("\n", " ")[:80]
        lines.append(f"[#{nid}] {frm} -> {title_clean}")
        if preview:
            preview_clean = preview.replace("\n", " ")[:200]
            lines.append(f"    {preview_clean}...")
        lines.append("")
    lines.extend(
        [
            "Detay icin:",
            f'  sqlite3 {DB_PATH} "SELECT content FROM notes WHERE id IN (...)"',
            "Okundu isaretle (PER-DEVICE — device parametresi ŞART, yoksa global okundu olur):",
            f'  curl -X PUT "http://127.0.0.1:8420/api/v1/memory/notes/<ID>/read?device={DEVICE}" -H "X-Memory-Key: $KEY"',
        ]
    )
    reason = "\n".join(lines)

    output = {"decision": "block", "reason": reason}
    print(json.dumps(output, ensure_ascii=False))
    log(f"blocked stop: {len(rows)} unread notes (ids={[r[0] for r in rows]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
