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
import json as _json
import re
import sqlite3
import subprocess
import time
from collections.abc import Callable
from typing import Any

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import claude_code as cc_module
from app.api import rag as rag_module
from app.api.memory import verify_key
from app.core.research_agent import ResearchAgent
from app.models.schemas import ResearchConfig, ResearchReport

MEMORY_DB = "/opt/linux-ai-server/data/claude_memory.db"
LLM_MODEL = "qwen2.5:3b"  # default: ~35 tok/s, Turkce yeterli
LLM_MODEL_HI = "aya:8b"  # high-accuracy TR: Cohere aya-23, ~16 tok/s, daha dogal Turkce
OLLAMA_URL = "http://localhost:11434"
LLM_TIMEOUT = 90
LLM_NUM_PREDICT = 300  # 3B model @ ~35 tok/s -> 300 token ~9sn

# Claude sentezi — MAX-ABONELİK CLI üzerinden (_anthropic_generate). Doğrudan API (API-key)
# KULLANILMAZ (kullanıcı standing tercihi "API istemiyorum"). Gate de CLI-varlığına bakar.
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # hızlı/ucuz (Haiku)
ANTHROPIC_MODEL_SONNET = "claude-sonnet-4-6"  # derin sentez (daha güçlü akıl-yürütme)
ANTHROPIC_MAX_TOKENS = 600
ANTHROPIC_TIMEOUT = 30

router = APIRouter(prefix="/api/v1/research", tags=["research"], dependencies=[Depends(verify_key)])


# ───────── kaynak fetchers ─────────


def _qdrant_chunks(question: str, top_k: int = 5, project: str | None = None) -> list[dict[str, Any]]:
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


def _discovery_chunks(question: str, limit: int = 8) -> list[dict[str, Any]]:
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


def _memory_chunks(question: str, limit: int = 5) -> list[dict[str, Any]]:
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


def _notes_chunks(question: str, limit: int = 3) -> list[dict[str, Any]]:
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


def _compose_context(chunks: list[dict[str, Any]]) -> str:
    blocks = []
    for c in chunks:
        tag = f"[{c['type']}:{c['id']}]"
        title = c.get("title", "")
        blocks.append(f"{tag} {title}\n{c['text']}")
    return "\n\n---\n\n".join(blocks)


def _ollama_generate(prompt: str, model: str = LLM_MODEL) -> str:
    """LLMCore.generate_sync üzerinden (tek transport/routing noktası). 503 kontratı korunur."""
    from app.core.agents.llmcore import llm_core

    try:
        return llm_core.generate_sync(
            prompt, task="research", model=model, num_predict=LLM_NUM_PREDICT, timeout=LLM_TIMEOUT, raise_on_error=True
        )
    except Exception as e:
        raise HTTPException(503, f"ollama generate fail: {e}") from e


CLAUDE_CLI_TIMEOUT = 90  # CLI spawn doğrudan-API'den yavaş; multi-hop için yeterli pencere


def _claude_available() -> bool:
    """Max-abonelik claude CLI mevcut mu — auto-engine gate'i (API-key DEĞİL; çağrı zaten CLI)."""
    return bool(cc_module._find_claude())


def _anthropic_generate(system: str, user: str, model: str = ANTHROPIC_MODEL) -> str:
    """Claude sentezi MAX ABONELİK üzerinden (claude CLI, ~/.claude OAuth) — eskiden
    doğrudan API (ANTHROPIC_API_KEY) idi; kredi bitince "Credit balance is too low" ile
    düşüyordu. Artık _build_env() API-key/auth-token'ı strip eder → abonelik kimliği =
    sıfır API faturası (#156 spawn-fix ile aynı desen). Sync (research /run threadpool'da
    koşar → subprocess bloklaması event-loop'u kesmez). Tool-suz salt-üretim (-p headless).
    Fail → çağıran aya:8b'ye düşer (research düşmesin). Ad legacy; transport=CLI."""
    binary = cc_module._find_claude()
    if not binary:
        raise HTTPException(503, "claude CLI bulunamadi (abonelik sentezi icin gerekli)")
    env = cc_module._build_env()  # ANTHROPIC_API_KEY/AUTH strip → Max abonelik
    proc = subprocess.run(
        [binary, "-p", user, "--append-system-prompt", system, "--model", model, "--output-format", "json"],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=CLAUDE_CLI_TIMEOUT,
    )
    if proc.returncode != 0:
        raise HTTPException(503, f"claude cli rc={proc.returncode}: {(proc.stderr or '')[:200]}")
    data = _json.loads(proc.stdout or "{}")
    if data.get("is_error"):
        raise HTTPException(503, f"claude cli error: {str(data.get('result'))[:200]}")
    return str(data.get("result", "")).strip()


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
# Semantik web-alaka eşiği (bge-m3 cosine). Canlı-smoke kalibrasyonu (06-15):
# off-topic homonim sonuçlar (ör. "klipper" 3D-yazıcı firmware'i) 0.36–0.53,
# gerçekten ilgili içerik 0.62+. 0.55 ikisini ayırır. Token-örtüşmesi tek güçlü
# homonim token'da (her ikisine de uyan) çuvallıyordu — anlamsal kapı bunu çözer.
WEB_RELEVANCE_MIN_COS = 0.55
# Anlamsal kapı için en fazla kaç aday embed'lenir (DDG zaten rank-sıralı döner).
WEB_RELEVANCE_MAX_EMBED = 10
_WEB_LINK_RE = re.compile(r'<a rel="nofollow" href="([^"]+)"[^>]*class=[\'"]result-link[\'"][^>]*>(.*?)</a>', re.S)
_WEB_SNIP_RE = re.compile(r'class="result-snippet"[^>]*>(.*?)</td>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _htmllib.unescape(_TAG_RE.sub("", s)).strip()


def _web_search(query: str, n: int = 5) -> list[dict[str, Any]]:
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
    cands: list[dict[str, Any]] = []
    for i, (url, title) in enumerate(links):
        snippet = _strip_html(snips[i]) if i < len(snips) else ""
        cands.append({"url": url, "title": _strip_html(title)[:120] or url, "text": snippet[:600]})
    cands = _filter_relevant(cands, query, n)
    return [
        {
            "type": "web",
            "id": c["url"],
            "title": c["title"],
            "score": round(max(0.4, 0.65 - i * 0.04), 3),  # rank→sözde-skor (0.65↓)
            "text": c["text"],
        }
        for i, c in enumerate(cands)
    ]


def _cosine(a: list[float], b: list[float]) -> float:
    """Saf-python cosine benzerliği (numpy bağımlılığı yok)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _token_filter(cands: list[dict[str, Any]], query: str, n: int) -> list[dict[str, Any]]:
    """Ucuz token-örtüşme filtresi (anlamsal kapı kullanılamazsa fallback). ≥4-harf
    query token'ı title+snippet'te hiç geçmeyeni at; hiçbiri kalmazsa boş dön
    (ham-listeye dönmek off-topic kirliliği geri sokardı — dürüst boş daha iyi)."""
    toks = set(re.findall(r"\w{4,}", query.lower()))
    if not toks:
        return cands[:n]
    return [c for c in cands if any(t in (c["title"] + " " + c["text"]).lower() for t in toks)][:n]


def _filter_relevant(cands: list[dict[str, Any]], query: str, n: int) -> list[dict[str, Any]]:
    """Off-topic web sonuçlarını anlamsal-benzerlikle ele. Token-örtüşmesi tek güçlü
    homonim token'da çuvallıyordu (ör. "klipper" hem bu-sunucu hem 3D-yazıcı firmware'i
    → printer sayfaları geçiyordu). bge-m3 embed + cosine: query'ye anlamsal yakınlığı
    WEB_RELEVANCE_MIN_COS altındaki adayları at, skora göre sırala. Embed/Ollama fail →
    token-örtüşme fallback (asla hard-fail etme; web opt-in ve fail'de RAG'la devam)."""
    if not cands:
        return []
    try:
        qv = rag_module._embed(query)
        scored: list[tuple[float, dict[str, Any]]] = []
        for c in cands[:WEB_RELEVANCE_MAX_EMBED]:
            cv = rag_module._embed(f"{c['title']} {c['text']}")
            scored.append((_cosine(qv, cv), c))
        kept = sorted(
            (sc for sc in scored if sc[0] >= WEB_RELEVANCE_MIN_COS),
            key=lambda sc: sc[0],
            reverse=True,
        )
        return [c for _, c in kept[:n]]
    except Exception:
        return _token_filter(cands, query, n)


_CITE_RE = re.compile(r"\[([a-z]+):([^\]\s]+)\]")


def _validate_citations(answer: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
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
    chunks: list[dict[str, Any]] = []
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
        # Yuksek kaynak yoğunluğu = Claude (qwen yavaş + citation tutarsız). Gate CLI-varlığına
        # bakar (API-key DEĞİL): çağrı zaten _anthropic_generate=CLI; key silinse de CLI çalışır.
        engine = "claude" if len(chunks) >= 8 and _claude_available() else "local"

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


def _persist_research(report: ResearchReport, project: str | None) -> int | None:
    """Araştırma raporunu discoveries'e 'learning' olarak kaydet → /ask gelecekte FTS ile
    bulur (kümülatif araştırma). Aynı topic tekrar = upsert (partial unique index
    project+type+title WHERE active). Yazma fail → None (rapor yine döner; kayıt best-effort)."""
    proj = project or "linux-ai-server"
    title = f"[araştırma] {report.topic}"[:200]
    findings = "\n".join(f"- {f}" for f in report.findings[:10])
    details = (
        f"{report.summary}\n\nÇIKARIMLAR:\n{findings or '(yok)'}\n\n"
        f"(güven={report.confidence_score}, kaynak={len(report.sources)}, alt-soru={len(report.subquestions)})"
    )
    try:
        db = sqlite3.connect(MEMORY_DB, timeout=5)
        try:
            db.execute("PRAGMA busy_timeout=5000")  # lock'ta hemen READONLY atma ([[fix_db_retention]])
            # Açık insert/update — discoveries_fts external-content + trigger YOK; FTS'i
            # elle senkronlamalıyız (yoksa /ask kaydı bulamaz). Upsert'te eski FTS girdisini
            # 'delete' ile sil, yeniyi ekle (re-insert çift-indekslerdi).
            existing = db.execute(
                "SELECT id, title, details FROM discoveries WHERE project=? AND type='learning' AND title=? AND status='active'",
                (proj, title),
            ).fetchone()
            if existing:
                rid, old_title, old_details = int(existing[0]), existing[1], existing[2]
                db.execute("UPDATE discoveries SET details=?, created_at=datetime('now') WHERE id=?", (details, rid))
                _sync_fts_replace(db, rid, old_title, old_details or "", title, details)
            else:
                cur = db.execute(
                    "INSERT INTO discoveries (project, type, title, details, status, device_name) "
                    "VALUES (?, 'learning', ?, ?, 'active', 'klipper')",
                    (proj, title, details),
                )
                rid = int(cur.lastrowid)
                _sync_fts_replace(db, rid, None, None, title, details)
            db.commit()
            return rid
        finally:
            db.close()
    except Exception:
        return None


def _sync_fts_replace(db, rid: int, old_title, old_details, new_title: str, new_details: str) -> None:
    """discoveries_fts (external-content) elle senkron: eski girdi varsa 'delete', sonra
    yeniyi ekle. FTS tablosu yok/hatalıysa sessiz geç (best-effort, [[memory._sync_fts]] deseni)."""
    try:
        if old_title is not None:
            db.execute(
                "INSERT INTO discoveries_fts(discoveries_fts, rowid, title, details) VALUES('delete', ?, ?, ?)",
                (rid, old_title, old_details or ""),
            )
        db.execute("INSERT INTO discoveries_fts(rowid, title, details) VALUES (?, ?, ?)", (rid, new_title, new_details))
    except Exception:
        pass


@router.post("/run", response_model=ResearchReport)
def research_run(config: ResearchConfig) -> ResearchReport:
    """Otonom çok-aşamalı araştırma: planla→ara(Qdrant)→sentezle→atıflı-rapor.

    Auth: router-level verify_key (X-Memory-Key). RAG=canlı Qdrant (ChromaDB ÖLÜ).
    Plan=hızlı Ollama (qwen). SENTEZ=synth_model: sonnet(varsayılan)/haiku/ollama —
    Claude'lar fail/anahtar-yok'ta aya:8b'ye düşer. Web=opt-in (include_web). Multi-hop=max_hops.
    Critic=opt-in (config.critic): sentezi eleştir→gerekirse tek revizyon (synth_model'de).
    Save=opt-in (config.save): raporu discoveries'e 'learning' kaydet (kümülatif araştırma).
    Ağır iş → sync endpoint threadpool'da koşar, event-loop'u bloklamaz.
    """
    synth = _synth_llm(config.synth_model)
    agent = ResearchAgent(
        llm=_ollama_generate,  # plan: hızlı/ucuz
        synth_llm=synth,  # sentez: sonnet/haiku/aya
        search=lambda q, k, p: _qdrant_chunks(q, top_k=k, project=p),
        web_search=(lambda q, k: _web_search(q, k)) if config.include_web else None,  # FAZ2: opt-in
        critic_llm=synth,  # FAZ5: critic = sentezle aynı güçlü model (config.critic ile aktif)
    )
    report = agent.run(config)
    if config.save:  # FAZ6: opt-in kalıcılaştırma (best-effort; fail rapora dokunmaz)
        report.saved_discovery_id = _persist_research(report, config.project)
    return report


@router.get("/health")
def research_health():
    out: dict[str, Any] = {}
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
    out["anthropic"] = {
        # Artık MAX ABONELİK (claude CLI, ~/.claude OAuth) — API-key DEĞİL (#156 deseni).
        # configured = CLI bulunuyor mu (sentez buna bağlı); API-key'e bakmaz.
        "auth_mode": "subscription-cli",
        "configured": bool(cc_module._find_claude()),
        "model": ANTHROPIC_MODEL,  # /ask varsayılanı (Haiku) — geriye-uyum
        "synth_models": {"haiku": ANTHROPIC_MODEL, "sonnet": ANTHROPIC_MODEL_SONNET},
        "default_synth_model": "sonnet",
    }
    return out
