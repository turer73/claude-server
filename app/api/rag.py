"""
RAG API + metric logging (Qdrant + Ollama bge-m3 + qwen2.5)
"""

import os
import re
import sqlite3
import time

import requests
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from app.api.memory import verify_key

QDRANT_URL = "http://localhost:6333"
OLLAMA_URL = "http://localhost:11434"
COLLECTION = "klipper-memory"
EMBED_MODEL = "bge-m3"
LLM_MODEL = "qwen2.5:3b"
ALLOWED_LLM_MODELS = {"qwen2.5:3b", "qwen2.5:7b", "qwen2.5-coder:7b", "aya:8b"}
METRICS_DB = os.environ.get("RAG_METRICS_DB", "/opt/linux-ai-server/data/rag_metrics.db")

router = APIRouter(prefix="/api/v1/rag", tags=["rag"], dependencies=[Depends(verify_key)])


def _init_metrics_db():
    conn = sqlite3.connect(METRICS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rag_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            endpoint TEXT NOT NULL,
            query TEXT NOT NULL,
            project TEXT,
            source TEXT,
            top_k INTEGER,
            hit_count INTEGER,
            top_score REAL,
            duration_ms INTEGER,
            tokens INTEGER,
            tokens_per_sec REAL,
            client_ip TEXT,
            user_agent TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON rag_queries(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_project ON rag_queries(project)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_endpoint ON rag_queries(endpoint)")
    conn.commit()
    conn.close()


# Modul-load zamani init: CI veya path-eksik ortamda fail edebilir, sessiz gec.
# _log_query ve /metrics ilk cagrildiginda dogal olarak CREATE TABLE IF NOT EXISTS
# tetiklenir (sqlite3.connect dosyayi olusturur).
try:
    _init_metrics_db()
except sqlite3.OperationalError:
    pass


def _log_query(endpoint, query, project, source, top_k, hits, duration_ms, tokens=None, tps=None, request=None):
    try:
        conn = sqlite3.connect(METRICS_DB, timeout=2)
        ip = ua = None
        if request:
            ip = request.client.host if request.client else None
            ua = request.headers.get("user-agent", "")[:200]
        top_score = float(hits[0]["score"]) if hits else None
        conn.execute(
            """
            INSERT INTO rag_queries (ts, endpoint, query, project, source, top_k, hit_count, top_score, duration_ms, tokens, tokens_per_sec, client_ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (int(time.time()), endpoint, query[:500], project, source, top_k, len(hits), top_score, duration_ms, tokens, tps, ip, ua),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _embed(text):
    text = (text or "")[:8000]
    if not text.strip():
        raise HTTPException(400, "empty text")
    r = requests.post(f"{OLLAMA_URL}/api/embeddings", json={"model": EMBED_MODEL, "prompt": text}, timeout=60)
    if not r.ok:
        raise HTTPException(503, f"embed fail: {r.status_code}")
    return r.json().get("embedding", [])


def _search(vec, top_k=5, project=None, source=None):
    filt = {"must": []}
    if project:
        filt["must"].append({"key": "project", "match": {"value": project}})
    if source:
        filt["must"].append({"key": "source", "match": {"value": source}})
    body = {"vector": vec, "limit": top_k, "with_payload": True}
    if filt["must"]:
        body["filter"] = filt
    r = requests.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/search", json=body, timeout=30)
    if not r.ok:
        raise HTTPException(503, f"qdrant fail: {r.status_code}")
    return r.json().get("result", [])


# ── Hybrid retrieval (dense + keyword → Reciprocal Rank Fusion) ──────────────
# Clean-room: standart RRF deseni (Odysseus'tan FIKIR alindi, KOD degil — repo
# tutarsiz-lisansli dev=MIT/main=AGPL). Dense (bge-m3 cosine) anlamsal yakaliyor;
# keyword (Qdrant full-text) tam-terim/nadir-token (hata-kodu, fonksiyon-adi, ID)
# yakaliyor — embedding'in kacirdigi recall'i kapatir.
RRF_K = 60
KW_PAGE = 128  # keyword scroll sayfa-boyu
KW_MAX_PAGES = 8  # en cok 8 sayfa (1024 aday) -> common-token'da bile genis havuz
_STOP = {"ve", "ile", "bir", "bu", "icin", "the", "and", "for", "with", "that", "this", "var", "yok", "ama", "veya"}


def _ensure_text_index():
    """Qdrant 'text' payload alanina full-text index (keyword-match icin). Idempotent —
    zaten varsa Qdrant no-op doner; hata/erisimsizlik sessizce gecilir (dense calismaya devam)."""
    try:
        requests.put(
            f"{QDRANT_URL}/collections/{COLLECTION}/index",
            json={
                "field_name": "text",
                "field_schema": {"type": "text", "tokenizer": "word", "lowercase": True, "min_token_len": 2},
            },
            timeout=10,
        )
    except Exception:
        pass


def _tokenize(q):
    toks = re.findall(r"\w+", (q or "").lower())
    return [t for t in toks if len(t) >= 3 and t not in _STOP][:12]


def _keyword_search(query, top_k=5, project=None, source=None):
    """Qdrant full-text 'should' match (>=1 token) → token-kapsama skoruyla siralanir.
    Index yoksa / Qdrant hata → bos liste (hybrid graceful dense-only'e duser)."""
    toks = _tokenize(query)
    if not toks:
        return []
    flt = {"should": [{"key": "text", "match": {"text": t}} for t in toks]}
    must = []
    if project:
        must.append({"key": "project", "match": {"value": project}})
    if source:
        must.append({"key": "source", "match": {"value": source}})
    if must:
        flt["must"] = must
    # Codex P2: scroll'u SAYFALA — tek 'limit' common-token'da en iyi token-kapsama
    # eslesmelerini kesebilir (scroll ID-sirasi, relevance degil). Genis aday-havuzu
    # topla (≤KW_MAX_PAGES×KW_PAGE), SONRA kapsama'ya gore sirala.
    pts = []
    offset = None
    for _ in range(KW_MAX_PAGES):
        body = {"filter": flt, "limit": KW_PAGE, "with_payload": True}
        if offset is not None:
            body["offset"] = offset
        try:
            r = requests.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll", json=body, timeout=20)
            if not r.ok:
                break
            res = r.json().get("result", {})
        except Exception:
            break
        page = res.get("points", [])
        pts.extend(page)
        offset = res.get("next_page_offset")
        if not offset or not page:
            break
    scored = []
    for p in pts:
        text = (p.get("payload", {}).get("text", "") or "").lower()
        cover = sum(1 for t in toks if t in text)
        if cover:
            scored.append((cover, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"id": p["id"], "score": float(c), "payload": p.get("payload", {})} for c, p in scored[:top_k]]


def _hybrid_search(query, vec, top_k=5, project=None, source=None):
    """Dense + keyword → Reciprocal Rank Fusion (k=RRF_K). Keyword-leg bos/fail → dense-only."""
    pool = max(top_k * 4, 20)
    dense = _search(vec, top_k=pool, project=project, source=source)
    kw = _keyword_search(query, top_k=pool, project=project, source=source)
    fused: dict = {}
    keep: dict = {}
    for rank, h in enumerate(dense):
        pid = h["id"]
        fused[pid] = fused.get(pid, 0.0) + 1.0 / (RRF_K + rank + 1)
        keep[pid] = h
    for rank, h in enumerate(kw):
        pid = h["id"]
        fused[pid] = fused.get(pid, 0.0) + 1.0 / (RRF_K + rank + 1)
        keep.setdefault(pid, h)
    ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:top_k]
    out = []
    for pid, rrf in ranked:
        h = dict(keep[pid])
        h["score"] = rrf
        out.append(h)
    return out


# Modul-load: text-index garanti (idempotent, erisimsizlikte sessiz).
try:
    _ensure_text_index()
except Exception:
    pass


@router.get("/health")
def health():
    try:
        q = requests.get(f"{QDRANT_URL}/collections/{COLLECTION}", timeout=5).json()
        o = requests.get(f"{OLLAMA_URL}/api/version", timeout=5).json()
        _init_metrics_db()  # Idempotent — CI/fresh-install path icin garanti
        conn = sqlite3.connect(METRICS_DB, timeout=2)
        total_queries = conn.execute("SELECT COUNT(*) FROM rag_queries").fetchone()[0]
        conn.close()
        return {
            "qdrant": {"ok": True, "points": q.get("result", {}).get("points_count")},
            "ollama": {"ok": True, "version": o.get("version")},
            "embed_model": EMBED_MODEL,
            "llm_model": LLM_MODEL,
            "allowed_llm_models": sorted(ALLOWED_LLM_MODELS),
            "metrics_db_total_queries": total_queries,
        }
    except Exception as e:
        raise HTTPException(503, str(e))


@router.post("/search")
def search(
    request: Request,
    q: str = Body(..., embed=True),
    top_k: int = Body(5, embed=True),
    project: str | None = Body(None, embed=True),
    source: str | None = Body(None, embed=True),
    mode: str = Body("hybrid", embed=True),
):
    t0 = time.time()
    # mode: hybrid (varsayilan, dense+keyword RRF) | vector (eski cosine) | keyword
    if mode == "keyword":
        hits = _keyword_search(q, top_k=top_k, project=project, source=source)
    else:
        vec = _embed(q)
        hits = (
            _hybrid_search(q, vec, top_k=top_k, project=project, source=source)
            if mode != "vector"
            else _search(vec, top_k=top_k, project=project, source=source)
        )
    duration_ms = int((time.time() - t0) * 1000)
    _log_query("search", q, project, source, top_k, hits, duration_ms, request=request)
    return {
        "query": q,
        "mode": mode,
        "count": len(hits),
        "duration_ms": duration_ms,
        "results": [
            {
                "score": float(h["score"]),
                "source": h["payload"].get("source"),
                "project": h["payload"].get("project"),
                "title": h["payload"].get("name") or h["payload"].get("title") or h["payload"].get("task") or h["payload"].get("file_path"),
                "text": h["payload"].get("text", "")[:1500],
                "memory_id": h["payload"].get("memory_id"),
                "discovery_id": h["payload"].get("discovery_id"),
                "session_id": h["payload"].get("session_id"),
                "task_id": h["payload"].get("task_id"),
                "file_path": h["payload"].get("file_path"),
            }
            for h in hits
        ],
    }


@router.post("/ask")
def ask(
    request: Request,
    q: str = Body(..., embed=True),
    project: str | None = Body(None, embed=True),
    top_k: int = Body(5, embed=True),
    temperature: float = Body(0.2, embed=True),
    max_tokens: int = Body(400, embed=True),
    model: str = Body(LLM_MODEL, embed=True),
):
    if model not in ALLOWED_LLM_MODELS:
        raise HTTPException(422, f"model '{model}' not allowed; use one of: {sorted(ALLOWED_LLM_MODELS)}")
    t0 = time.time()
    vec = _embed(q)
    hits = _hybrid_search(q, vec, top_k=top_k, project=project)
    context = "\n\n".join(
        f"--- Kaynak {i + 1} ({h['payload']['source']}, skor {h['score']:.2f}) ---\n{h['payload'].get('text', '')[:1500]}"
        for i, h in enumerate(hits)
    )
    prompt = (
        f"Sen Klipper merkezi hafiza uzmanisin. Sadece asagidaki KAYNAKLAR'a dayanarak SORU'yu Turkce cevapla. "
        f"Kaynaklarda bulunmuyorsa 'Hafizamda yetersiz bilgi' de. Madde madde yaz.\n\n"
        f"KAYNAKLAR:\n{context}\n\nSORU: {q}\n\nCEVAP (Turkce, kaynaklara dayali):"
    )
    # LLMCore.complete_sync (tek transport/routing; num_ctx+metrik passthrough). 503 kontratı korunur.
    from app.core.agents.llmcore import llm_core

    try:
        res = llm_core.complete_sync(
            prompt,
            task="rag",
            model=model,
            options={"temperature": temperature, "num_predict": max_tokens, "num_ctx": 8192},
            timeout=300,
            raise_on_error=True,
        )
    except Exception as e:
        raise HTTPException(503, f"ollama fail: {e}") from e
    eval_count = res.get("eval_count", 0)
    eval_duration = res.get("eval_duration", 1) / 1e9
    tps = round(eval_count / max(eval_duration, 0.001), 1)
    duration_ms = int((time.time() - t0) * 1000)
    _log_query("ask", q, project, None, top_k, hits, duration_ms, tokens=eval_count, tps=tps, request=request)
    return {
        "query": q,
        "project": project,
        "model": model,
        "answer": res.get("response", "").strip(),
        "sources": [
            {
                "score": float(h["score"]),
                "source": h["payload"].get("source"),
                "project": h["payload"].get("project"),
                "title": h["payload"].get("name") or h["payload"].get("title") or h["payload"].get("task") or h["payload"].get("file_path"),
            }
            for h in hits
        ],
        "stats": {
            "retrieval_count": len(hits),
            "tokens": eval_count,
            "duration_sec": round(eval_duration, 2),
            "tokens_per_sec": tps,
            "total_duration_ms": duration_ms,
        },
    }


@router.get("/projects")
def projects():
    from collections import Counter

    r = requests.post(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
        json={"limit": 10000, "with_payload": ["project"], "with_vector": False},
        timeout=30,
    )
    if not r.ok:
        raise HTTPException(503, f"qdrant fail: {r.status_code}")
    pts = r.json()["result"]["points"]
    counts = Counter(p["payload"].get("project", "general") for p in pts)
    return {
        "total": len(pts),
        "projects": [{"project": p, "count": c} for p, c in counts.most_common()],
    }


@router.get("/metrics")
def metrics(days: int = Query(30, ge=1, le=365)):
    """RAG kullanim metric ozeti (son N gun)"""
    since = int(time.time()) - days * 86400
    _init_metrics_db()  # Idempotent — bos DB'de tabloyu garanti et
    conn = sqlite3.connect(METRICS_DB, timeout=5)
    cur = conn.cursor()

    # Toplam istatistik
    cur.execute("SELECT COUNT(*), AVG(duration_ms), AVG(hit_count), AVG(top_score) FROM rag_queries WHERE ts >= ?", (since,))
    total, avg_dur, avg_hits, avg_score = cur.fetchone()

    # Endpoint dagilim
    cur.execute("SELECT endpoint, COUNT(*) FROM rag_queries WHERE ts >= ? GROUP BY endpoint", (since,))
    by_endpoint = dict(cur.fetchall())

    # Proje dagilim
    cur.execute("SELECT COALESCE(project, '(all)'), COUNT(*) FROM rag_queries WHERE ts >= ? GROUP BY project ORDER BY 2 DESC", (since,))
    by_project = [{"project": p, "count": c} for p, c in cur.fetchall()]

    # Top 20 sorgu (sik)
    cur.execute(
        """
        SELECT query, COUNT(*) cnt, AVG(top_score) score, AVG(duration_ms) dur
        FROM rag_queries WHERE ts >= ?
        GROUP BY query ORDER BY cnt DESC LIMIT 20
    """,
        (since,),
    )
    top_queries = [{"query": r[0], "count": r[1], "avg_score": r[2], "avg_duration_ms": r[3]} for r in cur.fetchall()]

    # Son 10 sorgu
    cur.execute(
        """
        SELECT ts, endpoint, query, project, hit_count, top_score, duration_ms, tokens
        FROM rag_queries WHERE ts >= ?
        ORDER BY ts DESC LIMIT 10
    """,
        (since,),
    )
    recent = [
        {"ts": r[0], "endpoint": r[1], "query": r[2], "project": r[3], "hits": r[4], "top_score": r[5], "duration_ms": r[6], "tokens": r[7]}
        for r in cur.fetchall()
    ]

    # Gunluk dagilim
    cur.execute(
        """
        SELECT DATE(ts, 'unixepoch') d, COUNT(*) cnt
        FROM rag_queries WHERE ts >= ?
        GROUP BY d ORDER BY d DESC LIMIT 30
    """,
        (since,),
    )
    daily = [{"date": r[0], "count": r[1]} for r in cur.fetchall()]

    conn.close()

    return {
        "period_days": days,
        "total_queries": total or 0,
        "avg_duration_ms": round(avg_dur, 1) if avg_dur else None,
        "avg_hit_count": round(avg_hits, 2) if avg_hits else None,
        "avg_top_score": round(avg_score, 3) if avg_score else None,
        "by_endpoint": by_endpoint,
        "by_project": by_project,
        "top_queries": top_queries,
        "daily": daily,
        "recent": recent,
    }
