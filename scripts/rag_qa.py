import time

import requests


def embed(t):
    return requests.post("http://localhost:11434/api/embeddings", json={"model": "bge-m3", "prompt": t}, timeout=60).json()["embedding"]


def search(q, k=4):
    v = embed(q)
    return (
        requests.post("http://localhost:6333/collections/bilge-arena/points/search", json={"vector": v, "limit": k, "with_payload": True})
        .json()
        .get("result", [])
    )


def generate(prompt):
    r = requests.post(
        "http://localhost:11434/api/generate",
        json={"model": "qwen2.5:7b", "prompt": prompt, "stream": False, "options": {"temperature": 0.2, "num_predict": 300}},
        timeout=180,
    )
    return r.json()


def rag_qa(query):
    print(f"\n{'=' * 72}")
    print(f"SORU: {query}")
    print("=" * 72)

    t0 = time.time()
    hits = search(query, k=5)
    t_search = time.time() - t0

    context = "\n\n".join(
        [
            f"--- Kaynak {i + 1} ({h['payload']['source']}, skor {h['score']:.2f}) ---\n{h['payload'].get('text', '')[:1200]}"
            for i, h in enumerate(hits)
        ]
    )

    prompt = f"""Sen Bilge Arena projesi uzmanisin. Sadece asagidaki KAYNAKLAR'a dayanarak SORU'yu Turkce cevapla. Kaynaklarda yoksa "Hafizamda yetersiz bilgi" de. Madde madde cevap ver.

KAYNAKLAR:
{context}

SORU: {query}

CEVAP (Turkce, kaynaklara dayali, ozet):"""

    t0 = time.time()
    res = generate(prompt)
    t_gen = time.time() - t0

    print(f"\nRAG sure: search={t_search * 1000:.0f}ms, gen={t_gen:.1f}s, tokens={res.get('eval_count')}")
    print(f"\n{res.get('response', '').strip()}")


queries = [
    "Bilge Arena VPS Realtime mimarisi nasil? Hangi tenant?",
    "TDK Turkce uyum hangi PR'lar ile yapildi, ne sirayla?",
    "PWA Next 16 ile sw.js sorunu kac denemeden sonra cozuldu, nasil?",
    "AI soru ureticisi 3-tier pipeline nasil calisir?",
]

for q in queries:
    rag_qa(q)
