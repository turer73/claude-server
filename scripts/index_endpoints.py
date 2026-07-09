"""API endpoint'lerini Qdrant'a indexle.

Her endpoint'i şu formatta Qdrant'a kaydeder:
[Endpoint] METHOD /path
summary
description
parameters
requestBody (varsa)

Proje: linux-ai-server
Source: api-endpoint
"""

import json, sys, urllib.request, uuid, time, requests as reqlib

QDRANT = "http://localhost:6333"
OLLAMA = "http://localhost:11434"
COLLECTION = "klipper-memory"
EMBED_MODEL = "bge-m3"
OPENAPI = "http://localhost:8420/openapi.json"


def embed(text):
    text = (text or "")[:8000]
    if not text.strip():
        return None
    try:
        r = reqlib.post(f"{OLLAMA}/api/embeddings", json={"model": EMBED_MODEL, "prompt": text}, timeout=120)
        return r.json().get("embedding")
    except Exception as e:
        print(f"  embed err: {e}", file=sys.stderr)
        return None


# OpenAPI'yi çek
req = urllib.request.Request(OPENAPI)
resp = json.loads(urllib.request.urlopen(req).read())

paths = resp.get("paths", {})

points = []
total = 0
skipped = 0

for path, methods in paths.items():
    for method, spec in methods.items():
        method = method.upper()
        summary = spec.get("summary", "")
        description = spec.get("description", "")
        tags = spec.get("tags", [])
        parameters = spec.get("parameters", [])

        # Parametreleri özetle
        param_strs = []
        for p in parameters:
            name = p.get("name", "")
            reqd = "(gerekli)" if p.get("required") else "(opsiyonel)"
            desc = p.get("description", "")
            param_strs.append(f"  {name} {reqd}: {desc}")
        params_text = "\n".join(param_strs) if param_strs else "parametre yok"

        # Request body
        body_text = ""
        rb = spec.get("requestBody")
        if rb:
            content = rb.get("content", {})
            for ctype, cspec in content.items():
                schema = cspec.get("schema", {})
                ref = schema.get("$ref", "")
                body_text = f"requestBody: {ctype} | schema: {ref or 'inline'}"

        doc = f"[API] {method} {path}\n{summary}\n{description}\nTaglar: {', '.join(tags)}\nParametreler:\n{params_text}\n{body_text}".strip()

        # Zaten var mı kontrol et — aynı path+method'u bul
        search_body = {
            "limit": 1,
            "with_payload": False,
            "with_vector": False,
            "filter": {
                "must": [
                    {"key": "source", "match": {"value": "api-endpoint"}},
                    {"key": "path", "match": {"value": f"{method} {path}"}},
                ]
            },
        }
        existing = reqlib.post(
            f"{QDRANT}/collections/{COLLECTION}/points/scroll",
            json={"limit": 1, "with_payload": ["path"], "filter": {"must": [{"key": "source", "match": {"value": "api-endpoint"}}, {"key": "path", "match": {"value": f"{method} {path}"}}]}},
            timeout=10,
        ).json().get("result", {}).get("points", [])

        if existing:
            skipped += 1
            continue

        vec = embed(doc)
        if vec:
            points.append({
                "id": str(uuid.uuid4()),
                "vector": vec,
                "payload": {
                    "source": "api-endpoint",
                    "path": f"{method} {path}",
                    "method": method,
                    "title": summary or f"{method} {path}",
                    "project": "linux-ai-server",
                    "text": doc[:2000],
                },
            })
            total += 1
            if total % 50 == 0:
                print(f"  Hazir: {total}", flush=True)

print(f"\nHazir: {total} yeni endpoint | Skipped (zaten var): {skipped}")

if not points:
    print("Yeni endpoint yok, indexleme atlandi")
    sys.exit(0)

# Upsert batch
t0 = time.time()
for i in range(0, len(points), 100):
    batch = points[i : i + 100]
    reqlib.put(f"{QDRANT}/collections/{COLLECTION}/points?wait=true", json={"points": batch}, timeout=60)
    print(f"  Upserted {i+len(batch)}/{len(points)}", flush=True)

r = reqlib.get(f"{QDRANT}/collections/{COLLECTION}", timeout=10).json()
pc = r["result"]["points_count"]
print(f"\nToplam koleksiyon: {pc} points")
print(f"Sure: {time.time() - t0:.0f}s")
print("ALL_INDEX_OK")