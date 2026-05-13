#!/usr/bin/env python3
"""Generic project code indexer - parametrized"""
import os
import sys
import time
import uuid
import requests
import argparse
from pathlib import Path

QDRANT = "http://localhost:6333"
OLLAMA = "http://localhost:11434"
COLLECTION = "klipper-memory"
EMBED_MODEL = "bge-m3"

CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200
MAX_CHUNKS_PER_FILE = 8

SKIP_PATTERNS = [
    "node_modules/", ".next/", "dist/", ".turbo/", "coverage/",
    "/__tests__/", ".test.ts", ".test.tsx", ".spec.ts",
    "next-env.d.ts", ".d.ts", ".config.ts", ".config.mjs", ".config.js",
    "pnpm-lock.yaml", "package-lock.json", "playwright-report/",
    "test-results/", ".vercel/", "build/",
]

def should_skip(path):
    return any(p in path for p in SKIP_PATTERNS)

def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    if len(text) <= size:
        return [text]
    chunks = []
    pos = 0
    while pos < len(text):
        chunks.append(text[pos:pos + size])
        if len(chunks) >= MAX_CHUNKS_PER_FILE:
            break
        pos += size - overlap
    return chunks

def embed(text):
    try:
        r = requests.post(f"{OLLAMA}/api/embeddings",
                          json={"model": EMBED_MODEL, "prompt": text[:8000]},
                          timeout=60)
        return r.json().get("embedding")
    except Exception as e:
        print(f"  embed err: {e}", file=sys.stderr)
        return None

def collect_files(repo):
    files = []
    exts = {".ts", ".tsx", ".sql", ".md", ".js", ".jsx", ".py", ".astro"}
    for root, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in {"node_modules", ".next", "dist", ".git", "coverage", ".turbo", ".vercel", "playwright-report", "test-results", "build"}]
        for name in names:
            path = os.path.join(root, name)
            if should_skip(path):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in exts:
                rel = os.path.relpath(path, repo)
                files.append((path, rel, ext))
    return files

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="Repo path")
    ap.add_argument("--project", required=True, help="Project name (for payload)")
    ap.add_argument("--delete-existing", action="store_true", help="Delete existing code points for this project first")
    args = ap.parse_args()
    
    if args.delete_existing:
        # Delete only code/sql/doc source for this project
        print(f"Deleting existing entries for project={args.project}...")
        del_body = {
            "filter": {
                "must": [
                    {"key": "project", "match": {"value": args.project}},
                    {"key": "source", "match": {"any": ["code", "sql", "doc"]}}
                ]
            }
        }
        r = requests.post(f"{QDRANT}/collections/{COLLECTION}/points/delete?wait=true", json=del_body, timeout=60)
        print(f"  Delete: {r.status_code}")
    
    files = collect_files(args.repo)
    print(f"\n=== {args.project} indexer ===")
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
        except Exception:
            continue
        if not content.strip():
            continue
        
        if ext in (".ts", ".tsx", ".js", ".jsx"):
            stype = "code"
        elif ext == ".sql":
            stype = "sql"
        elif ext == ".md":
            stype = "doc"
        elif ext == ".py":
            stype = "code"
        else:
            stype = "code"
        
        chunks = chunk_text(content)
        
        for i, chunk in enumerate(chunks):
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
                        "project": args.project,
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
            print(f"  ... {files_processed}/{len(files)}, {chunks_total} chunks, {elapsed:.0f}s", flush=True)
    
    print(f"\nDone: {files_processed} files -> {chunks_total} chunks ({time.time()-t_start:.0f}s)")
    
    print("Uploading...")
    for i in range(0, len(points), 100):
        r = requests.put(f"{QDRANT}/collections/{COLLECTION}/points?wait=true",
                        json={"points": points[i:i+100]}, timeout=60)
        if i % 500 == 0:
            print(f"  {i}+ done", flush=True)
    
    info = requests.get(f"{QDRANT}/collections/{COLLECTION}").json()["result"]
    print(f"\nCollection total: {info['points_count']} points")
    print(f"Total time: {time.time()-t_start:.0f}s")
    print("INDEX_OK")

if __name__ == "__main__":
    main()