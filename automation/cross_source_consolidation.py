#!/usr/bin/env python3
"""Cross-Source Consolidation Agent — Farklı kaynaklardan öğrenmeleri birleştir.

discoveries (type=learning) + ci_lesson_learned + thoughts tabloslarını analiz eder.
Benzer konuları embedding-cosine ile kümeler, her küme için unified memory (type=reference)
oluşturur. Bu, aynı konunun farklı kaynaklardan gelen öğrenmelerini tek bir canonical
kaynakta birleştirir.

Tasarım:
- Salt-okunur: 3 tabloyu okur, memories'e yazar (NO-DELETE, soft-archive)
- Embedding-based: bge-m3 ile vektör üret, cosine similarity ile cluster
- Fail-safe: DB/Ollama hatası → OUTCOME:fail, crash yok
- Cron: haftalık (Pazar 05:00, memory-synth'ten sonra)

Çıktı formatı (OUTCOME marker cron-wrap için):
- OUTCOME: pass | N küme tespit edildi, M unified memory oluşturuldu
- OUTCOME: partial | N küme, memory yazılamadı: <err>
- OUTCOME: fail | <hata>
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.data_layer import get_conn

ENV_FILE = os.environ.get("NOTIFY_ENV_FILE", "/opt/linux-ai-server/.env")
API_BASE = os.environ.get("API_BASE", "http://localhost:8420")
MEMORY_DB = os.environ.get("MEMORY_DB", "/opt/linux-ai-server/data/claude_memory.db")
SERVER_DB = os.environ.get("DB_PATH", "/opt/linux-ai-server/data/server.db")

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
THRESHOLD = float(os.environ.get("CONSOLIDATION_THRESHOLD", "0.85"))
MIN_CLUSTER = int(os.environ.get("CONSOLIDATION_MIN_CLUSTER", "2"))


def _envget(key: str) -> str:
    v = os.environ.get(key)
    if v:
        return v
    try:
        with open(ENV_FILE) as fh:
            for line in fh:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def _post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    import urllib.request

    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def embed(texts: list[str]) -> list[list[float]]:
    """Ollama /api/embed ile vektör üret (sync). Boş/başarısız → boş liste."""
    if not texts:
        return []
    req = urllib.request.Request(
        f"{OLLAMA}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        embeddings = json.loads(resp.read()).get("embeddings", [])
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


def cluster(items: list[dict], vectors: list[list[float]], threshold: float = THRESHOLD) -> list[list[int]]:
    """cos≥threshold çiftlerini birleştirip bağlı-bileşen kümeleri döndür."""
    n = len(items)
    if n < 2:
        return []

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
        groups.setdefault(find(idx), []).append(idx)

    return [sorted(g) for g in groups.values() if len(g) >= MIN_CLUSTER]


def collect_learning_items(db_path: str | None = None, server_db: str | None = None) -> list[dict] | None:
    """3 kaynaktan öğrenme öğelerini topla: discoveries (type=learning), ci_lesson_learned, thoughts.

    Returns: [{id, source, title, content, timestamp}]
    None: DB okuma hatası
    """
    memory_db = db_path or MEMORY_DB
    srv_db = server_db or SERVER_DB

    items = []

    try:
        con = get_conn(memory_db, readonly=True, busy_timeout_ms=5000)
        if not con:
            return None

        rows = con.execute(
            """
            SELECT id, title, details, created_at
            FROM discoveries
            WHERE type='learning' AND status='active'
            ORDER BY created_at DESC
            LIMIT 100
            """
        ).fetchall()

        for r in rows:
            items.append({
                "id": f"discovery_{r['id']}",
                "source": "discovery",
                "title": r["title"],
                "content": f"{r['title']}\n{r['details'][:500]}",
                "timestamp": r["created_at"],
            })

        con.close()
    except sqlite3.Error:
        pass

    try:
        con = get_conn(srv_db, readonly=True, busy_timeout_ms=5000)
        if not con:
            return None

        rows = con.execute(
            """
            SELECT id, test_name, raw_error, created_at
            FROM ci_lesson_learned
            ORDER BY created_at DESC
            LIMIT 100
            """
        ).fetchall()

        for r in rows:
            items.append({
                "id": f"ci_lesson_{r['id']}",
                "source": "ci_lesson",
                "title": f"CI Lesson: {r['test_name']}",
                "content": f"{r['test_name']}\n{r['raw_error'][:500] if r['raw_error'] else ''}",
                "timestamp": r["created_at"],
            })

        con.close()
    except sqlite3.Error:
        pass

    try:
        con = get_conn(memory_db, readonly=True, busy_timeout_ms=5000)
        if not con:
            return None

        rows = con.execute(
            """
            SELECT id, focus, emotion, content, timestamp
            FROM thoughts
            WHERE is_deep = 1
            ORDER BY timestamp DESC
            LIMIT 100
            """
        ).fetchall()

        for r in rows:
            items.append({
                "id": f"thought_{r['id']}",
                "source": "thought",
                "title": f"Deep thought: {r['focus']}",
                "content": f"{r['focus']} ({r['emotion']})\n{r['content'][:500]}",
                "timestamp": r["timestamp"],
            })

        con.close()
    except sqlite3.Error:
        pass

    return items


def create_unified_memory(cluster_items: list[dict], mkey: str) -> str:
    """Cluster için unified memory oluştur (type=reference)."""
    if not mkey:
        return "no MEMORY_API_KEY"
    if not cluster_items:
        return "no items"

    day_tag = datetime.now(UTC).strftime("%Y-%m-%d")
    sources = list({item["source"] for item in cluster_items})
    titles = [item["title"] for item in cluster_items[:5]]

    name = f"consolidated-learning-{day_tag}-{len(sources)}sources"
    description = f"Cross-source consolidation: {len(cluster_items)} öğe, {len(sources)} kaynak ({', '.join(sources)})"
    content = f"Unified learning from {len(cluster_items)} items:\n\n" + "\n---\n".join(
        f"[{item['source']}] {item['title']}\n{item['content'][:300]}" for item in cluster_items[:5]
    )

    try:
        result = _post_json(
            f"{API_BASE}/api/v1/memory/memories",
            {
                "type": "reference",
                "name": name,
                "description": description,
                "content": content[:3800],
                "source_device": "klipper",
            },
            {"X-Memory-Key": mkey},
            15,
        )
        return ""
    except Exception as e:
        return str(e)[:150]


def main() -> int:
    items = collect_learning_items()
    mkey = _envget("MEMORY_API_KEY")

    if items is None:
        print(f"OUTCOME: fail | DB okuma hatası (memory: {MEMORY_DB}, server: {SERVER_DB})")
        return 1

    if not items:
        print(f"OUTCOME: pass | Öğrenme öğesi yok")
        return 0

    texts = [item["content"] for item in items]
    try:
        vectors = embed(texts)
    except Exception as e:
        print(f"OUTCOME: fail | Embedding hatası: {str(e)[:100]}")
        return 1

    if len(vectors) != len(items):
        print(f"OUTCOME: fail | Embedding sayısı uyuşmuyor: {len(vectors)} != {len(items)}")
        return 1

    clusters = cluster(items, vectors)

    if not clusters:
        print(f"OUTCOME: pass | {len(items)} öğe analiz edildi, küme yok")
        return 0

    created = 0
    errors = []
    for cluster_indices in clusters[:10]:
        cluster_items = [items[i] for i in cluster_indices]
        err = create_unified_memory(cluster_items, mkey)
        if err:
            errors.append(err)
        else:
            created += 1

    if errors:
        print(f"OUTCOME: partial | {len(clusters)} küme, {created} oluşturuldu, {len(errors)} hata: {errors[0][:100]}")
    else:
        print(f"OUTCOME: pass | {len(clusters)} küme tespit edildi, {created} unified memory oluşturuldu")
    return 0


if __name__ == "__main__":
    sys.exit(main())
