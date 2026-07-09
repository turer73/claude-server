"""Kod dosyalarını fonksiyon seviyesinde Qdrant'a indexle.

Her fonksiyon/metod'u şu formatta indeksler:
[Code] file.py:lineno def function_name
docstring (varsa)
ilk 20 satır kod

Skip: test dosyaları, __pycache__, venv
"""

import ast, json, os, sys, uuid, time
from pathlib import Path
from collections import Counter

import requests

QDRANT = "http://localhost:6333"
OLLAMA = "http://localhost:11434"
COLLECTION = "klipper-memory"
EMBED_MODEL = "bge-m3"

ROOTS = ["/opt/linux-ai-server/app", "/opt/linux-ai-server/automation", "/opt/linux-ai-server/scripts"]


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


def extract_functions_py(filepath):
    """Python dosyasındaki fonksiyon sınıfları döndürür."""
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        try:
            tree = ast.parse(f.read())
        except SyntaxError:
            return []
    rel = os.path.relpath(filepath, "/opt/linux-ai-server")
    chunks = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            lineno = node.lineno
            docstring = ast.get_docstring(node) or ""
            # İlk 15 satır kod
            with open(filepath, "r", encoding="utf-8", errors="replace") as f2:
                lines = f2.readlines()
            code_lines = lines[node.lineno - 1 : min(node.end_lineno or node.lineno + 20, node.lineno + 25)]
            code_text = "".join(code_lines[:20]).strip()
            text = f"[Code] {rel}:{lineno} def {name}\n"
            if docstring:
                text += f"{docstring}\n"
            text += f"```python\n{code_text}\n```"
            project = rel.split("/")[0]
            if project in ("app",):
                project = "linux-ai-server"
            meta = {
                "source": "code",
                "path": f"{rel}:{lineno}",
                "title": f"{name} — {rel}",
                "project": project,
                "text": text[:2000],
            }
            chunks.append(meta)
    return chunks


def extract_sections_sh(filepath):
    """Shell dosyasındaki fonksiyon benzeri blokları çıkar (comment başlıklı)."""
    rel = os.path.relpath(filepath, "/opt/linux-ai-server")
    chunks = []
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    content = "".join(lines)
    project = rel.split("/")[0]
    if project in ("automation",):
        project = "linux-ai-server"
    text = f"[Code] {rel}\n```bash\n{content[:1500]}\n```"
    chunks.append({
        "source": "code",
        "path": rel,
        "title": rel,
        "project": project,
        "text": text[:2000],
    })
    return chunks


# Zaten var olan code noktalarını kontrol et
print("Mevcut code noktalari kontrol ediliyor...")
try:
    r = requests.post(
        f"{QDRANT}/collections/{COLLECTION}/points/scroll",
        json={"limit": 100, "with_payload": ["source", "path"], "filter": {"must": [{"key": "source", "match": {"value": "code"}}]}},
        timeout=10,
    )
    existing_paths = set()
    for p in r.json().get("result", {}).get("points", []):
        pp = p["payload"].get("path", "")
        existing_paths.add(pp)
except Exception:
    existing_paths = set()

print(f"Zaten indexlenen kod noktalari: {len(existing_paths)}")

# Topla chunks
all_chunks = []
file_count = 0
func_count = 0
skip_count = 0

for root in ROOTS:
    for fpath in Path(root).rglob("*"):
        if not fpath.is_file():
            continue
        s = str(fpath)
        if "/venv/" in s or "/__pycache__/" in s or ".egg" in s:
            continue
        # Skip test dosyaları? Hayır — test fonksiyonları da önemli
        file_count += 1
        if fpath.suffix == ".py":
            funcs = extract_functions_py(str(fpath))
            for f in funcs:
                if f["path"] in existing_paths:
                    skip_count += 1
                    continue
                all_chunks.append(f)
                func_count += 1
        elif fpath.suffix == ".sh":
            shells = extract_sections_sh(str(fpath))
            for sh in shells:
                if sh["path"] in existing_paths:
                    skip_count += 1
                    continue
                all_chunks.append(sh)
                func_count += 1
        if file_count % 50 == 0:
            print(f"  Scanned: {file_count} files, {len(all_chunks)} new chunks")

print(f"\nScanned: {file_count} files")
print(f"New chunks: {len(all_chunks)} (skip: {skip_count})")

if not all_chunks:
    print("Yeni chunk yok, indexleme atlandi")
    sys.exit(0)


# Embed + upsert batch
t0 = time.time()
upserted = 0
batch = []
for i, ch in enumerate(all_chunks):
    text = ch["text"]
    vec = embed(text)
    if not vec:
        continue
    batch.append({
        "id": str(uuid.uuid4()),
        "vector": vec,
        "payload": {
            "source": "code",
            "path": ch["path"],
            "title": ch["title"],
            "project": ch["project"],
            "text": ch["text"],
        },
    })
    if len(batch) >= 100:
        requests.put(f"{QDRANT}/collections/{COLLECTION}/points?wait=true", json={"points": batch}, timeout=60)
        upserted += len(batch)
        batch = []
        print(f"  Upserted {upserted}/{len(all_chunks)}", flush=True)

if batch:
    requests.put(f"{QDRANT}/collections/{COLLECTION}/points?wait=true", json={"points": batch}, timeout=60)
    upserted += len(batch)

r = requests.get(f"{QDRANT}/collections/{COLLECTION}", timeout=10).json()
pc = r["result"]["points_count"]
print(f"\nUpserted: {upserted} points")
print(f"Toplam koleksiyon: {pc} points")
print(f"Sure: {time.time() - t0:.0f}s")
print("ALL_INDEX_OK")