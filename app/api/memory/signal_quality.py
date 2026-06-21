"""Sinyal-kalitesi katmanı — discoveries bi-temporal + decay-scoring + semantik-dedup.

Tasarım sözleşmesi (klipper KRİTİK guard, PAZARLIK-DIŞI):
    Bu modüldeki HER Ollama/Qdrant çağrısı fail-safe'tir. Ollama/Qdrant down, boş
    veya garbage dönerse YAZMA YOLU **yine başarmalı** güvenli varsayılanlarla:
      - importance fail  → 5 (nötr)
      - semantic-dedup fail → ADD (mevcut exact-title-dedup zaten devreye girer)
      - qdrant upsert fail  → sessizce atla (dedup ileride exact-title'a düşer)
    Bir discovery ASLA LLM/vektör-fail yüzünden bloklanmaz/kaybedilmez.
    Her degrade DEGRADED-log ile GÖRÜNÜR yapılır (sessiz-fail salgını dersine karşı).

Ortak-substrat (memory write-path) Ollama-liveness'e BAĞIMLI DEĞİLDİR.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from typing import Any, cast

import requests

log = logging.getLogger("signal_quality")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
EMBED_MODEL = "bge-m3"
SIGNAL_LLM_MODEL = os.environ.get("SIGNAL_LLM_MODEL", "qwen2.5:7b")
DISCO_COLLECTION = "discoveries"
VECTOR_SIZE = 1024  # bge-m3
DEDUP_COSINE_THRESHOLD = 0.90
DEFAULT_IMPORTANCE = 5

# Decay: 0.995^saat ≈ 5.8 gün yarı-ömür (Generative Agents).
_DECAY_BASE = 0.995


def _degraded(op: str, reason: str) -> None:
    """Degrade GÖRÜNÜR log — sessiz değil. Yazma yolu devam eder, ama biliriz."""
    log.warning("[SIGNAL-DEGRADED] %s: %s", op, reason)


# ----------------------------------------------------------------------------
# Migration (idempotent — _ensure_read_by deseniyle birebir)
# ----------------------------------------------------------------------------
_SIGNAL_COLUMNS = {
    "valid_at": "TEXT",  # gerçek-dünya geçerlilik başlangıcı (default=created_at)
    "invalid_at": "TEXT",  # gerçek-dünyada geçersizleşme (obsolete/superseded/completed)
    "supersedes_id": "INTEGER",  # bu kayıt hangi (çözülmüş) kaydın regression'ı
    "importance": "INTEGER",  # 1-10 (decay-scoring), default 5
    "last_accessed": "TEXT",  # son okuma (recency bump)
}


def ensure_signal_columns(db: sqlite3.Connection) -> None:
    """discoveries'e bi-temporal/decay kolonlarını idempotent ekle + valid_at backfill.

    _ensure_read_by ile aynı sözleşme: PRAGMA table_info → eksik kolonu ALTER,
    her şey try/except (deploy-güvenli, eski DB'de de çalışır). Flag YOK: her çağrı
    ucuz PRAGMA-check, ALTER/backfill yalnız eksikse → multi-DB/test-güvenli.
    """
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(discoveries)").fetchall()}
        added = False
        for col, sqltype in _SIGNAL_COLUMNS.items():
            if col not in cols:
                db.execute(f"ALTER TABLE discoveries ADD COLUMN {col} {sqltype}")
                added = True
        if added:
            # valid_at backfill yalnız yeni-eklendiğinde (idempotent; sonraki çağrılar no-op).
            db.execute("UPDATE discoveries SET valid_at = created_at WHERE valid_at IS NULL")
        db.commit()
    except Exception as e:  # noqa: BLE001 — migration asla yazma yolunu patlatmamalı
        _degraded("ensure_signal_columns", repr(e))


# ----------------------------------------------------------------------------
# Ollama (embed + LLM) — fail-safe
# ----------------------------------------------------------------------------
def embed_safe(text: str) -> list[float] | None:
    """bge-m3 embedding. Fail → None (çağıran ADD/exact-title'a düşer)."""
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text or ""},
            timeout=20,
        )
        if r.status_code != 200:
            _degraded("embed", f"http {r.status_code}")
            return None
        vec = r.json().get("embedding")
        if not vec or len(vec) != VECTOR_SIZE:
            _degraded("embed", f"bad vector len={len(vec) if vec else 0}")
            return None
        return cast("list[float]", vec)
    except Exception as e:  # noqa: BLE001
        _degraded("embed", repr(e))
        return None


def _ollama_json_safe(prompt: str, *, op: str) -> dict[str, Any] | None:
    """qwen2.5'ten JSON cevap iste (format=json). Fail/parse-hatası → None."""
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": SIGNAL_LLM_MODEL, "prompt": prompt, "stream": False, "format": "json"},
            timeout=60,
        )
        if r.status_code != 200:
            _degraded(op, f"http {r.status_code}")
            return None
        raw = (r.json().get("response") or "").strip()
        if not raw:
            _degraded(op, "empty response")
            return None
        result = json.loads(raw)
        return cast("dict[str, Any]", result) if isinstance(result, dict) else None
    except Exception as e:  # noqa: BLE001
        _degraded(op, repr(e))
        return None


# ----------------------------------------------------------------------------
# Qdrant (raw HTTP — rag.py / rag_index_all.py deseniyle aynı) — fail-safe
# ----------------------------------------------------------------------------
def ensure_collection() -> bool:
    """`discoveries` collection'ı yoksa oluştur (bge-m3 1024 Cosine). Fail → False."""
    try:
        info = requests.get(f"{QDRANT_URL}/collections/{DISCO_COLLECTION}", timeout=5)
        if info.status_code == 200:
            return True
        r = requests.put(
            f"{QDRANT_URL}/collections/{DISCO_COLLECTION}",
            json={"vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}},
            timeout=15,
        )
        return r.status_code in (200, 201)
    except Exception as e:  # noqa: BLE001
        _degraded("qdrant_ensure_collection", repr(e))
        return False


def upsert_discovery(disc_id: int, vec: list[float], payload: dict[str, Any]) -> None:
    """Discovery vektörünü `discoveries` collection'a yaz. Fail → sessiz-atla (görünür-log)."""
    if not vec:
        return
    try:
        requests.put(
            f"{QDRANT_URL}/collections/{DISCO_COLLECTION}/points?wait=true",
            json={"points": [{"id": disc_id, "vector": vec, "payload": payload}]},
            timeout=20,
        )
    except Exception as e:  # noqa: BLE001
        _degraded("qdrant_upsert", repr(e))


def set_payload_status(disc_id: int, status: str) -> None:
    """Qdrant payload.status'u güncelle — status terminal'e geçince active-search'ten çıksın.

    Codex/klipper: status DB'de değişince Qdrant payload BAYAT kalıyordu → çözülmüş kayıt
    'active' görünüp yeni-bug'a yanlış-merge (data-loss) riski. Bu sync onu kapatır.
    Fail-safe — Qdrant down → atla (görünür-log). Gate kapalıysa no-op (vektör zaten yok).
    """
    if os.environ.get("SIGNAL_SEMANTIC_DEDUP", "1") != "1":
        return
    try:
        requests.post(
            f"{QDRANT_URL}/collections/{DISCO_COLLECTION}/points/payload?wait=true",
            json={"payload": {"status": status}, "points": [disc_id]},
            timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        _degraded("qdrant_set_payload", repr(e))


def search_similar(vec: list[float], project: str, dtype: str, top_k: int = 5) -> list[dict[str, Any]]:
    """Aynı project + AYNI type'ta aktif benzer discovery'ler (cosine skorlu). Fail → [].

    Codex P2: type-scope — bir 'bug' bir 'architecture' kaydıyla dedup edilmemeli.
    """
    if not vec:
        return []
    try:
        body = {
            "vector": vec,
            "limit": top_k,
            "with_payload": True,
            "filter": {
                "must": [
                    {"key": "project", "match": {"value": project}},
                    {"key": "type", "match": {"value": dtype}},
                    {"key": "status", "match": {"value": "active"}},
                ]
            },
        }
        r = requests.post(f"{QDRANT_URL}/collections/{DISCO_COLLECTION}/points/search", json=body, timeout=15)
        if r.status_code != 200:
            _degraded("qdrant_search", f"http {r.status_code}")
            return []
        return r.json().get("result", []) or []
    except Exception as e:  # noqa: BLE001
        _degraded("qdrant_search", repr(e))
        return []


# ----------------------------------------------------------------------------
# Importance scoring (Generative Agents 1-10) — fail-safe → 5
# ----------------------------------------------------------------------------
def score_importance(title: str, details: str | None) -> int:
    """qwen2.5 ile 1-10 önem skoru (TR-prompt). Fail/garbage → DEFAULT_IMPORTANCE (5)."""
    prompt = (
        "Bir yazılım/altyapı bulgusunun önem derecesini 1-10 arası değerlendir. "
        '1=önemsiz/rutin, 10=kritik/acil. SADECE şu JSON: {"importance": <1-10 tamsayı>}\n\n'
        f"Başlık: {title}\nDetay: {(details or '')[:500]}"
    )
    data = _ollama_json_safe(prompt, op="score_importance")
    if not data:
        return DEFAULT_IMPORTANCE
    try:
        val = int(data.get("importance", 0))
        if 1 <= val <= 10:
            return val
        _degraded("score_importance", f"out of range: {val}")
    except (TypeError, ValueError) as e:
        _degraded("score_importance", repr(e))
    return DEFAULT_IMPORTANCE


# ----------------------------------------------------------------------------
# Semantik-dedup (mem0 ADD/UPDATE/NOOP/SUPERSEDE) — fail-safe → ADD
# ----------------------------------------------------------------------------
_VALID_OPS = {"ADD", "UPDATE", "NOOP", "SUPERSEDE"}


def dedup_decision(candidate: dict[str, Any], matches: list[dict[str, Any]]) -> dict[str, Any]:
    """qwen2.5'e aday + benzer-aktif kayıtları verip operasyon seçtir (mem0 deseni).

    Dönen: {"operation": ADD|UPDATE|NOOP|SUPERSEDE, "target_id": int|None, "reason": str}
    Fail/garbage/geçersiz-op → {"operation": "ADD"} (yeni kayıt; asla blok yok).
    """
    cand_txt = f"Başlık: {candidate.get('title')}\nDetay: {(candidate.get('details') or '')[:400]}"
    match_lines = "\n".join(f"- id={m['id']} (benzerlik={m.get('score', 0):.2f}) {m.get('payload', {}).get('title', '')}" for m in matches)
    prompt = (
        "Yeni bir bulgu (ADAY) ile mevcut AKTİF benzer bulgular verildi. Operasyon seç:\n"
        "ADD=gerçekten yeni/farklı bir bulgu. UPDATE=aynı konunun evrilmişi (mevcut kaydı güncelle). "
        "NOOP=aynısının tekrarı (yeni kayıt gereksiz). SUPERSEDE=mevcut çözülmüş/eski bir bulgunun "
        "yeniden ortaya çıkışı (regression).\n"
        'SADECE şu JSON: {"operation": "ADD|UPDATE|NOOP|SUPERSEDE", "target_id": <id veya null>, '
        '"reason": "<kısa>"}\n\n'
        f"ADAY:\n{cand_txt}\n\nMEVCUT AKTİF BENZERLER:\n{match_lines}"
    )
    data = _ollama_json_safe(prompt, op="dedup_decision")
    if not data:
        return {"operation": "ADD", "degraded": True}
    op = str(data.get("operation", "")).upper()
    if op not in _VALID_OPS:
        _degraded("dedup_decision", f"invalid op: {op!r}")
        return {"operation": "ADD", "degraded": True}
    if op == "ADD":
        return {"operation": "ADD"}
    target = data.get("target_id")
    try:
        target = int(target) if target is not None else None
    except (TypeError, ValueError):
        target = None
    # UPDATE/NOOP/SUPERSEDE target_id ister; yoksa güvenli tarafta ADD'e düş.
    if target is None:
        _degraded("dedup_decision", f"{op} target_id yok → ADD")
        return {"operation": "ADD", "degraded": True}
    # Codex P2: LLM, dönen matches-DIŞI bir id hayal edebilir → reddet (ADD).
    if target not in {m.get("id") for m in matches}:
        _degraded("dedup_decision", f"{op} target {target} matches-dışı → ADD")
        return {"operation": "ADD", "degraded": True}
    return {"operation": op, "target_id": target, "reason": str(data.get("reason", ""))[:200]}


def semantic_dedup(*, project: str, dtype: str, title: str, details: str | None) -> dict[str, Any]:
    """Tam akış: embed → Qdrant ara → eşik üstü benzer varsa op-kararı.

    Dönen: {"operation": ..., "target_id"?, "vector"?, "degraded"?}
    HER fail-yolu ADD'e düşer (vector=None ise upsert atlanır). ASLA exception fırlatmaz.

    Env-gate SIGNAL_SEMANTIC_DEDUP=0 → tamamen atla (ADD). Ops kill-switch + testlerde
    deterministik (canlı Ollama/Qdrant'a bağımlı değil). Default '1' (açık).
    """
    if os.environ.get("SIGNAL_SEMANTIC_DEDUP", "1") != "1":
        return {"operation": "ADD", "vector": None, "degraded": "disabled"}
    vec = embed_safe(f"{title}\n{details or ''}")
    if vec is None:
        # Embed yok → semantik-dedup atla, exact-title-dedup (mevcut) devralır.
        return {"operation": "ADD", "vector": None, "degraded": "embed"}
    matches = [m for m in search_similar(vec, project, dtype) if m.get("score", 0) >= DEDUP_COSINE_THRESHOLD]
    if not matches:
        return {"operation": "ADD", "vector": vec}
    decision = dedup_decision({"title": title, "details": details}, matches)
    decision["vector"] = vec
    return decision


# ----------------------------------------------------------------------------
# Decay-scoring (recency × importance × relevance) — saf-hesap, fail yok
# ----------------------------------------------------------------------------
def recency_weight(hours_since_access: float) -> float:
    """0.995^saat üstel çürüme. Negatif saat (gelecek) → 1.0 clamp."""
    return float(_DECAY_BASE ** max(0.0, hours_since_access))


def decay_score(importance: int | None, hours_since_access: float, relevance: float) -> float:
    """score = recency + importance/10 + relevance (üçü de ~[0,1])."""
    imp = (importance if importance is not None else DEFAULT_IMPORTANCE) / 10.0
    return recency_weight(hours_since_access) + imp + max(0.0, min(1.0, relevance))


def cosine(a: list[float], b: list[float]) -> float:
    """İki vektör cosine benzerliği (test/yardımcı)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
