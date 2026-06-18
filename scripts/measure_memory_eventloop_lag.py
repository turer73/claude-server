#!/usr/bin/env python3
"""Faz 1 ölçümü — memory.py sync-sqlite çağrıları event-loop'u blokluyor mu?

Yöntem (kara-kutu, READ-ONLY — production write yapmaz):
  1. Baseline: idle iken no-DB async /health gecikmesi (p50/p95/p99).
  2. Yük: M eşzamanlı loader, DB-AĞIR GET /api/v1/memory/dashboard'u (10+ sıralı
     query) DURATION saniye döver. AYNI anda AYRI bağlantı havuzlu bir prober
     /health'i ~15ms'de bir örnekler.
  3. Sync DB event-loop'u bloklarsa → yük altında /health gecikmesi fırlar.
     (Prober'ın ayrı client'ı = client-side pool çekişmesi sinyali kirletmez.)

2-worker uyarısı: uvicorn 2 ayrı process/loop → yük dağılır, sinyal SEYRELİR
(blokaj varsa bile etki tek-worker'a göre ~yarı). Yine de fark görünür.

Kullanım: venv/bin/python scripts/measure_memory_eventloop_lag.py [loaders] [duration_s]
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8420"
PROBE_PATH = "/health"  # no-DB, async, sub-ms idle → temiz prob
LOAD_PATH = "/api/v1/memory/dashboard"  # DB-ağır okuma (en güçlü sinyal)


def _read_key() -> str:
    env = Path("/opt/linux-ai-server/.env")
    for line in env.read_text().splitlines():
        if line.startswith("MEMORY_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("MEMORY_API_KEY .env'de bulunamadı")


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))
    return s[i] * 1000  # ms


async def timed_get(client: httpx.AsyncClient, url: str, headers=None) -> tuple[float, int]:
    t = time.perf_counter()
    try:
        r = await client.get(url, headers=headers)
        return time.perf_counter() - t, r.status_code
    except Exception:
        return time.perf_counter() - t, 0


async def main() -> None:
    loaders = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    key = _read_key()
    hdr = {"X-Memory-Key": key}
    limits = httpx.Limits(max_connections=loaders + 10, max_keepalive_connections=loaders + 10)

    # --- Phase A: baseline /health (idle) ---
    async with httpx.AsyncClient(base_url=BASE, limits=limits, timeout=30) as c:
        base = []
        for _ in range(60):
            dt, code = await timed_get(c, PROBE_PATH)
            if code == 200:
                base.append(dt)
            await asyncio.sleep(0.01)

    # --- Phase B: probe /health while hammering DB endpoint (separate pools) ---
    stop = False
    load_lat: list[float] = []
    load_codes: dict[int, int] = {}
    probe_lat: list[float] = []

    async def loader(client):
        while not stop:
            dt, code = await timed_get(client, LOAD_PATH, hdr)
            load_lat.append(dt)
            load_codes[code] = load_codes.get(code, 0) + 1

    async def prober(client):
        while not stop:
            dt, code = await timed_get(client, PROBE_PATH)
            if code == 200:
                probe_lat.append(dt)
            await asyncio.sleep(0.015)

    load_limits = httpx.Limits(max_connections=loaders + 5, max_keepalive_connections=loaders + 5)
    async with (
        httpx.AsyncClient(base_url=BASE, limits=load_limits, timeout=30) as lc,
        httpx.AsyncClient(base_url=BASE, limits=httpx.Limits(max_connections=5), timeout=30) as pc,
    ):
        tasks = [asyncio.create_task(loader(lc)) for _ in range(loaders)]
        tasks.append(asyncio.create_task(prober(pc)))
        await asyncio.sleep(duration)
        stop = True
        await asyncio.gather(*tasks, return_exceptions=True)

    # --- Rapor ---
    rps = len(load_lat) / duration
    print(f"\n=== Faz 1: event-loop blokaj ölçümü ({loaders} loader, {duration}s) ===")
    print(f"Yük endpoint: GET {LOAD_PATH}  | Prob: GET {PROBE_PATH}\n")
    print(f"{'metrik':<34}{'p50':>9}{'p95':>9}{'p99':>9}  (ms)")
    print(f"{'/health idle (baseline)':<34}{pct(base,50):>9.2f}{pct(base,95):>9.2f}{pct(base,99):>9.2f}")
    print(f"{'/health YÜK ALTINDA':<34}{pct(probe_lat,50):>9.2f}{pct(probe_lat,95):>9.2f}{pct(probe_lat,99):>9.2f}")
    print(f"{'dashboard (DB-ağır)':<34}{pct(load_lat,50):>9.2f}{pct(load_lat,95):>9.2f}{pct(load_lat,99):>9.2f}")
    print(f"\ndashboard throughput: {rps:.0f} req/s  | yanıt kodları: {load_codes}")
    print(f"prob örnek sayısı: idle={len(base)} yük={len(probe_lat)}")

    # Verdict
    b95, l95 = pct(base, 95), pct(probe_lat, 95)
    ratio = (l95 / b95) if b95 else 0
    print(f"\n/health p95: idle {b95:.2f}ms → yük {l95:.2f}ms  (x{ratio:.1f})")
    if l95 > 50 and ratio > 3:
        print("VERDİKT: ✅ Event-loop blokajı GERÇEK ve anlamlı → Faz 2 (threadpool/aiosqlite) gerekçeli.")
    elif l95 > 20 and ratio > 2:
        print("VERDİKT: 🟡 Ölçülebilir blokaj var ama ılımlı → Faz 2 düşük öncelik, izle.")
    else:
        print("VERDİKT: ❌ Anlamlı blokaj YOK (bu ölçekte) → Faz 2/3 ERTELE; busy_timeout yeterli.")


if __name__ == "__main__":
    asyncio.run(main())
