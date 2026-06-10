#!/usr/bin/env python3
"""Tum projeleri tek 'klipper-memory' collection altinda index"""

import sqlite3
import sys
import time
import uuid

import requests

DB = "/opt/linux-ai-server/data/claude_memory.db"
QDRANT = "http://localhost:6333"
OLLAMA = "http://localhost:11434"
COLLECTION = "klipper-memory"
EMBED_MODEL = "bge-m3"
VECTOR_SIZE = 1024


def embed(text):
    text = (text or "")[:8000]
    if not text.strip():
        return None
    try:
        r = requests.post(f"{OLLAMA}/api/embeddings", json={"model": EMBED_MODEL, "prompt": text}, timeout=120)
        return r.json().get("embedding")
    except Exception as e:
        print(f"embed err: {e}", file=sys.stderr)
        return None


# Atomik reindex (#566): DROP-then-rebuild ~10dk RAG kesintisi yaratiyordu (rag.py 503).
# Yeni: NEW (versiyonlu) koleksiyona insa et (eski hala servis ediyor) -> atomik alias-swap
# -> eski-sil = SIFIR kesinti. COLLECTION artik ALIAS adi (rag.py bunu sorgular; Qdrant cozer).
ALIAS = COLLECTION
NEW = f"{COLLECTION}-{int(time.time())}"

requests.put(f"{QDRANT}/collections/{NEW}", json={"vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}}, timeout=30)
# Indexler: project/source (filter) + text (full-text, hybrid-retrieval keyword-leg icin)
requests.put(f"{QDRANT}/collections/{NEW}/index", json={"field_name": "project", "field_schema": "keyword"}, timeout=30)
requests.put(f"{QDRANT}/collections/{NEW}/index", json={"field_name": "source", "field_schema": "keyword"}, timeout=30)
requests.put(
    f"{QDRANT}/collections/{NEW}/index",
    json={"field_name": "text", "field_schema": {"type": "text", "tokenizer": "word", "lowercase": True, "min_token_len": 2}},
    timeout=30,
)

conn = sqlite3.connect(DB)
cur = conn.cursor()
points = []
start = time.time()

# 1) Memories
cur.execute("SELECT id, type, name, description, content, source_device FROM memories WHERE active=1 OR active IS NULL")
total_mem = 0
for mid, typ, name, desc, content, src in cur.fetchall():
    text = f"[Memory:{typ}] {name}\n{desc or ''}\n{content or ''}"
    # Proje cikarimi (heuristic - name+desc+content icinde proje adi)
    proj = None
    haystack = f"{name} {desc or ''} {content or ''}".lower()
    for p in [
        "bilge-arena",
        "panola",
        "renderhane",
        "koken-akademi",
        "petvet",
        "kuafor",
        "3d-labx",
        "cadforge",
        "panola-social",
        "linux-ai-server",
        "infra",
        "vps",
        "bilge-arena-en",
        "bilge-english",
    ]:
        if p in haystack:
            proj = p
            break
    if not proj:
        proj = "general"

    vec = embed(text)
    if vec:
        points.append(
            {
                "id": str(uuid.uuid4()),
                "vector": vec,
                "payload": {
                    "source": "memory",
                    "type": typ,
                    "name": name,
                    "project": proj,
                    "memory_id": mid,
                    "source_device": src,
                    "text": text[:2000],
                },
            }
        )
        total_mem += 1
        if total_mem % 100 == 0:
            print(f"  Memories: {total_mem}", flush=True)
print(f"Memories: {total_mem}")

# 2) Discoveries
cur.execute("SELECT id, project, type, title, details, status FROM discoveries")
total_disc = 0
for did, proj, typ, title, details, status in cur.fetchall():
    text = f"[Discovery:{typ}({status})] {title}\n{details or ''}"
    vec = embed(text)
    if vec:
        points.append(
            {
                "id": str(uuid.uuid4()),
                "vector": vec,
                "payload": {
                    "source": "discovery",
                    "type": typ,
                    "title": title,
                    "status": status,
                    "project": proj or "general",
                    "discovery_id": did,
                    "text": text[:2000],
                },
            }
        )
        total_disc += 1
        if total_disc % 100 == 0:
            print(f"  Discoveries: {total_disc}", flush=True)
print(f"Discoveries: {total_disc}")

# 3) Sessions
cur.execute("SELECT id, date, device_name, summary FROM sessions WHERE summary IS NOT NULL")
total_sess = 0
for sid, date, dev, summary in cur.fetchall():
    text = f"[Session {date} {dev}]\n{summary or ''}"
    # Session proje cikarimi summary'den
    sl = (summary or "").lower()
    proj = "general"
    for p in [
        "bilge-arena",
        "panola",
        "renderhane",
        "koken-akademi",
        "petvet",
        "kuafor",
        "3d-labx",
        "cadforge",
        "panola-social",
        "linux-ai-server",
        "infra",
        "vps",
    ]:
        if p in sl:
            proj = p
            break
    vec = embed(text)
    if vec:
        points.append(
            {
                "id": str(uuid.uuid4()),
                "vector": vec,
                "payload": {"source": "session", "date": date, "device": dev, "project": proj, "session_id": sid, "text": text[:2000]},
            }
        )
        total_sess += 1
        if total_sess % 100 == 0:
            print(f"  Sessions: {total_sess}", flush=True)
print(f"Sessions: {total_sess}")

# 4) Tasks
cur.execute("SELECT id, project, task, details, status FROM tasks_log")
total_tasks = 0
for tid, proj, task, details, status in cur.fetchall():
    text = f"[Task({status})] {task}\n{details or ''}"
    vec = embed(text)
    if vec:
        points.append(
            {
                "id": str(uuid.uuid4()),
                "vector": vec,
                "payload": {
                    "source": "task",
                    "task": task,
                    "status": status,
                    "project": proj or "general",
                    "task_id": tid,
                    "text": text[:2000],
                },
            }
        )
        total_tasks += 1
        if total_tasks % 100 == 0:
            print(f"  Tasks: {total_tasks}", flush=True)
print(f"Tasks: {total_tasks}")

conn.close()
print(f"\nTotal: {len(points)} points to upsert (embed sure: {time.time() - start:.0f}s)")

# Upsert batch
t0 = time.time()
for i in range(0, len(points), 100):
    requests.put(f"{QDRANT}/collections/{NEW}/points?wait=true", json={"points": points[i : i + 100]}, timeout=60)
    if i % 500 == 0:
        print(f"  Upserted {i}+...", flush=True)

info = requests.get(f"{QDRANT}/collections/{NEW}", timeout=10).json()["result"]
print(f"\nFinal: {info['points_count']} points, vec_size={info['config']['params']['vectors']['size']}")

# Per-project counts
print("\nProje dagilimi:")
result = requests.post(
    f"{QDRANT}/collections/{NEW}/points/scroll", json={"limit": 10000, "with_payload": ["project"], "with_vector": False}, timeout=30
).json()
from collections import Counter

projs = Counter(p["payload"]["project"] for p in result["result"]["points"])
for proj, cnt in sorted(projs.items(), key=lambda x: -x[1]):
    print(f"  {proj}: {cnt}")

print(f"\nToplam sure: {time.time() - start:.0f}s")


# ── Atomik alias-swap (#566): NEW hazir -> ALIAS'i NEW'a yonlendir + eski-sil = sifir-kesinti ──
def _alias_target():
    """ALIAS bir alias ise hedef-koleksiyon adi, degilse None."""
    try:
        al = requests.get(f"{QDRANT}/aliases", timeout=10).json()
        for a in al.get("result", {}).get("aliases", []):
            if a.get("alias_name") == ALIAS:
                return a.get("collection_name")
    except Exception:
        pass
    return None


def _is_collection(name):
    try:
        cols = requests.get(f"{QDRANT}/collections", timeout=10).json()
        return name in [c["name"] for c in cols.get("result", {}).get("collections", [])]
    except Exception:
        return False


old_target = _alias_target()  # steady-state: onceki versiyonlu koleksiyon (alias hedefi)
# Ilk-migration: ALIAS hala GERCEK koleksiyon (alias degil) -> aliaslamak icin once sil
# (tek-seferlik, ~1sn pencere; sonraki tum reindex'ler tam-atomik).
if old_target is None and _is_collection(ALIAS):
    requests.delete(f"{QDRANT}/collections/{ALIAS}", timeout=30)

actions = []
if old_target:
    actions.append({"delete_alias": {"alias_name": ALIAS}})
actions.append({"create_alias": {"collection_name": NEW, "alias_name": ALIAS}})
swap = requests.post(f"{QDRANT}/collections/aliases", json={"actions": actions}, timeout=30)
if not swap.ok:
    print(f"ALIAS-SWAP FAIL: {swap.status_code} {swap.text[:200]}", file=sys.stderr)
    sys.exit(1)  # ALL_INDEX_OK YAZMA -> cron fail isaretler

# Eski hedefi + sizan eski versiyonlu koleksiyonlari temizle (alias artik NEW'da)
try:
    for c in requests.get(f"{QDRANT}/collections", timeout=10).json().get("result", {}).get("collections", []):
        n = c["name"]
        if n.startswith(f"{ALIAS}-") and n != NEW:
            requests.delete(f"{QDRANT}/collections/{n}", timeout=30)
except Exception:
    pass

print(f"ALIAS '{ALIAS}' -> '{NEW}' (atomik swap, sifir-kesinti)")
print("ALL_INDEX_OK")
