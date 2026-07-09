"""Bug fix pattern'lerini Qdrant'a indexle: resolved discoveries + remediation_log.

Format:
[Fix] <title>
Project: <proj>
Type: <type>
Details: <details>

[Remediation] <alert_source> <mode>
Action: <action>
Success: <result>
"""

import json, os, sys, uuid, time, sqlite3

import requests

QDRANT = "http://localhost:6333"
OLLAMA = "http://localhost:11434"
COLLECTION = "klipper-memory"
EMBED_MODEL = "bge-m3"
MEMORY_DB = "/opt/linux-ai-server/data/claude_memory.db"
SERVER_DB = "/opt/linux-ai-server/data/server.db"


def embed(text):
    text = (text or "")[:8000]
    if not text.strip():
        return None
    try:
        r = requests.post(f"{OLLAMA}/api/embeddings", json={"model": EMBED_MODEL, "prompt": text}, timeout=120)
        return r.json().get("embedding")
    except Exception as e:
        print(f"  embed err: {e}", file=sys.stderr)
        return None


# Mevcut fix noktalarını kontrol et — dedup
r = requests.post(
    f"{QDRANT}/collections/{COLLECTION}/points/scroll",
    json={"limit": 1000, "with_payload": ["source", "path"], "filter": {"must": [{"key": "source", "match": {"value": "fix"}}]}},
    timeout=10,
)
existing = set()
for p in r.json().get("result", {}).get("points", []):
    pp = p["payload"].get("path", "")
    existing.add(pp)

print(f"Mevcut fix noktalari: {len(existing)}")

points = []

# 1) Resolved discoveries
try:
    con = sqlite3.connect(MEMORY_DB, timeout=10)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, project, type, title, details, status FROM discoveries WHERE status IN ('completed','resolved','fixed') ORDER BY id DESC LIMIT 500"
    ).fetchall()
    for r in rows:
        path = f"discovery:{r['id']}"
        if path in existing:
            continue
        text = f"[Fix] {r['title']}\nProject: {r['project'] or '?'}\nType: {r['type']}\nDetails: {r['details'] or ''}\nStatus: {r['status']}"
        points.append({
            "id": str(uuid.uuid4()),
            "payload": {
                "source": "fix",
                "path": path,
                "title": r["title"][:100],
                "project": r["project"] or "general",
                "text": text[:2000],
            },
        })
    con.close()
    print(f"Resolved discoveries: {len([p for p in points if 'discovery:' in p['payload']['path']])}")
except Exception as e:
    print(f"discovery sorgusu hata: {e}", file=sys.stderr)

# 2) Failed remediations (öğrenme fırsatı)
try:
    con = sqlite3.connect(SERVER_DB, timeout=10)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, alert_source, severity, mode, action, command, success, result FROM remediation_log WHERE success=0 ORDER BY id DESC LIMIT 300"
    ).fetchall()
    for r in rows:
        path = f"remediation:{r['id']}"
        if path in existing:
            continue
        text = f"[Failed Remediation] {r['alert_source']} ({r['mode']})\nAction: {r['action']}\nCommand: {r['command']}\nResult: {r['result'] or '?'}"
        points.append({
            "id": str(uuid.uuid4()),
            "vector": None,  # will be filled
            "payload": {
                "source": "fix",
                "path": path,
                "title": f"{r['alert_source']} — {r['action'][:50]}",
                "project": "linux-ai-server",
                "text": text[:2000],
            },
        })
    con.close()
    print(f"Failed remediations: {len([p for p in points if 'remediation:' in p['payload']['path']])}")
except Exception as e:
    print(f"remediation sorgusu hata: {e}", file=sys.stderr)

# 3) Successful remediations (çalışan pattern)
try:
    con = sqlite3.connect(SERVER_DB, timeout=10)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, alert_source, severity, mode, action, command, success, result FROM remediation_log WHERE success=1 ORDER BY RANDOM() LIMIT 200"
    ).fetchall()
    for r in rows:
        path = f"remediation:ok:{r['id']}"
        if path in existing:
            continue
        text = f"[Successful Remediation] {r['alert_source']} ({r['mode']})\nAction: {r['action']}\nCommand: {r['command']}\nSeverity: {r['severity']}"
        points.append({
            "id": str(uuid.uuid4()),
            "vector": None,
            "payload": {
                "source": "fix",
                "path": path,
                "title": f"{r['alert_source']} — {r['action'][:50]} (OK)",
                "project": "linux-ai-server",
                "text": text[:2000],
            },
        })
    con.close()
    print(f"Successful remediations: {len([p for p in points if 'remediation:ok:' in p['payload']['path']])}")
except Exception as e:
    print(f"remediation ok sorgusu hata: {e}", file=sys.stderr)

if not points:
    print("Yeni fix noktasi yok")
    sys.exit(0)

print(f"\nToplam yeni fix: {len(points)}")

# Embed + upsert
t0 = time.time()
upserted = 0
batch = []
for p in points:
    vec = embed(p["payload"]["text"])
    if not vec:
        continue
    p["vector"] = vec
    batch.append(p)
    if len(batch) >= 100:
        to_send = [{"id": b["id"], "vector": b["vector"], "payload": b["payload"]} for b in batch]
        requests.put(f"{QDRANT}/collections/{COLLECTION}/points?wait=true", json={"points": to_send}, timeout=60)
        upserted += len(batch)
        batch = []
        print(f"  Upserted {upserted}/{len(points)}", flush=True)

if batch:
    to_send = [{"id": b["id"], "vector": b["vector"], "payload": b["payload"]} for b in batch]
    requests.put(f"{QDRANT}/collections/{COLLECTION}/points?wait=true", json={"points": to_send}, timeout=60)
    upserted += len(batch)

r = requests.get(f"{QDRANT}/collections/{COLLECTION}", timeout=10).json()
pc = r["result"]["points_count"]
print(f"\nUpserted: {upserted} fix pattern")
print(f"Toplam koleksiyon: {pc} points")
print(f"Sure: {time.time() - t0:.0f}s")
print("ALL_INDEX_OK")