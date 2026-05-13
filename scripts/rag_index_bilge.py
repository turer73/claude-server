#!/usr/bin/env python3
"""Bilge Arena RAG indexer - hafiza DB -> Qdrant"""
import sqlite3
import requests
import json
import uuid
import sys
import time

DB = "/opt/linux-ai-server/data/claude_memory.db"
QDRANT = "http://localhost:6333"
OLLAMA = "http://localhost:11434"
COLLECTION = "bilge-arena"
PROJECT_FILTER = "bilge"

def embed(text):
    """Ollama nomic-embed-text ile embedding (768d)"""
    text = text[:4096] if text else ""
    if not text.strip():
        return None
    r = requests.post(f"{OLLAMA}/api/embeddings", json={
        "model": "nomic-embed-text",
        "prompt": text
    }, timeout=30)
    return r.json().get("embedding")

def create_collection():
    """Collection olustur (recreate)"""
    requests.delete(f"{QDRANT}/collections/{COLLECTION}")
    r = requests.put(f"{QDRANT}/collections/{COLLECTION}", json={
        "vectors": {"size": 768, "distance": "Cosine"}
    })
    print(f"Collection created: {r.status_code}")

def upsert_batch(points):
    """Qdrant'a toplu yukle"""
    if not points:
        return
    r = requests.put(
        f"{QDRANT}/collections/{COLLECTION}/points?wait=true",
        json={"points": points},
        timeout=60
    )
    print(f"Upserted {len(points)} points: {r.status_code}")

def main():
    create_collection()
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    
    all_points = []
    counts = {"memory": 0, "discovery": 0, "session": 0, "task": 0}
    
    # 1) Memories
    print("== Memories ==")
    cur.execute("""
        SELECT id, type, name, description, content
        FROM memories
        WHERE name LIKE ? OR description LIKE ? OR content LIKE ?
    """, (f"%{PROJECT_FILTER}%",) * 3)
    for row in cur.fetchall():
        mid, typ, name, desc, content = row
        text = f"[Memory: {typ}] {name}\n{desc or ''}\n{content or ''}"
        vec = embed(text)
        if vec:
            all_points.append({
                "id": str(uuid.uuid4()),
                "vector": vec,
                "payload": {
                    "source": "memory",
                    "type": typ,
                    "name": name,
                    "description": desc or "",
                    "memory_id": mid,
                    "text": text[:500]
                }
            })
            counts["memory"] += 1
            if counts["memory"] % 50 == 0:
                print(f"  ... {counts['memory']}")
    
    # 2) Discoveries
    print("== Discoveries ==")
    cur.execute("""
        SELECT id, project, type, title, details, status
        FROM discoveries
        WHERE project LIKE ?
    """, (f"%{PROJECT_FILTER}%",))
    for row in cur.fetchall():
        did, proj, typ, title, details, status = row
        text = f"[Discovery {typ} ({status})] {title}\n{details or ''}"
        vec = embed(text)
        if vec:
            all_points.append({
                "id": str(uuid.uuid4()),
                "vector": vec,
                "payload": {
                    "source": "discovery",
                    "type": typ,
                    "project": proj,
                    "title": title,
                    "status": status,
                    "discovery_id": did,
                    "text": text[:500]
                }
            })
            counts["discovery"] += 1
    
    # 3) Sessions
    print("== Sessions ==")
    cur.execute("""
        SELECT id, date, device_name, summary
        FROM sessions
        WHERE summary LIKE ?
    """, (f"%{PROJECT_FILTER}%",))
    for row in cur.fetchall():
        sid, date, device, summary = row
        text = f"[Session {date} {device}] {summary or ''}"
        vec = embed(text)
        if vec:
            all_points.append({
                "id": str(uuid.uuid4()),
                "vector": vec,
                "payload": {
                    "source": "session",
                    "date": date,
                    "device": device,
                    "session_id": sid,
                    "text": text[:500]
                }
            })
            counts["session"] += 1
            if counts["session"] % 50 == 0:
                print(f"  ... {counts['session']}")
    
    # 4) Tasks
    print("== Tasks ==")
    cur.execute("""
        SELECT id, project, task, details, status
        FROM tasks_log
        WHERE project LIKE ? OR task LIKE ?
    """, (f"%{PROJECT_FILTER}%", f"%{PROJECT_FILTER}%"))
    for row in cur.fetchall():
        tid, proj, task, details, status = row
        text = f"[Task ({status})] {task}\n{details or ''}"
        vec = embed(text)
        if vec:
            all_points.append({
                "id": str(uuid.uuid4()),
                "vector": vec,
                "payload": {
                    "source": "task",
                    "project": proj,
                    "task": task,
                    "status": status,
                    "task_id": tid,
                    "text": text[:500]
                }
            })
            counts["task"] += 1
            if counts["task"] % 100 == 0:
                print(f"  ... {counts['task']}")
    
    conn.close()
    
    # Upsert in batches of 100
    print(f"\nTotal points: {len(all_points)}")
    print(f"Counts: {counts}")
    for i in range(0, len(all_points), 100):
        batch = all_points[i:i+100]
        upsert_batch(batch)
    
    # Stats
    r = requests.get(f"{QDRANT}/collections/{COLLECTION}")
    if r.ok:
        info = r.json().get("result", {})
        print(f"\nCollection final: {info.get('points_count')} points, vector_size={info.get('config', {}).get('params', {}).get('vectors', {}).get('size')}")
    
    print(f"\nRAG_INDEX_OK")

if __name__ == "__main__":
    start = time.time()
    main()
    print(f"Sure: {time.time()-start:.1f}s")