"""P1 CLAIM-lock — active_claims DB-kısıtı (konu-1 kararı, klipper #100549 şeması).

Not-konvansiyonu yerine atomik acquire: partial-UNIQUE + 409. Bugünkü iki canlı çakışma
(PR#301 paralel-fix, disc#1288-1290 üç-ajan) sınıfının motor-seviyesi önlemi.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import TEST_MEMORY_KEY
from tests.test_memory_api import memory_db  # noqa: F401 (fixture)

_CLAIMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS active_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key TEXT NOT NULL,
    device TEXT NOT NULL,
    repo TEXT,
    branch TEXT,
    note TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    released_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_claims_key ON active_claims(task_key) WHERE active=1;
"""


@pytest.fixture(autouse=True)
def _claims_db(memory_db, monkeypatch):  # noqa: F811
    from app.api import memory as mem_module
    from app.api.memory import claims as claims_module

    con = sqlite3.connect(memory_db)
    con.executescript(_CLAIMS_SCHEMA)
    con.commit()
    con.close()
    monkeypatch.setattr(claims_module, "_claims_ready", False)
    monkeypatch.setattr(mem_module, "_device_keys_ready", False)
    return memory_db


def _hdr(key=TEST_MEMORY_KEY):
    return {"X-Memory-Key": key}


async def _acquire(client, task_key, device="surer", key=TEST_MEMORY_KEY, **kw):
    body = {"task_key": task_key, "device": device, **kw}
    return await client.post("/api/v1/memory/claims", json=body, headers=_hdr(key))


@pytest.mark.usefixtures("_claims_db")
async def test_acquire_then_conflict_409(client):
    r1 = await _acquire(client, "claude-server:memory-api", device="surer")
    assert r1.status_code == 200
    assert r1.json()["status"] == "acquired"

    r2 = await _acquire(client, "claude-server:memory-api", device="klipper")
    assert r2.status_code == 409
    detail = r2.json()["detail"]
    assert detail["holder"]["device"] == "surer"  # ikinci gelen KIMIN tuttugunu gorur


@pytest.mark.usefixtures("_claims_db")
async def test_release_then_reacquire(client):
    cid = (await _acquire(client, "bilge-arena:quiz", device="surer")).json()["id"]
    r = await client.put(f"/api/v1/memory/claims/{cid}/release", headers=_hdr())
    assert r.json()["status"] == "released"
    assert (await _acquire(client, "bilge-arena:quiz", device="klipper")).status_code == 200


@pytest.mark.usefixtures("_claims_db")
async def test_release_requires_owner_or_master(client):
    # surer'in claim'ini opencode'un DEVICE-KEY'i release EDEMEZ (403); master EDEBILIR
    surer_key = (await client.post("/api/v1/memory/devices/surer/key", headers=_hdr())).json()["key"]
    opencode_key = (await client.post("/api/v1/memory/devices/opencode/key", headers=_hdr())).json()["key"]

    cid = (await _acquire(client, "claude-server:automation", key=surer_key, device="SAHTE")).json()["id"]
    # P0 entegrasyonu: device-key ile acquire -> kimlik KEY'den (body 'SAHTE' gecersiz)
    lst = (await client.get("/api/v1/memory/claims", headers=_hdr())).json()["claims"]
    assert lst[0]["device"] == "surer"

    r = await client.put(f"/api/v1/memory/claims/{cid}/release", headers=_hdr(opencode_key))
    assert r.status_code == 403
    r = await client.put(f"/api/v1/memory/claims/{cid}/release", headers=_hdr())  # master
    assert r.status_code == 200


async def test_ttl_lazy_expiry_frees_key(client, _claims_db):  # noqa: PT019 (deger sqlite3.connect icin kullanilir)
    cid = (await _acquire(client, "renderhane:seo", device="klipper")).json()["id"]
    con = sqlite3.connect(_claims_db)
    con.execute("UPDATE active_claims SET expires_at=datetime('now','-1 hours') WHERE id=?", (cid,))
    con.commit()
    con.close()
    # TTL dolmus -> yeni acquire lazy-expiry ile basarili (crash-korumasi)
    r = await _acquire(client, "renderhane:seo", device="surer")
    assert r.status_code == 200


async def test_renew_extends(client, _claims_db):  # noqa: PT019 (deger sqlite3.connect icin kullanilir)
    cid = (await _acquire(client, "koken:magaza", device="surer", ttl_hours=0.5)).json()["id"]
    r = await client.put(f"/api/v1/memory/claims/{cid}/renew?ttl_hours=8", headers=_hdr())
    assert r.json()["status"] == "renewed"
    con = sqlite3.connect(_claims_db)
    exp = con.execute("SELECT expires_at > datetime('now','+7 hours') FROM active_claims WHERE id=?", (cid,)).fetchone()[0]
    con.close()
    assert exp == 1


@pytest.mark.usefixtures("_claims_db")
async def test_list_filters_repo_branch(client):
    await _acquire(client, "a:x", device="surer", repo="claude-server", branch="feat/x")
    await _acquire(client, "b:y", device="klipper", repo="bilge-arena", branch="fix/y")
    r = await client.get("/api/v1/memory/claims?repo=claude-server", headers=_hdr())
    claims = r.json()["claims"]
    assert len(claims) == 1  # CI-gate botunun sorgusu
    assert claims[0]["branch"] == "feat/x"


async def test_renew_expired_claim_404_no_false_renewed(client, _claims_db):  # noqa: PT019 (deger sqlite3.connect icin kullanilir)
    # Codex#303-P2: renew artik expiry+read+owner+UPDATE'i tek BEGIN IMMEDIATE'de yapar.
    # TTL'i dolmus claim renew'da 'renewed' DONMEMELI — ayni transaction'daki lazy-expiry
    # onu dusurur -> 404 (eski kod expiry'yi ayri commit'liyordu; read-update arasi race).
    cid = (await _acquire(client, "claude-server:renew-race", device="surer")).json()["id"]
    con = sqlite3.connect(_claims_db)
    con.execute("UPDATE active_claims SET expires_at=datetime('now','-1 minutes') WHERE id=?", (cid,))
    con.commit()
    con.close()
    r = await client.put(f"/api/v1/memory/claims/{cid}/renew?ttl_hours=4", headers=_hdr())
    assert r.status_code == 404
    # DB'de satir inactive kaldi (sahte-renew ile canlanmadi)
    con = sqlite3.connect(_claims_db)
    active = con.execute("SELECT active FROM active_claims WHERE id=?", (cid,)).fetchone()[0]
    con.close()
    assert active == 0


@pytest.mark.usefixtures("_claims_db")
async def test_admin_key_can_release_claim(client, monkeypatch):
    # Codex#303-3tur: admin-key sahiplik kacis-kapisina dahil — admin kendi actigi
    # (dispatch_origin admin'de '' -> body-device) claim'i release/renew EDEBILMELI
    from app.api import memory as mem_module

    monkeypatch.setattr(mem_module, "MEMORY_API_KEY_ADMIN", "admin-secret-claims")
    cid = (await _acquire(client, "claude-server:admin-claim", device="turgut", key="admin-secret-claims")).json()["id"]
    r = await client.put(f"/api/v1/memory/claims/{cid}/renew?ttl_hours=2", headers=_hdr("admin-secret-claims"))
    assert r.status_code == 200
    r = await client.put(f"/api/v1/memory/claims/{cid}/release", headers=_hdr("admin-secret-claims"))
    assert r.status_code == 200


async def test_release_expired_claim_404(client, _claims_db):  # noqa: PT019 (deger sqlite3.connect icin kullanilir)
    # release da ayni atomik desene alindi (renew ile ayni sinif, proaktif)
    cid = (await _acquire(client, "claude-server:release-race", device="surer")).json()["id"]
    con = sqlite3.connect(_claims_db)
    con.execute("UPDATE active_claims SET expires_at=datetime('now','-1 minutes') WHERE id=?", (cid,))
    con.commit()
    con.close()
    r = await client.put(f"/api/v1/memory/claims/{cid}/release", headers=_hdr())
    assert r.status_code == 404
