"""Research API — multi-source synthesis (Qdrant + memory tables + qwen2.5).

Endpoint: POST /api/v1/research/ask

Pipeline:
  1. Soru kaynaklar uzerinde paralel sorgulanir:
       - Qdrant (rag) — semantik embedding araması
       - discoveries_fts (FTS5) — bug/fix/learning/architecture entries
       - memories — name + description LIKE (FTS yok)
       - notes (opsiyonel) — title + content LIKE
  2. Her chunk [type:id] etiketi ile context blokuna konur
  3. qwen2.5:7b "sadece kaynaklara dayan, [tag] format'inda atif goster"
     direktifiyle cevap uretir
  4. Post-process: cevaptaki [tag]'lerin gercek chunk'larda olmasi dogrulanir
     (hallucinated citation listesi yanitla birlikte donulur)

Auth: verify_key (memory API key). Internal sorgulara yonelik.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api import rag as rag_module
from app.api.memory import verify_key
from app.core.config import read_env_var

MEMORY_DB = "/opt/linux-ai-server/data/claude_memory.db"
LLM_MODEL = "qwen2.5:7b"
OLLAMA_URL = "http://localhost:11434"
LLM_TIMEOUT = 90
LLM_NUM_PREDICT = 300  # 7B model + 8K context, 300 token ~30-45sn

# Anthropic fallback — daha hizli + daha iyi citation
ANTHROPIC_API_KEY = read_env_var("ANTHROPIC_API_KEY")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_MAX_TOKENS = 600
ANTHROPIC_TIMEOUT = 30

router = APIRouter(prefix="/api/v1/research", tags=["research"], dependencies=[Depends(verify_key)])


# ───────── kaynak fetchers ─────────


def _qdrant_chunks(question: str, top_k: int = 5, project: Optional[str] = None) -> list[dict]:
    """rag._embed + rag._search uzerinden Qdrant top-K."""
    vec = rag_module._embed(question)
    hits = rag_module._search(vec, top_k=top_k, project=project)
    return [
        {
            "type": "rag",
            "id": str(h.get("id", "?")),
            "score": round(float(h.get("score", 0)), 3),
            "title": h.get("payload", {}).get("source", "?"),
            "project": h.get("payload", {}).get("project", ""),
            "text": (h.get("payload", {}).get("text", ""))[:600],
        }
        for h in hits
    ]


def _fts_q(q: str) -> str:
    """FTS5 sorgu — quote/star/colon/hyphen toxic. Hyphen "bilge-arena" gibi
    ifadeyi "bilge AND arena" diye parse edip 'no such column: arena' atiyor.
    Cozum: ozel karakterleri sok, kelimeleri OR ile birlestir.
    """
    cleaned = re.sub(r'[\'"*:.,/]+', " ", q)
    tokens = [t for t in re.split(r"[\s\-]+", cleaned) if len(t) > 2]
    return " OR ".join(tokens) if tokens else cleaned.strip()


def _discovery_chunks(question: str, limit: int = 8) -> list[dict]:
    db = sqlite3.connect(MEMORY_DB, timeout=3)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT d.id, d.project, d.type AS subtype, d.title, d.details, d.status "
            "FROM discoveries_fts f JOIN discoveries d ON d.id = f.rowid "
            "WHERE discoveries_fts MATCH ? ORDER BY rank LIMIT ?",
            (_fts_q(question), limit),
        ).fetchall()
        return [
            {
                "type": "discovery",
                "id": str(r["id"]),
                "project": r["project"] or "",
                "subtype": r["subtype"] or "",
                "title": r["title"] or "",
                "status": r["status"] or "active",
                "text": (r["details"] or "")[:600],
            }
            for r in rows
        ]
    finally:
        db.close()


def _memory_chunks(question: str, limit: int = 5) -> list[dict]:
    """memories.name+description LIKE — FTS yok."""
    terms = [t for t in re.findall(r"\w+", question, flags=re.UNICODE) if len(t) > 2][:5]
    if not terms:
        return []
    conds, params = [], []
    for t in terms:
        conds.append("(LOWER(name) LIKE ? OR LOWER(description) LIKE ?)")
        params.extend([f"%{t.lower()}%", f"%{t.lower()}%"])
    params.append(limit)
    db = sqlite3.connect(MEMORY_DB, timeout=3)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            f"SELECT id, type, name, description, content FROM memories "
            f"WHERE active=1 AND ({' OR '.join(conds)}) ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [
            {
                "type": "memory",
                "id": str(r["id"]),
                "subtype": r["type"],
                "title": r["name"],
                "text": ((r["description"] or "") + "\n" + (r["content"] or ""))[:600],
            }
            for r in rows
        ]
    finally:
        db.close()


def _notes_chunks(question: str, limit: int = 3) -> list[dict]:
    terms = [t for t in re.findall(r"\w+", question, flags=re.UNICODE) if len(t) > 2][:3]
    if not terms:
        return []
    conds, params = [], []
    for t in terms:
        conds.append("(LOWER(title) LIKE ? OR LOWER(content) LIKE ?)")
        params.extend([f"%{t.lower()}%", f"%{t.lower()}%"])
    params.append(limit)
    db = sqlite3.connect(MEMORY_DB, timeout=3)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            f"SELECT id, from_device, title, content FROM notes "
            f"WHERE ({' OR '.join(conds)}) ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [
            {
                "type": "note",
                "id": str(r["id"]),
                "from_device": r["from_device"],
                "title": r["title"],
                "text": (r["content"] or "")[:800],
            }
            for r in rows
        ]
    finally:
        db.close()


# ───────── synthesis ─────────


SYS_PROMPT = (
    "Sen Klipper'in dahili arastirma asistanisin. Asagida etiketli kaynaklar verildi. "
    "Soruyu YALNIZ bu kaynaklara dayanarak yanitla. Her iddiani [type:id] format'inda "
    "kaynak goster (or. [memory:485], [discovery:382], [rag:bilge.md], [note:12]). "
    "Kaynaktan dogrudan cikmayan bilgi yazma. Kaynak yetersiz ise 'Yetersiz kaynak' de. "
    "Cevap Turkce, kisa, mesleki ton."
)


def _compose_context(chunks: list[dict]) -> str:
    blocks = []
    for c in chunks:
        tag = f"[{c['type']}:{c['id']}]"
        title = c.get("title", "")
        blocks.append(f"{tag} {title}\n{c['text']}")
    return "\n\n---\n\n".join(blocks)


def _ollama_generate(prompt: str) -> str:
    r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": LLM_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": LLM_NUM_PREDICT},
        },
        timeout=LLM_TIMEOUT,
    )
    if not r.ok:
        raise HTTPException(503, f"ollama generate fail: {r.status_code}")
    return r.json().get("response", "").strip()


def _anthropic_generate(system: str, user: str) -> str:
    """Claude Haiku 4.5 — synthesis fallback. Citation izlemede daha tutarli."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(503, "ANTHROPIC_API_KEY .env'de tanimli degil")
    r = requests.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=ANTHROPIC_TIMEOUT,
    )
    if not r.ok:
        raise HTTPException(503, f"anthropic fail: {r.status_code} {r.text[:200]}")
    data = r.json()
    parts = data.get("content", [])
    text_parts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    return "".join(text_parts).strip()


_CITE_RE = re.compile(r"\[([a-z]+):([^\]\s]+)\]")


def _validate_citations(answer: str, chunks: list[dict]) -> dict:
    """Cevaptaki [type:id] referanslari gercek chunk'larda mi?"""
    cited = set(_CITE_RE.findall(answer))
    valid_tags = {(c["type"], c["id"]) for c in chunks}
    used = sorted({f"{t}:{i}" for t, i in cited & valid_tags})
    hallucinated = sorted({f"{t}:{i}" for t, i in cited - valid_tags})
    unused = sorted({f"{t}:{i}" for t, i in valid_tags - cited})
    return {"used": used, "hallucinated": hallucinated, "unused": unused}


# ───────── routes ─────────


class AskRequest(BaseModel):
    q: str
    top_k: int = 3  # Qdrant top-K (cogu durumda 3-5 yeter)
    project: Optional[str] = None
    # Default lokal qwen2.5:7b icin: discoveries+memories yeter, RAG genis context'i
    # cogu zaman yavaslatir (12 kaynak prompt -> ~70sn synth). include_rag=true ise
    # max_chunks=8 ile sinirlandirilir.
    include_rag: bool = False
    include_discoveries: bool = True
    include_memories: bool = True
    include_notes: bool = False  # gurultu cogu zaman
    max_chunks: int = 8
    # Engine: "local" (qwen2.5:7b, ucretsiz, 20-70sn) veya "claude" (haiku 4.5,
    # 1-3sn, ~$0.007/call, citation tutarli) veya "auto" (max_chunks>=8 ise claude)
    engine: str = "auto"


@router.post("/ask")
def research_ask(req: AskRequest, request: Request):
    t0 = time.time()
    chunks: list[dict] = []
    errors: dict[str, str] = {}

    if req.include_rag:
        try:
            chunks.extend(_qdrant_chunks(req.q, top_k=req.top_k, project=req.project))
        except HTTPException as e:
            errors["rag"] = str(e.detail)
    if req.include_discoveries:
        try:
            chunks.extend(_discovery_chunks(req.q, limit=8))
        except Exception as e:
            errors["discoveries"] = str(e)[:100]
    if req.include_memories:
        try:
            chunks.extend(_memory_chunks(req.q, limit=5))
        except Exception as e:
            errors["memories"] = str(e)[:100]
    if req.include_notes:
        try:
            chunks.extend(_notes_chunks(req.q, limit=3))
        except Exception as e:
            errors["notes"] = str(e)[:100]

    # Kaynak fazlaysa kap — qwen 7B prompt boyu hassas
    if len(chunks) > req.max_chunks:
        chunks = chunks[: req.max_chunks]

    duration_retrieval = int((time.time() - t0) * 1000)

    if not chunks:
        return {
            "question": req.q,
            "answer": "Yetersiz kaynak — kaynaklarda eslesen veri yok.",
            "sources": [],
            "source_count": 0,
            "citations": {"used": [], "hallucinated": [], "unused": []},
            "duration_ms": {"retrieval": duration_retrieval, "synthesis": 0, "total": duration_retrieval},
            "errors": errors or None,
        }

    # Engine secimi
    engine = req.engine
    if engine == "auto":
        # Yuksek kaynak yoğunluğu = Claude (qwen yavaş + citation tutarsız)
        engine = "claude" if len(chunks) >= 8 and ANTHROPIC_API_KEY else "local"

    t1 = time.time()
    context = _compose_context(chunks)
    if engine == "claude":
        user_msg = f"# Kaynaklar:\n{context}\n\n# Soru: {req.q}"
        answer = _anthropic_generate(SYS_PROMPT, user_msg)
    elif engine == "local":
        prompt = f"{SYS_PROMPT}\n\n# Kaynaklar:\n{context}\n\n# Soru: {req.q}\n\n# Cevap:"
        answer = _ollama_generate(prompt)
    else:
        raise HTTPException(400, f"engine must be local|claude|auto, got: {engine}")
    duration_synth = int((time.time() - t1) * 1000)

    citations = _validate_citations(answer, chunks)

    sources = [
        {
            "tag": f"{c['type']}:{c['id']}",
            "type": c["type"],
            "id": c["id"],
            **{k: v for k, v in c.items() if k in ("title", "project", "subtype", "status", "score")},
        }
        for c in chunks
    ]

    return {
        "question": req.q,
        "answer": answer,
        "engine": engine,
        "sources": sources,
        "source_count": len(sources),
        "citations": citations,
        "duration_ms": {
            "retrieval": duration_retrieval,
            "synthesis": duration_synth,
            "total": int((time.time() - t0) * 1000),
        },
        "errors": errors or None,
    }


@router.get("/health")
def research_health():
    out: dict = {}
    try:
        r = requests.get(f"{OLLAMA_URL}/api/version", timeout=3)
        out["ollama"] = {"ok": r.ok, "version": r.json().get("version") if r.ok else None}
    except Exception as e:
        out["ollama"] = {"ok": False, "error": str(e)[:100]}
    try:
        r = requests.get(f"{rag_module.QDRANT_URL}/collections/{rag_module.COLLECTION}", timeout=3)
        out["qdrant"] = {"ok": r.ok, "points": r.json().get("result", {}).get("points_count") if r.ok else None}
    except Exception as e:
        out["qdrant"] = {"ok": False, "error": str(e)[:100]}
    try:
        db = sqlite3.connect(MEMORY_DB, timeout=2)
        out["memory_db"] = {
            "ok": True,
            "memories": db.execute("SELECT COUNT(*) FROM memories WHERE active=1").fetchone()[0],
            "discoveries": db.execute("SELECT COUNT(*) FROM discoveries WHERE status='active'").fetchone()[0],
        }
        db.close()
    except Exception as e:
        out["memory_db"] = {"ok": False, "error": str(e)[:100]}
    out["anthropic"] = {"configured": bool(ANTHROPIC_API_KEY), "model": ANTHROPIC_MODEL}
    return out
