import requests

QDRANT = "http://localhost:6333"
OLLAMA = "http://localhost:11434"


def embed(text):
    return requests.post(f"{OLLAMA}/api/embeddings", json={"model": "bge-m3", "prompt": text}, timeout=60).json()["embedding"]


def search(query, top_k=4):
    vec = embed(query)
    r = requests.post(f"{QDRANT}/collections/bilge-arena/points/search", json={"vector": vec, "limit": top_k, "with_payload": True})
    return r.json().get("result", [])


queries = [
    "Bilge Arena VPS Realtime nasil calisiyor",
    "TDK Turkce uyum PR sureci",
    "AI soru ureticisi hangi modelleri kullaniyor",
    "PWA Next 16 sw.js manuel kalici",
    "Sprint 3 oda sistemi mimari",
]

for q in queries:
    print(f"\n>>> {q}")
    hits = search(q, top_k=3)
    for h in hits:
        p = h["payload"]
        title = p.get("name") or p.get("title") or p.get("task") or p.get("text", "")[:80]
        print(f"  [{p['source']}] score={h['score']:.3f}  {title[:80]}")
