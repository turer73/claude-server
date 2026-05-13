#!/usr/bin/env python3
"""Bilge Arena re-index with bge-m3 multilingual + full content"""
import sqlite3, requests, json, uuid, time

DB = "/opt/linux-ai-server/data/claude_memory.db"
QDRANT = "http://localhost:6333"
OLLAMA = "http://localhost:11434"
COLLECTION = "bilge-arena"
EMBED_MODEL = "bge-m3"
VECTOR_SIZE = 1024
FILTER = "bilge"

def embed(text):
    text = (text or "")[:8000]
    if not text.strip():
        return None
    r = requests.post(f"{OLLAMA}/api/embeddings", json={
        "model": EMBED_MODEL, "prompt": text
    }, timeout=60)
    return r.json().get("embedding")

# Recreate collection
requests.delete(f"{QDRANT}/collections/{COLLECTION}")
requests.put(f"{QDRANT}/collections/{COLLECTION}", json={
    "vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}
})

conn = sqlite3.connect(DB)
cur = conn.cursor()
points = []

# Memories - full content
cur.execute("SELECT id, type, name, description, content FROM memories WHERE name LIKE ? OR description LIKE ? OR content LIKE ?", (f"%{FILTER}%",)*3)
for mid, typ, name, desc, content in cur.fetchall():
    text = f"[{typ}] {name}\nAciklama: {desc or ''}\nIcerik: {content or ''}"
    vec = embed(text)
    if vec:
        points.append({"id": str(uuid.uuid4()), "vector": vec, "payload": {
            "source": "memory", "type": typ, "name": name,
            "memory_id": mid, "text": text[:2000]
        }})

print(f"Memories: {len(points)}")

# Discoveries
cur.execute("SELECT id, project, type, title, details, status FROM discoveries WHERE project LIKE ?", (f"%{FILTER}%",))
disc_start = len(points)
for did, proj, typ, title, details, status in cur.fetchall():
    text = f"[Discovery {typ} ({status})] {title}\nDetaylar: {details or ''}"
    vec = embed(text)
    if vec:
        points.append({"id": str(uuid.uuid4()), "vector": vec, "payload": {
            "source": "discovery", "type": typ, "title": title, "status": status,
            "discovery_id": did, "text": text[:2000]
        }})
print(f"Discoveries: {len(points)-disc_start}")

# Sessions
cur.execute("SELECT id, date, device_name, summary FROM sessions WHERE summary LIKE ?", (f"%{FILTER}%",))
sess_start = len(points)
for sid, date, dev, summary in cur.fetchall():
    text = f"[Session {date} {dev}]\n{summary or ''}"
    vec = embed(text)
    if vec:
        points.append({"id": str(uuid.uuid4()), "vector": vec, "payload": {
            "source": "session", "date": date, "device": dev,
            "session_id": sid, "text": text[:2000]
        }})
print(f"Sessions: {len(points)-sess_start}")

# Tasks
cur.execute("SELECT id, project, task, details, status FROM tasks_log WHERE project LIKE ? OR task LIKE ?", (f"%{FILTER}%", f"%{FILTER}%"))
task_start = len(points)
for tid, proj, task, details, status in cur.fetchall():
    text = f"[Task ({status})] {task}\nDetaylar: {details or ''}"
    vec = embed(text)
    if vec:
        points.append({"id": str(uuid.uuid4()), "vector": vec, "payload": {
            "source": "task", "project": proj, "task": task, "status": status,
            "task_id": tid, "text": text[:2000]
        }})
print(f"Tasks: {len(points)-task_start}")

conn.close()

# Upsert
for i in range(0, len(points), 100):
    requests.put(f"{QDRANT}/collections/{COLLECTION}/points?wait=true",
                 json={"points": points[i:i+100]}, timeout=60)

info = requests.get(f"{QDRANT}/collections/{COLLECTION}").json()["result"]
print(f"\nFinal: {info['points_count']} points, vec_size={info['config']['params']['vectors']['size']}")
print("REINDEX_OK")