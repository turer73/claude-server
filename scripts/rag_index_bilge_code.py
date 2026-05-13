#!/usr/bin/env python3
"""Bilge Arena kod indexer - .ts/.tsx/.sql/.md -> Qdrant (chunk-based)"""
import os
import sys
import time
import uuid
import requests
from pathlib import Path

REPO = "/opt/code-cache/bilge-arena"
QDRANT = "http://localhost:6333"
OLLAMA = "http://localhost:11434"
COLLECTION = "klipper-memory"
EMBED_MODEL = "bge-m3"
PROJECT = "bilge-arena"

# Chunking parametreleri
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200
MAX_CHUNKS_PER_FILE = 8  # buyuk dosyalari kapla

# Skip patterns
SKIP_PATTERNS = [
    "node_modules/", ".next/", "dist/", ".turbo/", "coverage/",
    "/__tests__/", ".test.ts", ".test.tsx", ".spec.ts",
    "next-env.d.ts", ".d.ts", ".config.ts", ".config.mjs",
    "pnpm-lock.yaml", "package-lock.json",
]

def should_skip(path: str) -> bool:
    return any(p in path for p in SKIP_PATTERNS)

def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP):
    """Sliding window chunks"""
    if len(text) <= size:
        return [text]
    chunks = []
    pos = 0
    while pos < len(text):
        chunk = text[pos:pos + size]
        chunks.append(chunk)
        if len(chunks) >= MAX_CHUNKS_PER_FILE:
            break
        pos += size - overlap
    return chunks

def embed(text: str):
    try:
        r = requests.post(f"{OLLAMA}/api/embeddings",
                          json={"model": EMBED_MODEL, "prompt": text[:8000]},
                          timeout=60)
        return r.json().get("embedding")
    except Exception as e:
        print(f"  embed err: {e}", file=sys.stderr)
        return None

def collect_files():
    """Index edilecek dosyalari topla"""
    files = []
    extensions = {".ts", ".tsx", ".sql", ".md"}
    for root, dirs, names in os.walk(REPO):
        # Prune dirs
        dirs[:] = [d for d in dirs if d not in {"node_modules", ".next", "dist", ".git", "coverage", ".turbo"}]
        for name in names:
            path = os.path.join(root, name)
            if should_skip(path):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in extensions:
                rel = os.path.relpath(path, REPO)
                files.append((path, rel, ext))
    return files

def main():
    print("=== Bilge Arena code indexer ===")
    
    files = collect_files()
    print(f"Total files: {len(files)}")
    by_ext = {}
    for _, _, ext in files:
        by_ext[ext] = by_ext.get(ext, 0) + 1
    for ext, cnt in sorted(by_ext.items()):
        print(f"  {ext}: {cnt}")
    
    t_start = time.time()
    points = []
    files_processed = 0
    chunks_total = 0
    
    for path, rel, ext in files:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            continue
        
        if not content.strip():
            continue
        
        # Source type
        if ext in (".ts", ".tsx"):
            stype = "code"
        elif ext == ".sql":
            stype = "sql"
        elif ext == ".md":
            stype = "doc"
        else:
            stype = "code"
        
        chunks = chunk_text(content)
        
        for i, chunk in enumerate(chunks):
            # Prepend context header
            header = f"// File: {rel}"
            if len(chunks) > 1:
                header += f" (chunk {i+1}/{len(chunks)})"
            text = f"{header}\n\n{chunk}"
            
            vec = embed(text)
            if vec:
                points.append({
                    "id": str(uuid.uuid4()),
                    "vector": vec,
                    "payload": {
                        "source": stype,
                        "project": PROJECT,
                        "file_path": rel,
                        "chunk_index": i,
                        "chunk_total": len(chunks),
                        "ext": ext,
                        "text": text[:2000],
                    }
                })
                chunks_total += 1
        
        files_processed += 1
        if files_processed % 50 == 0:
            elapsed = time.time() - t_start
            print(f"  ... {files_processed}/{len(files)} files, {chunks_total} chunks, {elapsed:.0f}s", flush=True)
    
    print(f"\nIndex complete: {files_processed} files -> {chunks_total} chunks ({time.time()-t_start:.0f}s)")
    
    # Upsert in batches
    print("Uploading to Qdrant...")
    for i in range(0, len(points), 100):
        batch = points[i:i+100]
        r = requests.put(
            f"{QDRANT}/collections/{COLLECTION}/points?wait=true",
            json={"points": batch}, timeout=60
        )
        if not r.ok:
            print(f"  upsert fail at {i}: {r.status_code}", file=sys.stderr)
        if i % 500 == 0:
            print(f"  {i}+ done", flush=True)
    
    # Stats
    info = requests.get(f"{QDRANT}/collections/{COLLECTION}").json()["result"]
    print(f"\nCollection total: {info['points_count']} points")
    
    # Per project after
    r = requests.post(f"{QDRANT}/collections/{COLLECTION}/points/scroll",
                     json={"limit": 10000,
                           "filter": {"must": [{"key": "project", "match": {"value": PROJECT}}]},
                           "with_payload": ["source"], "with_vector": False},
                     timeout=30)
    pts = r.json()["result"]["points"]
    from collections import Counter
    bsources = Counter(p["payload"]["source"] for p in pts)
    print(f"\nBilge Arena dagilim:")
    for src, cnt in bsources.most_common():
        print(f"  {src}: {cnt}")
    
    print(f"\nToplam sure: {time.time()-t_start:.0f}s")
    print("CODE_INDEX_OK")

if __name__ == "__main__":
    main()