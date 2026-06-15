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

import html as _htmllib
import re
import sqlite3
import time
from collections.abc import Callable

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import rag as rag_module
from app.api.memory import verify_key
from app.core.config import read_env_var
from app.core.research_agent import ResearchAgent
from app.models.schemas import ResearchConfig, ResearchReport

MEMORY_DB = "/opt/linux-ai-server/data/claude_memory.db"
LLM_MODEL = "qwen2.5:3b"  # default: ~35 tok/s, Turkce yeterli
LLM_MODEL_HI = "aya:8b"  # high-accuracy TR: Cohere aya-23, ~16 tok/s, daha dogal Turkce
OLLAMA_URL = "http://localhost:11434"
LLM_TIMEOUT = 90
LLM_NUM_PREDICT = 300  # 3B model @ ~35 tok/s -> 300 token ~9sn

# Anthropic fallback — daha hizli + daha iyi citation
ANTHROPIC_API_KEY = read_env_var("ANTHROPIC_API_KEY")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # hızlı/ucuz (Haiku)
ANTHROPIC_MODEL_SONNET = "claude-sonnet-4-6"  # derin sentez (daha güçlü akıl-yürütme)
ANTHROPIC_MAX_TOKENS = 600
ANTHROPIC_TIMEOUT = 30

router = APIRouter(prefix="/api/v1/research", tags=["research"], dependencies=[Depends(verify_key)])


# ───────── kaynak fetchers ─────────


def _qdrant_chunks(question: str, top_k: int = 5, project: str | None = None) -> list[dict]:
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
            f"SELECT id, from_device, title, content FROM notes WHERE ({' OR '.join(conds)}) ORDER BY created_at DESC LIMIT ?",
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


def _ollama_generate(prompt: str, model: str = LLM_MODEL) -> str:
    r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": LLM_NUM_PREDICT},
        },
        timeout=LLM_TIMEOUT,
    )
    if not r.ok:
        raise HTTPException(503, f"ollama generate fail: {r.status_code}")
    return r.json().get("response", "").strip()


def _anthropic_generate(system: str, user: str, model: str = ANTHROPIC_MODEL) -> str:
    """Claude (varsayılan Haiku; model= ile Sonnet seçilebilir). Citation tutarlı."""
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
            "model": model,
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


# ── Araştırma-ajanı sentez-modeli (FAZ1) ──
SYNTH_SYS = (
    "Sen kaynak-temelli araştırma sentez asistanısın. Verilen kaynaklara DAYANARAK, "
    "istenen formatı (özet + ÇIKARIMLAR madde-listesi) izle, iddiaları [1],[2] gibi "
    "kaynak numaralarıyla atıfla. Türkçe, mesleki, kaynak-dışı bilgi ekleme."
)


def _synth_llm(model: str) -> Callable[[str], str]:
    """Araştırma sentezi için model-seçici. sonnet → Claude Sonnet (en derin);
    haiku → Claude Haiku (hızlı); ikisi de fail/anahtar-yok → aya:8b yerel fallback.
    ollama → doğrudan aya:8b. Plan adımı ayrı (hep hızlı qwen); bu YALNIZ sentez içindir."""

    def aya(prompt: str) -> str:
        return _ollama_generate(prompt, model=LLM_MODEL_HI)

    if model == "ollama":
        return aya

    api_model = ANTHROPIC_MODEL_SONNET if model == "sonnet" else ANTHROPIC_MODEL

    def claude_then_aya(prompt: str) -> str:
        try:
            out = _anthropic_generate(SYNTH_SYS, prompt, model=api_model)
            if out.strip():
                return out
        except Exception:
            pass  # anahtar-yok / API-hata / boş → yerel fallback (araştırma düşmesin)
        return aya(prompt)

    return claude_then_aya


# ── Web arama (FAZ2): DDG-lite, anahtarsız, opt-in ──
WEB_UA = "Mozilla/5.0 (X11; Linux x86_64) klipper-research-agent"
WEB_TIMEOUT = 12
_WEB_LINK_RE = re.compile(r'<a rel="nofollow" href="([^"]+)"[^>]*class=[\'"]result-link[\'"][^>]*>(.*?)</a>', re.S)
_WEB_SNIP_RE = re.compile(r'class="result-snippet"[^>]*>(.*?)</td>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _htmllib.unescape(_TAG_RE.sub("", s)).strip()


def _web_search(query: str, n: int = 5) -> list[dict]:
    """DDG-lite anahtarsız web arama (FAZ2). HTML-parse toleranslı; ağ/parse fail → []
    (araştırma RAG'la devam eder). Skor rank-tabanlı sözde-skor (RAG ile karışsın, ezmesin)."""
    try:
        r = requests.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query},
            headers={"User-Agent": WEB_UA},
            timeout=WEB_TIMEOUT,
        )
        if not r.ok:
            return []
        page = r.text
    except Exception:
        return []
    links = _WEB_LINK_RE.findall(page)
    snips = _WEB_SNIP_RE.findall(page)
    out: list[dict] = []
    for i, (url, title) in enumerate(links[:n]):
        snippet = _strip_html(snips[i]) if i < len(snips) else ""
        out.append(
            {
                "type": "web",
                "id": url,
                "title": _strip_html(title)[:120] or url,
                "score": round(max(0.4, 0.65 - i * 0.04), 3),  # rank→sözde-skor (0.65↓)
                "text": snippet[:600],
            }
        )
    return out


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
    project: str | None = None
    # Default lokal qwen2.5:7b icin: discoveries+memories yeter, RAG genis context'i
    # cogu zaman yavaslatir (12 kaynak prompt -> ~70sn synth). include_rag=true ise
    # max_chunks=8 ile sinirlandirilir.
    include_rag: bool = False
    include_discoveries: bool = True
    include_memories: bool = True
    include_notes: bool = False  # gurultu cogu zaman
    max_chunks: int = 8
    # Engine:
    #   "local"    -> qwen2.5:3b (default, ~35 tok/s, 5-10sn)
    #   "local-hi" -> aya:8b (high-accuracy Turkce, ~16 tok/s, 10-20sn)
    #   "claude"   -> haiku 4.5 (~1-3sn, ~$0.007/call, citation tutarli)
    #   "auto"     -> max_chunks>=8 ise claude, yoksa local
    engine: str = "auto"


@router.post("/ask")
def research_ask(req: AskRequest):
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
        answer = _ollama_generate(prompt, model=LLM_MODEL)
    elif engine == "local-hi":
        # aya:8b dogal TR icin, citation icin DEGIL — 2026-05-24 prompt-tuning
        # testlerinde (v1/v2/v3) "kararli citation + relevance" dengesi
        # yakalanamadi. v1 (no per-engine tweak): 0 cit, konu-odakli. v2 (sert
        # kural): 4 cit ama konu-disi paragraflar. v3 (esnek): 0 cit. Aya 8B
        # structured format'i prompt icinden tutamiyor. Citation isteyen
        # kullanici /research-claude'a yonlendirilsin.
        prompt = f"{SYS_PROMPT}\n\n# Kaynaklar:\n{context}\n\n# Soru: {req.q}\n\n# Cevap:"
        answer = _ollama_generate(prompt, model=LLM_MODEL_HI)
    else:
        raise HTTPException(400, f"engine must be local|local-hi|claude|auto, got: {engine}")
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


@router.post("/run", response_model=ResearchReport)
def research_run(config: ResearchConfig) -> ResearchReport:
    """Otonom çok-aşamalı araştırma: planla→ara(Qdrant)→sentezle→atıflı-rapor.

    Auth: router-level verify_key (X-Memory-Key). RAG=canlı Qdrant (ChromaDB ÖLÜ).
    Plan=hızlı Ollama (qwen); SENTEZ=güçlü model (FAZ1: Haiku, fail'de aya:8b).
    Ağır iş → sync endpoint threadpool'da koşar, event-loop'u bloklamaz.
    """
    agent = ResearchAgent(
        llm=_ollama_generate,  # plan: hızlı/ucuz
        synth_llm=_synth_llm(config.synth_model),  # sentez: güçlü (Haiku/aya)
        search=lambda q, k, p: _qdrant_chunks(q, top_k=k, project=p),
        web_search=(lambda q, k: _web_search(q, k)) if config.include_web else None,  # FAZ2: opt-in
    )
    return agent.run(config)


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
