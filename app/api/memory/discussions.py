"""Tartışma platformu MVP — 4-katılımcı yapılandırılmış tartışma (konu-2 sentezi #100561).

Turgut'un isteği: eşit söz-hakkı, dürüstlük yapısal. Sentez-yakınsaması (4/4):
- KÖR İLK TUR **API-zorlamalı**: topic 'open' fazındayken pozisyonlar yalnız YAZANA görünür
  (konvansiyon bugün 4 kez delindi — klipper #100554 taban-oranı + opencode ihlal-bildirimi).
  Herkes yazınca ya da 24h deadline dolunca otomatik açılır; yazmayan = çekimser.
- Append-only: düzeltme yok, fikir-değişikliği round-2+ yeni kayıt.
- Zorunlu şablon: pozisyon/dayanak/güven/beni-ne-ikna-eder/min-1-itiraz (Pydantic-validasyon).
- Sentez ≠ konsensüs: sentezci konu-açan OLAMAZ; ihtilaf birinci-sınıf; karar Turgut'a
  (decide master-key-only). Eşitlik söz-hakkında, yetkide değil.
- Kimlik P0'dan: device-key auth → yazan KEY'den (spoof imkânsız), master → body (unverified).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.memory import (
    DiscussionCreate,
    PositionCreate,
    dispatch_origin,
    get_db,
    router,
    verify_master_key,
)

# Dashboard-köprüsü (sentez: minimalist kart MVP'de — opencode #100555). Dashboard JWT
# konuşur, memory-API X-Memory-Key ister; master-key'i dashboard'a gömmek sızıntı-yüzeyi
# olurdu → JWT-korumalı ince proxy: JWT-auth = Turgut'un dashboard'ı = device 'turgut'.
from app.middleware.dependencies import require_auth  # noqa: E402

ui_router = APIRouter(prefix="/api/v1/discussions-ui", tags=["discussions-ui"], dependencies=[Depends(require_auth)])

_discussions_ready = False

_OPEN, _DISCUSSION, _NEEDS_TURGUT = "open", "discussion", "needs_turgut"
_FINAL = ("decided", "rejected")


def _ensure_discussions(db: Any) -> None:
    global _discussions_ready
    if _discussions_ready:
        return
    try:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS discussion_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            question TEXT NOT NULL,
            creator_device TEXT NOT NULL,
            expected_devices TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            synthesis TEXT,
            synthesizer_device TEXT,
            decision TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            blind_deadline TEXT DEFAULT (datetime('now','+24 hours'))
        );
        CREATE TABLE IF NOT EXISTS discussion_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER NOT NULL,
            device TEXT NOT NULL,
            round INTEGER DEFAULT 1,
            position TEXT NOT NULL,
            evidence TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            persuadable_by TEXT NOT NULL,
            objection TEXT NOT NULL,
            out_of_scope TEXT DEFAULT '',
            verified INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_disc_pos_blind
            ON discussion_positions(topic_id, device, round);
        """)
        db.commit()
    except Exception:
        pass
    _discussions_ready = True


def _actor(body_device: str, forced_origin: str) -> tuple[str, int]:
    """(device, verified) — P0 deseni: key-kimliği body-iddiasını ezer."""
    return (forced_origin, 1) if forced_origin else (body_device, 0)


def _maybe_open_blind(db: Any, topic: Any) -> str:
    """Kör-tur açılım koşulu (lazy): beklenen herkes round-1 yazdı YA DA deadline doldu →
    status='discussion'. Yazmayan çekimser sayılır (ekstra satır üretilmez — sentezci
    'pozisyon yok = çekimser' okur, opencode #100555 TTL-kuralı)."""
    if topic["status"] != _OPEN:
        return str(topic["status"])
    expected = {d.strip() for d in topic["expected_devices"].split(",") if d.strip()}
    written = {
        r[0] for r in db.execute("SELECT DISTINCT device FROM discussion_positions WHERE topic_id=? AND round=1", (topic["id"],)).fetchall()
    }
    deadline_passed = bool(
        db.execute("SELECT 1 FROM discussion_topics WHERE id=? AND blind_deadline < datetime('now')", (topic["id"],)).fetchone()
    )
    if expected.issubset(written) or deadline_passed:
        db.execute("UPDATE discussion_topics SET status=? WHERE id=? AND status=?", (_DISCUSSION, topic["id"], _OPEN))
        db.commit()
        return _DISCUSSION
    return _OPEN


def _load_topic(db: Any, topic_id: int) -> Any:
    row = db.execute("SELECT * FROM discussion_topics WHERE id=?", (topic_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Tartışma konusu bulunamadı")
    return row


@router.post("/discussions")
async def create_discussion(data: DiscussionCreate, forced_origin: str = Depends(dispatch_origin)) -> dict[str, Any]:
    device, _ = _actor(data.device, forced_origin)
    db = get_db()
    try:
        _ensure_discussions(db)
        cur = db.execute(
            "INSERT INTO discussion_topics (title, question, creator_device, expected_devices) VALUES (?, ?, ?, ?)",
            (data.title, data.question, device, data.expected_devices),
        )
        db.commit()
        return {
            "id": cur.lastrowid,
            "status": _OPEN,
            "creator_device": device,
            "hint": "Kör-tur: herkes yazana/24h dolana dek pozisyonlar yalnız yazana görünür",
        }
    finally:
        db.close()


@router.get("/discussions")
async def list_discussions(status: str | None = None) -> dict[str, Any]:
    db = get_db()
    try:
        _ensure_discussions(db)
        q = "SELECT id, title, question, creator_device, status, created_at, blind_deadline FROM discussion_topics"
        params: list[Any] = []
        if status:
            q += " WHERE status=?"
            params.append(status)
        rows = db.execute(q + " ORDER BY id DESC LIMIT 50", params).fetchall()
        return {"topics": [dict(r) for r in rows]}
    finally:
        db.close()


@router.post("/discussions/{topic_id}/positions")
async def add_position(topic_id: int, data: PositionCreate, forced_origin: str = Depends(dispatch_origin)) -> dict[str, Any]:
    device, verified = _actor(data.device, forced_origin)
    db = get_db()
    try:
        _ensure_discussions(db)
        topic = _load_topic(db, topic_id)
        status = _maybe_open_blind(db, topic)
        if status in _FINAL:
            raise HTTPException(409, f"Konu kapalı ({status}) — append-only arşiv, yeni pozisyon alınmaz")
        rnd = 1 if status == _OPEN else 2
        # Kör-turda cihaz başına TEK pozisyon (unique-index); round-2+ append-only serbest —
        # ama aynı (topic,device,round) çakışırsa round'u son-kayit+1'e ilerlet (fikir-evrimi izli)
        if rnd >= 2:
            last = db.execute(
                "SELECT COALESCE(MAX(round),1) FROM discussion_positions WHERE topic_id=? AND device=?", (topic_id, device)
            ).fetchone()[0]
            rnd = max(2, int(last) + 1) if last >= 2 else 2
        try:
            cur = db.execute(
                "INSERT INTO discussion_positions (topic_id, device, round, position, evidence, confidence, "
                "persuadable_by, objection, out_of_scope, verified) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    topic_id,
                    device,
                    rnd,
                    data.position,
                    data.evidence,
                    data.confidence,
                    data.persuadable_by,
                    data.objection,
                    data.out_of_scope,
                    verified,
                ),
            )
            db.commit()
        except Exception as e:
            db.rollback()
            if "UNIQUE" in str(e):
                raise HTTPException(
                    409, "Kör-turda cihaz başına tek pozisyon (append-only düzeltme round-2'de — kör-tur açılınca)"
                ) from None
            raise
        new_status = _maybe_open_blind(db, _load_topic(db, topic_id))
        return {"id": cur.lastrowid, "round": rnd, "device": device, "verified": bool(verified), "topic_status": new_status}
    finally:
        db.close()


@router.get("/discussions/{topic_id}/positions")
async def list_positions(topic_id: int, as_device: str = "", forced_origin: str = Depends(dispatch_origin)) -> dict[str, Any]:
    """KÖR-TUR GATE: topic 'open' iken yalnız KENDİ pozisyonların döner (API-zorlama, 4/4
    yakınsama). Viewer kimliği device-key'den; master-legacy ?as_device= ile bildirir."""
    viewer = forced_origin or as_device
    db = get_db()
    try:
        _ensure_discussions(db)
        topic = _load_topic(db, topic_id)
        status = _maybe_open_blind(db, topic)
        if status == _OPEN:
            rows = db.execute("SELECT * FROM discussion_positions WHERE topic_id=? AND device=? ORDER BY id", (topic_id, viewer)).fetchall()
            return {
                "status": status,
                "blind": True,
                "positions": [dict(r) for r in rows],
                "note": "Kör-tur: yalnız kendi pozisyonun görünür",
            }
        rows = db.execute("SELECT * FROM discussion_positions WHERE topic_id=? ORDER BY round, id", (topic_id,)).fetchall()
        return {
            "status": status,
            "blind": False,
            "positions": [dict(r) for r in rows],
            "synthesis": topic["synthesis"],
            "decision": topic["decision"],
        }
    finally:
        db.close()


@router.post("/discussions/{topic_id}/synthesize")
async def synthesize_discussion(topic_id: int, body: dict[str, str], forced_origin: str = Depends(dispatch_origin)) -> dict[str, Any]:
    """Sentez: konu-açan OLAMAZ (4/4). İhtilaf birinci-sınıf — sentez metni yakınsama VE
    ayrışmayı ayrı yazmalı (şablon zorlanmaz, kültür). Status → needs_turgut."""
    synthesis = (body.get("synthesis") or "").strip()
    device = forced_origin or (body.get("device") or "").strip()
    if len(synthesis) < 30:
        raise HTTPException(422, "synthesis en az 30 karakter (yakınsama + ihtilaf ayrı yazılmalı)")
    if not device:
        raise HTTPException(422, "device gerekli (ya device-key ile auth ol)")
    db = get_db()
    try:
        _ensure_discussions(db)
        topic = _load_topic(db, topic_id)
        if topic["status"] in _FINAL:
            raise HTTPException(409, "Konu zaten karara bağlandı")
        if device == topic["creator_device"]:
            raise HTTPException(403, "Sentezci konu-açan olamaz (4/4 yakınsama — bağımsız-göz ilkesi)")
        db.execute(
            "UPDATE discussion_topics SET synthesis=?, synthesizer_device=?, status=? WHERE id=?",
            (synthesis, device, _NEEDS_TURGUT, topic_id),
        )
        db.commit()
        return {"id": topic_id, "status": _NEEDS_TURGUT, "synthesizer": device}
    finally:
        db.close()


@router.post("/discussions/{topic_id}/decide", dependencies=[Depends(verify_master_key)])
async def decide_discussion(topic_id: int, body: dict[str, str]) -> dict[str, Any]:
    """Nihai karar — MASTER-key-only (eşitlik söz-hakkında, yetkide değil: karar Turgut'un).
    Ajan device-key'leri bu endpoint'e giremez."""
    decision = (body.get("decision") or "").strip()
    outcome = (body.get("status") or "decided").strip()
    if outcome not in _FINAL:
        raise HTTPException(422, f"status {_FINAL} olmalı")
    if len(decision) < 5:
        raise HTTPException(422, "decision en az 5 karakter")
    db = get_db()
    try:
        _ensure_discussions(db)
        _load_topic(db, topic_id)
        db.execute("UPDATE discussion_topics SET decision=?, status=? WHERE id=?", (decision, outcome, topic_id))
        db.commit()
        return {"id": topic_id, "status": outcome}
    finally:
        db.close()


@ui_router.get("/summary")
async def ui_summary() -> dict[str, Any]:
    """Dashboard kartı: açık konular + durum + pozisyon-sayısı (JWT, read-only)."""
    db = get_db()
    try:
        _ensure_discussions(db)
        rows = db.execute(
            "SELECT t.id, t.title, t.status, t.creator_device, t.created_at, t.blind_deadline, "
            "(SELECT COUNT(*) FROM discussion_positions p WHERE p.topic_id=t.id) AS n_positions "
            "FROM discussion_topics t ORDER BY t.id DESC LIMIT 20"
        ).fetchall()
        return {"topics": [dict(r) for r in rows]}
    finally:
        db.close()


@ui_router.post("/topics/{topic_id}/position")
async def ui_add_position(topic_id: int, data: PositionCreate) -> dict[str, Any]:
    """Turgut'un dashboard-formu: JWT-kimliği device='turgut' + verified=1 sayılır
    (dashboard'a yalnız Turgut erişir; body.device gözardı — P0 ilkesiyle tutarlı)."""
    forced = "turgut"
    db = get_db()
    try:
        _ensure_discussions(db)
        topic = _load_topic(db, topic_id)
        status = _maybe_open_blind(db, topic)
        if status in _FINAL:
            raise HTTPException(409, "Konu kapalı")
        rnd = 1 if status == _OPEN else 2
        if rnd >= 2:
            last = db.execute(
                "SELECT COALESCE(MAX(round),1) FROM discussion_positions WHERE topic_id=? AND device=?", (topic_id, forced)
            ).fetchone()[0]
            rnd = max(2, int(last) + 1) if last >= 2 else 2
        try:
            cur = db.execute(
                "INSERT INTO discussion_positions (topic_id, device, round, position, evidence, confidence, "
                "persuadable_by, objection, out_of_scope, verified) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    topic_id,
                    forced,
                    rnd,
                    data.position,
                    data.evidence,
                    data.confidence,
                    data.persuadable_by,
                    data.objection,
                    data.out_of_scope,
                ),
            )
            db.commit()
        except Exception as e:
            db.rollback()
            if "UNIQUE" in str(e):
                raise HTTPException(409, "Kör-turda tek pozisyon") from None
            raise
        return {"id": cur.lastrowid, "round": rnd, "device": forced}
    finally:
        db.close()
