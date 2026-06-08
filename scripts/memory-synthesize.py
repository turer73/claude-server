#!/usr/bin/env python3
"""memory-synthesize.py — Hafıza Sentezi (LIVESYS-MEMSYN).

Hafıza biriktirir ama sentezlemezse şişer + çelişir (746+ kayıt). Bu script benzer
memory'leri embedding-cosine ile kümeler (≥EŞİK), her kümede bir CANONICAL seçer
(en zengin içerik + en çok okunan), diğerlerini merged_into=<canonical_id> + active=0
ile ARŞİVLER. Hiçbir memory SİLİNMEZ (soft-archive + merged_into izi → geri-alınabilir).

GÜVENLİK (surer SPEC):
  - İLK-HAFTA/VARSAYILAN: DRY_RUN — MEMSYN_APPLY=1 değilse YALNIZ öneri yazdırır, DB'ye DOKUNMAZ.
  - APPLY öncesi DB-backup ZORUNLU (timestamped kopya).
  - NO-DELETE: yalnız active=0 + merged_into; satır silinmez, içerik korunur.
  - Idempotent: merged_into kolonu yoksa eklenir (ALTER); zaten-merged kayıt tekrar işlenmez.

v1 KAPSAM (dürüst): deterministik canonical-SEÇİM (LLM yok). LLM-canonical-rewrite ve
pattern-promote v2'ye bırakıldı (DB-yazan ilk sürümde determinizm + test-edilebilirlik öncelik).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import sys
import urllib.request
from typing import Any

ROOT = os.environ.get("LIVESYS_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("MEMORY_DB", os.path.join(ROOT, "data", "claude_memory.db"))
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
THRESHOLD = float(os.environ.get("MEMSYN_THRESHOLD", "0.86"))
APPLY = os.environ.get("MEMSYN_APPLY") == "1"  # default: DRY_RUN
# Staged-apply (surer): APPLY'da YALNIZ ≥MIN_CLUSTER üyeli kümeleri arşivle. Varsayılan 2
# (tüm kümeler). 3 = yalnız büyük-küme (autonomous-log gürültüsü) güvenli-apply; 2-üyeli
# bilgi-çiftleri (FP-riski) review-sonrası ayrıca. DRY_RUN raporu yine TÜM kümeleri gösterir.
MIN_CLUSTER = int(os.environ.get("MEMSYN_MIN_CLUSTER", "2"))


def embed(texts: list[str]) -> list[list[float]]:
    """Ollama /api/embed ile vektör üret (sync). Boş/başarısız → boş liste."""
    if not texts:
        return []
    req = urllib.request.Request(  # noqa: S310 (yerel Ollama, sabit http şeması)
        f"{OLLAMA}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        embeddings: list[list[float]] = json.loads(resp.read()).get("embeddings", [])
        return embeddings


def cosine(a: list[float], b: list[float]) -> float:
    """İki vektör arası kosinüs benzerliği (0..1 normalde)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def cluster(ids: list[int], vectors: list[list[float]], threshold: float = THRESHOLD) -> list[list[int]]:
    """cos≥threshold çiftlerini birleştirip bağlı-bileşen kümeleri döndür (SAF, test-edilebilir).
    Yalnız boyut≥2 küme döner (tekil memory'ler sentezlenmez)."""
    n = len(ids)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    for i in range(n):
        for j in range(i + 1, n):
            if cosine(vectors[i], vectors[j]) >= threshold:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(ids[idx])
    return [sorted(g) for g in groups.values() if len(g) >= 2]


def pick_canonical(members: list[dict[str, Any]]) -> int:
    """Kümede canonical id: en uzun içerik, eşitlikte en çok okunan, eşitlikte en küçük id (kararlı)."""
    best = max(members, key=lambda m: (len(m.get("content") or ""), m.get("read_count") or 0, -m["id"]))
    return int(best["id"])


def _ensure_schema(con: sqlite3.Connection) -> None:
    """merged_into kolonu yoksa ekle (idempotent migration)."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(memories)").fetchall()]
    if "merged_into" not in cols:
        con.execute("ALTER TABLE memories ADD COLUMN merged_into INTEGER")
        con.commit()


def _backup_db() -> str:
    """APPLY öncesi timestamped DB-backup (ZORUNLU). Yedek yolunu döndürür."""
    mtime = int(os.path.getmtime(DB_PATH))
    dst = f"{DB_PATH}.memsyn-bak.{mtime}"
    shutil.copy2(DB_PATH, dst)
    return dst


def _has_merged_into(con: sqlite3.Connection) -> bool:
    return "merged_into" in [r[1] for r in con.execute("PRAGMA table_info(memories)").fetchall()]


def synthesize() -> dict[str, Any]:
    """Kümele + (APPLY ise) arşivle. Özet döndürür. DRY_RUN'da DB'yi MUTATE ETMEZ (Codex P2)."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    has_mi = _has_merged_into(con)
    # Şema migration YALNIZ APPLY'da (backup main()'de alındıktan sonra). DRY_RUN'da ALTER
    # ÇALIŞMAZ → prod-DB dokunulmaz, read-only DB'de patlamaz, /world-model yanlış 'synthesized' demez.
    if APPLY and not has_mi:
        _ensure_schema(con)
        has_mi = True
    sel = "SELECT id, type, name, description, content, read_count FROM memories WHERE active=1"
    if has_mi:
        sel += " AND merged_into IS NULL"
    rows = con.execute(sel).fetchall()
    items = [dict(r) for r in rows]
    by_id = {m["id"]: m for m in items}
    if len(items) < 2:
        con.close()
        return {"total": len(items), "clusters": 0, "archived": 0, "applied": APPLY, "merges": []}

    texts = [f"{m['name']}\n{m['description']}\n{m['content']}" for m in items]
    vectors = embed(texts)
    if len(vectors) != len(items):
        con.close()
        raise RuntimeError(f"embed sayısı uyuşmuyor: {len(vectors)} != {len(items)}")

    clusters = cluster([m["id"] for m in items], vectors, THRESHOLD)
    merges = []
    archived = 0
    skipped_small = 0  # MIN_CLUSTER altındaki kümeler (APPLY'da atlandı, raporda görünür)
    for grp in clusters:
        members = [by_id[i] for i in grp]
        canon = pick_canonical(members)
        losers = [i for i in grp if i != canon]
        applies = len(grp) >= MIN_CLUSTER
        merges.append({"canonical": canon, "merged": losers, "names": [by_id[i]["name"] for i in grp], "size": len(grp)})
        if APPLY and not applies:
            skipped_small += 1
            continue
        if APPLY:
            for lid in losers:
                con.execute(
                    "UPDATE memories SET active=0, merged_into=?, updated_at=datetime('now') WHERE id=?",
                    (canon, lid),
                )
                archived += 1
    if APPLY:
        con.commit()
    con.close()
    return {
        "total": len(items),
        "clusters": len(clusters),
        "archived": archived,
        "skipped_small": skipped_small,
        "min_cluster": MIN_CLUSTER,
        "applied": APPLY,
        "merges": merges,
    }


def main() -> int:
    if not os.path.isfile(DB_PATH):
        print(f"OUTCOME: fail | memory DB yok: {DB_PATH}")
        return 0
    backup = None
    if APPLY:
        try:
            backup = _backup_db()
        except OSError as e:
            print(f"OUTCOME: fail | DB-backup başarısız, APPLY iptal: {e}")
            return 0
    try:
        res = synthesize()
    except (urllib.error.URLError, RuntimeError, sqlite3.Error) as e:
        print(f"OUTCOME: fail | sentez hatası: {e}")
        return 0

    mode = "APPLY" if res["applied"] else "DRY_RUN"
    min_c = res.get("min_cluster", 2)
    for m in res["merges"]:
        # Codex P2: APPLY'da MIN_CLUSTER altı kümeler ATLANIR — onları 'arşiv' diye loglama
        # (yanıltıcı). Skip'leri ayrı etiketle.
        if res["applied"] and m.get("size", 2) < min_c:
            print(f"[SKIP<{min_c}ü] küme→canonical#{m['canonical']} atlandı:{m['merged']} ({', '.join(m['names'][:3])})")
        else:
            print(f"[{mode}] küme→canonical#{m['canonical']} arşiv:{m['merged']} ({', '.join(m['names'][:3])})")
    if backup:
        print(f"[backup] {backup}")

    if res["clusters"] == 0:
        print(f"OUTCOME: pass | memory-synth: {res['total']} aktif, sentezlenecek küme yok")
    elif res["applied"]:
        sk = f", {res['skipped_small']} küme <{res['min_cluster']}üye atlandı" if res.get("skipped_small") else ""
        print(
            f"OUTCOME: pass | memory-synth APPLY(min={res['min_cluster']}): {res['clusters']} küme, "
            f"{res['archived']} arşivlendi (NO-DELETE){sk}"
        )
    else:
        print(f"OUTCOME: partial | memory-synth DRY_RUN: {res['clusters']} küme önerisi (APPLY=1 ile uygula)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
