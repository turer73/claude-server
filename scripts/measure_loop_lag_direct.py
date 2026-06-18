#!/usr/bin/env python3
"""Faz 1 (temiz) — sync sqlite event-loop'u ne kadar blokluyor? HTTP/rate-limit YOK.

Doğrudan ölçüm: bir kalp-atışı (heartbeat) coroutine'i loop gecikmesini örnekler
(5ms hedef aralık; gerçekte ne kadar GEÇ tetiklendiği = loop ne kadar bloklandı).
AYNI ağır-okuma işi iki modda koşar:
  B) loop'ta DOĞRUDAN (mevcut memory.py deseni: async handler içinde sync db.execute)
  C) asyncio.to_thread ile THREADPOOL'da (Faz 2 önizlemesi)
Loop gecikmesi B'de yüksek, C'de düşükse → blokaj gerçek VE to_thread çözer.

READ-ONLY (sadece SELECT/COUNT). Kullanım: venv/bin/python scripts/measure_loop_lag_direct.py
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

DB = "/opt/linux-ai-server/data/claude_memory.db"


def heavy_read() -> int:
    """memory dashboard'a benzer ağır-okuma: tek bağlantı + ~10 sıralı sorgu."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    n = 0
    for sql in (
        "SELECT COUNT(*) FROM memories",
        "SELECT COUNT(*) FROM discoveries",
        "SELECT COUNT(*) FROM discoveries WHERE status='active'",
        "SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active'",
        "SELECT * FROM memories WHERE active=1 ORDER BY updated_at DESC LIMIT 20",
        "SELECT * FROM discoveries WHERE status='active' ORDER BY id DESC LIMIT 30",
        "SELECT id,project,type,title,read_count FROM discoveries ORDER BY read_count DESC LIMIT 10",
        "SELECT COUNT(*) FROM sessions",
        "SELECT * FROM sessions ORDER BY id DESC LIMIT 10",
        "SELECT name,description FROM memories WHERE type='feedback' AND active=1",
    ):
        try:
            n += len(conn.execute(sql).fetchall())
        except Exception:
            pass
    conn.close()
    return n


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))] * 1000  # ms


async def measure(mode: str, duration: float, concurrency: int):
    lag: list[float] = []
    stop = False

    async def heartbeat():
        interval = 0.005
        while not stop:
            t0 = time.perf_counter()
            await asyncio.sleep(interval)
            lag.append((time.perf_counter() - t0) - interval)  # gecikme (geç kalma)

    async def worker():
        while not stop:
            if mode == "loop":
                heavy_read()  # sync — loop'u BLOKLAR
            else:
                await asyncio.to_thread(heavy_read)  # threadpool — loop serbest
            await asyncio.sleep(0)

    hb = asyncio.create_task(heartbeat())
    workers = [asyncio.create_task(worker()) for _ in range(concurrency)] if mode != "idle" else []
    await asyncio.sleep(duration)
    stop = True
    await asyncio.gather(hb, *workers, return_exceptions=True)
    return lag


async def main():
    dur, conc = 3.0, 8
    # tek heavy_read maliyeti (referans)
    t = time.perf_counter()
    rows = heavy_read()
    single = (time.perf_counter() - t) * 1000
    print(f"\n=== Faz 1 (temiz, doğrudan loop-lag) — DB={DB} ===")
    print(f"tek heavy_read: {single:.1f}ms, {rows} satır | {conc} eşzamanlı worker, {dur}s/mod\n")

    idle = await measure("idle", 1.5, 0)
    loopm = await measure("loop", dur, conc)
    thread = await measure("thread", dur, conc)

    print(f"{'event-loop gecikmesi':<40}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}  (ms)")
    for label, xs in (("idle (referans)", idle), ("B) sync DB loop'ta (mevcut)", loopm), ("C) sync DB to_thread (Faz 2)", thread)):
        print(f"{label:<40}{pct(xs,50):>9.2f}{pct(xs,95):>9.2f}{pct(xs,99):>9.2f}{pct(xs,100):>9.2f}")

    lp95, tp95 = pct(loopm, 95), pct(thread, 95)
    print(f"\nloop-blokaj p95: mevcut {lp95:.1f}ms → to_thread {tp95:.1f}ms")
    if lp95 > 50:
        print("VERDİKT: ✅ Ciddi blokaj — Faz 2 (to_thread/aiosqlite) net gerekçeli.")
    elif lp95 > 15:
        print("VERDİKT: 🟡 Ölçülebilir blokaj — Faz 2 değerli AMA rate-limit(200/min) pratikte sınırlıyor → orta öncelik.")
    else:
        print("VERDİKT: ❌ Bu DB+donanımda blokaj küçük → Faz 2/3 ERTELE; busy_timeout + rate-limit yeterli.")


if __name__ == "__main__":
    asyncio.run(main())
