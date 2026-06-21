"""Sinyal-kalitesi katmanı testleri — fail-safe sözleşmesi (klipper KRİTİK guard) odaklı.

EN ÖNEMLİ kontrat: Ollama/Qdrant down/garbage → helper'lar güvenli-default döner,
ASLA raise etmez, memory yazma yolu bloklanmaz. Bu oturumun sessiz-fail salgını dersi:
degrade GÖRÜNÜR (signal_degraded), sessiz değil.
"""

import sqlite3

import pytest

from app.api.memory import signal_quality as sq


class _Boom:
    """requests.post/get her çağrıda patlar — Ollama/Qdrant down simülasyonu."""

    @staticmethod
    def post(*a, **k):
        raise ConnectionError("ollama/qdrant down")

    @staticmethod
    def get(*a, **k):
        raise ConnectionError("qdrant down")


@pytest.fixture
def disco_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE discoveries (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    db.execute("INSERT INTO discoveries (created_at) VALUES ('2026-01-01 00:00:00')")
    db.commit()
    yield db
    db.close()


# ---------- Migration ----------
def test_ensure_signal_columns_idempotent_and_backfill(disco_db):
    sq.ensure_signal_columns(disco_db)
    cols = {r[1] for r in disco_db.execute("PRAGMA table_info(discoveries)").fetchall()}
    assert {"valid_at", "invalid_at", "supersedes_id", "importance", "last_accessed"} <= cols
    # valid_at backfill = created_at
    row = disco_db.execute("SELECT created_at, valid_at FROM discoveries WHERE id=1").fetchone()
    assert row["valid_at"] == row["created_at"] == "2026-01-01 00:00:00"
    # idempotent: ikinci çağrı patlamaz, kolon iki-kez eklenmez
    sq.ensure_signal_columns(disco_db)
    cols2 = {r[1] for r in disco_db.execute("PRAGMA table_info(discoveries)").fetchall()}
    assert cols2 == cols


# ---------- Fail-safe sözleşmesi (KRİTİK) ----------
def test_importance_fail_safe_default_5(monkeypatch):
    monkeypatch.setattr(sq, "requests", _Boom)
    assert sq.score_importance("kritik bug", "detay") == sq.DEFAULT_IMPORTANCE == 5  # raise YOK


def test_embed_fail_safe_none(monkeypatch):
    monkeypatch.setattr(sq, "requests", _Boom)
    assert sq.embed_safe("herhangi metin") is None  # raise YOK


def test_semantic_dedup_fail_safe_add(monkeypatch):
    monkeypatch.setenv("SIGNAL_SEMANTIC_DEDUP", "1")  # autouse gate'i bu test icin ac (gercek embed-fail yolu)
    monkeypatch.setattr(sq, "requests", _Boom)
    out = sq.semantic_dedup(project="x", title="t", details="d")
    assert out["operation"] == "ADD"  # embed yok → ADD'e düş
    assert out["vector"] is None
    assert out.get("degraded")  # GÖRÜNÜR degrade


def test_qdrant_helpers_fail_safe(monkeypatch):
    monkeypatch.setattr(sq, "requests", _Boom)
    assert sq.ensure_collection() is False
    assert sq.search_similar([0.1] * sq.VECTOR_SIZE, "x") == []
    sq.upsert_discovery(1, [0.1] * sq.VECTOR_SIZE, {"project": "x"})  # raise YOK


# ---------- mem0 op-karar parse ----------
def test_dedup_decision_parsing(monkeypatch):
    matches = [{"id": 7, "score": 0.95, "payload": {"title": "eski"}}]
    monkeypatch.setattr(sq, "_ollama_json_safe", lambda *a, **k: {"operation": "UPDATE", "target_id": 7, "reason": "evrildi"})
    assert sq.dedup_decision({"title": "yeni"}, matches) == {"operation": "UPDATE", "target_id": 7, "reason": "evrildi"}
    # None/garbage → ADD
    monkeypatch.setattr(sq, "_ollama_json_safe", lambda *a, **k: None)
    assert sq.dedup_decision({"title": "y"}, matches)["operation"] == "ADD"
    # geçersiz op → ADD
    monkeypatch.setattr(sq, "_ollama_json_safe", lambda *a, **k: {"operation": "DESTROY"})
    assert sq.dedup_decision({"title": "y"}, matches)["operation"] == "ADD"
    # UPDATE ama target_id yok → güvenli ADD
    monkeypatch.setattr(sq, "_ollama_json_safe", lambda *a, **k: {"operation": "UPDATE", "target_id": None})
    assert sq.dedup_decision({"title": "y"}, matches)["operation"] == "ADD"


# ---------- Decay scoring (saf-hesap) ----------
def test_recency_and_decay_score():
    assert sq.recency_weight(0) == 1.0
    assert 0.45 < sq.recency_weight(138.6) < 0.55  # ~5.8 gün yarı-ömür
    assert sq.recency_weight(-10) == 1.0  # gelecek → clamp
    assert sq.decay_score(importance=10, hours_since_access=0, relevance=1.0) == pytest.approx(3.0)
    assert sq.decay_score(importance=None, hours_since_access=0, relevance=0.0) == pytest.approx(1.5)  # default 5


def test_cosine():
    assert sq.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert sq.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert sq.cosine([], [1]) == 0.0  # boyut-uyumsuz → 0
