#!/usr/bin/env python3
"""RAG + LLM end-to-end: query -> Qdrant search -> Qwen context-aware response"""

import time

import requests

QDRANT = "http://localhost:6333"
OLLAMA = "http://localhost:11434"
COLLECTION = "bilge-arena"


def embed(text):
    return requests.post(f"{OLLAMA}/api/embeddings", json={"model": "nomic-embed-text", "prompt": text}, timeout=30).json()["embedding"]


def retrieve(query, top_k=5):
    vec = embed(query)
    r = requests.post(f"{QDRANT}/collections/{COLLECTION}/points/search", json={"vector": vec, "limit": top_k, "with_payload": True})
    return r.json().get("result", [])


def generate(query, context):
    """Qwen 2.5 7B ile context-aware yanit"""
    prompt = f"""Sen Bilge Arena projesi uzmanisin. Asagidaki BAGLAM bilgilerini kullanarak SORU'yu Turkce, kisa ve dogru yanitla. Eger bilgi yoksa "Hafizamda yok" de.

BAGLAM:
{context}

SORU: {query}

YANIT (Turkce, 2-3 cumle):"""

    r = requests.post(
        f"{OLLAMA}/api/generate",
        json={"model": "qwen2.5:7b", "prompt": prompt, "stream": False, "options": {"temperature": 0.3, "num_predict": 250}},
        timeout=120,
    )
    return r.json()


def rag_answer(query):
    print(f"\n{'=' * 70}")
    print(f"SORU: {query}")
    print("=" * 70)

    t0 = time.time()
    hits = retrieve(query, top_k=4)
    t_search = time.time() - t0

    context_parts = []
    for h in hits[:4]:
        p = h["payload"]
        context_parts.append(f"- {p.get('text', '')[:400]}")
    context = "\n".join(context_parts)

    print(f"\nRetrieved {len(hits)} chunks ({t_search:.2f}s):")
    for i, h in enumerate(hits[:3], 1):
        p = h["payload"]
        print(f"  {i}. [{p['source']}] {(p.get('name') or p.get('title') or p.get('task') or '')[:80]}")

    t0 = time.time()
    result = generate(query, context)
    t_gen = time.time() - t0

    response = result.get("response", "")
    tokens = result.get("eval_count", 0)
    eval_dur = result.get("eval_duration", 1) / 1e9

    print(f"\nYANIT ({t_gen:.1f}s, {tokens} token, {tokens / max(eval_dur, 0.001):.1f} tok/s):")
    print(response.strip())
    print()


# Test sorulari
queries = [
    "Bilge Arena VPS Realtime mimarisi nasil?",
    "TDK Turkce uyum sureci hangi PR'lar ile yapildi?",
    "AI soru ureticisi hangi modeller kullaniyor?",
    "PWA Next 16 ile sw.js sorunu nasil cozuldu?",
]

for q in queries:
    rag_answer(q)
