#!/usr/bin/env python3
"""Bilge Arena RAG query test"""
import requests
import json

QDRANT = "http://localhost:6333"
OLLAMA = "http://localhost:11434"
COLLECTION = "bilge-arena"

def embed(text):
    r = requests.post(f"{OLLAMA}/api/embeddings", json={
        "model": "nomic-embed-text",
        "prompt": text
    }, timeout=30)
    return r.json()["embedding"]

def search(query, top_k=5):
    vec = embed(query)
    r = requests.post(f"{QDRANT}/collections/{COLLECTION}/points/search", json={
        "vector": vec,
        "limit": top_k,
        "with_payload": True
    })
    return r.json().get("result", [])

queries = [
    "Realtime hibrit nasil calisiyor",
    "PWA service worker manuel",
    "soru ureticisi AI Claude",
    "TDK Turkce uyum",
    "Codex review",
]

for q in queries:
    print(f"\n=== Sorgu: {q} ===")
    hits = search(q, top_k=3)
    for i, h in enumerate(hits, 1):
        p = h["payload"]
        score = h["score"]
        title = p.get("name") or p.get("title") or p.get("task") or p.get("text", "")[:80]
        print(f"  {i}. [{p['source']}] score={score:.3f}")
        print(f"     {title[:100]}")