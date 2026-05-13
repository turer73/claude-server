#!/usr/bin/env python3
"""Tum projeleri tek 'klipper-memory' collection altinda index"""
import sqlite3, requests, uuid, time, sys

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
        r = requests.post(f"{OLLAMA}/api/embeddings", json={
            "model": EMBED_MODEL, "prompt": text
        }, timeout=120)
        return r.json().get("embedding")
    except Exception as e:
        print(f"embed err: {e}", file=sys.stderr)
        return None

# Drop+recreate
requests.delete(f"{QDRANT}/collections/{COLLECTION}")
requests.put(f"{QDRANT}/collections/{COLLECTION}", json={
    "vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}
})
# Index for project filter
requests.put(f"{QDRANT}/collections/{COLLECTION}/index", json={
    "field_name": "project", "field_schema": "keyword"
})
requests.put(f"{QDRANT}/collections/{COLLECTION}/index", json={
    "field_name": "source", "field_schema": "keyword"
})

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
    for p in ["bilge-arena", "panola", "renderhane", "koken-akademi", "petvet", "kuafor", "3d-labx", "cadforge", "panola-social", "linux-ai-server", "infra", "vps", "bilge-arena-en", "bilge-english"]:
        if p in haystack:
            proj = p
            break
    if not proj:
        proj = "general"
    
    vec = embed(text)
    if vec:
        points.append({"id": str(uuid.uuid4()), "vector": vec, "payload": {
            "source": "memory", "type": typ, "name": name, "project": proj,
            "memory_id": mid, "source_device": src, "text": text[:2000]
        }})
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
        points.append({"id": str(uuid.uuid4()), "vector": vec, "payload": {
            "source": "discovery", "type": typ, "title": title, "status": status,
            "project": proj or "general", "discovery_id": did, "text": text[:2000]
        }})
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
    for p in ["bilge-arena", "panola", "renderhane", "koken-akademi", "petvet", "kuafor", "3d-labx", "cadforge", "panola-social", "linux-ai-server", "infra", "vps"]:
        if p in sl:
            proj = p
            break
    vec = embed(text)
    if vec:
        points.append({"id": str(uuid.uuid4()), "vector": vec, "payload": {
            "source": "session", "date": date, "device": dev,
            "project": proj, "session_id": sid, "text": text[:2000]
        }})
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
        points.append({"id": str(uuid.uuid4()), "vector": vec, "payload": {
            "source": "task", "task": task, "status": status,
            "project": proj or "general", "task_id": tid, "text": text[:2000]
        }})
        total_tasks += 1
        if total_tasks % 100 == 0:
            print(f"  Tasks: {total_tasks}", flush=True)
print(f"Tasks: {total_tasks}")

conn.close()
print(f"\nTotal: {len(points)} points to upsert (embed sure: {time.time()-start:.0f}s)")

# Upsert batch
t0 = time.time()
for i in range(0, len(points), 100):
    requests.put(f"{QDRANT}/collections/{COLLECTION}/points?wait=true",
                 json={"points": points[i:i+100]}, timeout=60)
    if i % 500 == 0:
        print(f"  Upserted {i}+...", flush=True)

info = requests.get(f"{QDRANT}/collections/{COLLECTION}").json()["result"]
print(f"\nFinal: {info['points_count']} points, vec_size={info['config']['params']['vectors']['size']}")

# Per-project counts
print("\nProje dagilimi:")
result = requests.post(f"{QDRANT}/collections/{COLLECTION}/points/scroll", json={
    "limit": 10000, "with_payload": ["project"], "with_vector": False
}).json()
from collections import Counter
projs = Counter(p["payload"]["project"] for p in result["result"]["points"])
for proj, cnt in sorted(projs.items(), key=lambda x: -x[1]):
    print(f"  {proj}: {cnt}")

print(f"\nToplam sure: {time.time()-start:.0f}s")
print("ALL_INDEX_OK")