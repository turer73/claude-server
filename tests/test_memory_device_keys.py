"""P0 kimlik — per-device API-key: from_device sunucu-tarafinda KEY'den turetilir.

Konu-1 karari (Turgut onayi, #100557): #100526 kimlik-karismasi sinifinin yapisal cozumu.
Otonom-key deseninin (GAP-1 A-2 unforgeable) tum cihazlara genellenmesi test edilir:
mint/rotate/revoke (master-only) + create_note override + verified etiketi.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import TEST_MEMORY_KEY
from tests.test_memory_api import memory_db  # noqa: F401 (fixture)


@pytest.fixture(autouse=True)
def _reset_ensure_flags(monkeypatch):
    # Global idempotent-migration flag'leri per-test tmp-DB'de yeniden calissin
    from app.api import memory as mem_module

    monkeypatch.setattr(mem_module, "_device_keys_ready", False)
    monkeypatch.setattr(mem_module, "_verified_ready", False)


async def _mint(client, name, key=TEST_MEMORY_KEY):
    return await client.post(f"/api/v1/memory/devices/{name}/key", headers={"X-Memory-Key": key})


async def test_mint_key_then_note_identity_forced(client, memory_db):  # noqa: F811
    resp = await _mint(client, "surer")
    assert resp.status_code == 200
    surer_key = resp.json()["key"]
    assert surer_key != TEST_MEMORY_KEY

    # Sahte from_device iddiasiyla not at — sunucu KEY'den turetmeli (#100526 senaryosu)
    resp = await client.post(
        "/api/v1/memory/notes",
        json={"from_device": "klipper", "title": "kimlik testi", "content": "spoof denemesi"},
        headers={"X-Memory-Key": surer_key},
    )
    assert resp.status_code == 200
    note_id = resp.json()["id"]
    con = sqlite3.connect(memory_db)
    row = con.execute("SELECT from_device, verified FROM notes WHERE id=?", (note_id,)).fetchone()
    con.close()
    assert row == ("surer", 1)  # body-iddiasi (klipper) DEGIL, key-kimligi (surer) + verified


async def test_master_key_note_stays_unverified(client, memory_db):  # noqa: F811
    resp = await client.post(
        "/api/v1/memory/notes",
        json={"from_device": "klipper", "title": "legacy yol", "content": "master-key yazimi"},
        headers={"X-Memory-Key": TEST_MEMORY_KEY},
    )
    assert resp.status_code == 200
    con = sqlite3.connect(memory_db)
    row = con.execute("SELECT from_device, verified FROM notes WHERE id=?", (resp.json()["id"],)).fetchone()
    con.close()
    assert row == ("klipper", 0)  # geriye-uyum: body korunur ama durustce unverified


async def test_rotate_invalidates_old_key(client, memory_db):  # noqa: F811
    old_key = (await _mint(client, "surer")).json()["key"]
    new_key = (await _mint(client, "surer")).json()["key"]
    assert old_key != new_key
    # /devices allowlist-disi (Codex#302-4tur: unscoped-global) -> probe icin allowlist'te
    # kalan /notes kullan (rotate/revoke semantigi test edilen, route'un kendisi degil)
    ok = await client.get("/api/v1/memory/notes", headers={"X-Memory-Key": new_key})
    assert ok.status_code == 200
    stale = await client.get("/api/v1/memory/notes", headers={"X-Memory-Key": old_key})
    assert stale.status_code == 401  # rotate = eski key aninda gecersiz


async def test_revoke_key(client, memory_db):  # noqa: F811
    key = (await _mint(client, "opencode")).json()["key"]
    resp = await client.delete("/api/v1/memory/devices/opencode/key", headers={"X-Memory-Key": TEST_MEMORY_KEY})
    assert resp.json()["revoked"] is True
    assert (await client.get("/api/v1/memory/devices", headers={"X-Memory-Key": key})).status_code == 401


async def test_mint_requires_master_key(client, memory_db):  # noqa: F811
    # Device-key mint EDEMEZ (onboarding-leak dersi: key-uretimi yalniz master/admin).
    # 3.tur default-deny sonrasi red allowlist-katmanindan gelir (403); verify_admin_key
    # ikinci savunma-hatti olarak durur.
    dev_key = (await _mint(client, "surer")).json()["key"]
    resp = await _mint(client, "hacker-device", key=dev_key)
    assert resp.status_code == 403


async def test_admin_key_active_master_cannot_mint(client, memory_db, monkeypatch):  # noqa: F811
    # Codex#302-P1: admin-key set+distinct -> mint/revoke YALNIZ admin. Master (herkesin
    # gunluk-credential'i) key-idaresi yapamaz -> bir ajan digerinin key'ini rotate edemez.
    from app.api import memory as mem_module

    monkeypatch.setattr(mem_module, "MEMORY_API_KEY_ADMIN", "admin-secret-distinct")
    # master ile mint REDDEDILIR
    assert (await _mint(client, "surer", key=TEST_MEMORY_KEY)).status_code == 401
    # admin ile mint GECER
    r = await _mint(client, "surer", key="admin-secret-distinct")
    assert r.status_code == 200
    # admin==master (config-hatasi) -> dormant, master calisir (collision-guard)
    monkeypatch.setattr(mem_module, "MEMORY_API_KEY_ADMIN", TEST_MEMORY_KEY)
    assert (await _mint(client, "klipper", key=TEST_MEMORY_KEY)).status_code == 200


async def test_revoked_device_key_fails_closed_not_master_legacy(client, memory_db):  # noqa: F811
    # Codex#302-P2: revoke edilmis device-key ile not — master-legacy'ye DUSUP body-spoof
    # KABUL ETMEMELI; 401 (fail-closed). verify_key zaten 401 verir; regresyon-kilidi.
    key = (await _mint(client, "opencode")).json()["key"]
    await client.delete("/api/v1/memory/devices/opencode/key", headers={"X-Memory-Key": TEST_MEMORY_KEY})
    resp = await client.post(
        "/api/v1/memory/notes",
        json={"from_device": "klipper", "title": "revoke sonrasi spoof", "content": "x"},
        headers={"X-Memory-Key": key},
    )
    assert resp.status_code == 401  # master-legacy'ye dusmedi


async def test_device_key_scoped_out_of_memory_router(client, memory_db):  # noqa: F811
    # Codex#302-2tur #3 (Turgut karari, route-bazli scope): device-key SADECE /memory/*
    # acar. verify_key'i import eden dis endpointler (dispatch/research/rag/classifier/
    # prometheus/ws_status) device-key'i REDDEDER — notes-koordinasyon key'i gercek-aksiyon
    # tetikleyememeli.
    dev_key = (await _mint(client, "surer")).json()["key"]
    # memory router: GECER (scoped dependency) -- /devices allowlist-disi (unscoped-global,
    # Codex#302-4tur), /notes allowlist-ICI kaldigi icin burada onu kullaniyoruz
    assert (await client.get("/api/v1/memory/notes", headers={"X-Memory-Key": dev_key})).status_code == 200
    # verify_key'li dis endpoint: 401
    assert (await client.get("/api/v1/ws/status", headers={"X-Memory-Key": dev_key})).status_code == 401
    # ayni endpoint master ile GECER (davranis-korunumu)
    assert (await client.get("/api/v1/ws/status", headers={"X-Memory-Key": TEST_MEMORY_KEY})).status_code == 200


async def test_admin_equals_autonomous_collision_dormant(client, memory_db, monkeypatch):  # noqa: F811
    # Codex#302-2tur #4: ADMIN==AUTONOMOUS config-hatasi -> admin DORMANT. Otonom-surec
    # (insan degil) mint/rotate/revoke YAPAMAZ; dormant'ta master gecis-deseniyle calisir
    # (master-collision davranisiyla tutarli — mint tamamen kilitlenmez).
    from app.api import memory as mem_module

    monkeypatch.setattr(mem_module, "MEMORY_API_KEY_AUTONOMOUS", "auto-secret-x")
    monkeypatch.setattr(mem_module, "MEMORY_API_KEY_ADMIN", "auto-secret-x")
    # otonom-key (admin'le ayni string) key-idaresi YAPAMAZ
    assert (await _mint(client, "surer", key="auto-secret-x")).status_code == 401
    # dormant -> master mint edebilir
    assert (await _mint(client, "surer", key=TEST_MEMORY_KEY)).status_code == 200


async def test_dedup_unverified_does_not_bury_verified_write(client, memory_db):  # noqa: F811
    # Codex#302-2tur #5: master-key (unverified) ayni icerigi ONCE yazarsa, gercek cihazin
    # device-key'li (verified) yazimi dedup'a takilip GOMULMEMELI; tersi yon bloklanir.
    dev_key = (await _mint(client, "surer")).json()["key"]
    body = {"from_device": "surer", "title": "ayni baslik", "content": "ayni icerik"}
    r1 = await client.post("/api/v1/memory/notes", json=body, headers={"X-Memory-Key": TEST_MEMORY_KEY})
    assert r1.json()["status"] == "created"
    # verified yazim: unverified satir onu BLOKLAYAMAZ
    r2 = await client.post("/api/v1/memory/notes", json=body, headers={"X-Memory-Key": dev_key})
    assert r2.json()["status"] == "created"
    assert r2.json()["id"] != r1.json()["id"]
    # unverified duplicate: verified satir varken BLOKLANIR (verified>=0 esles)
    r3 = await client.post("/api/v1/memory/notes", json=body, headers={"X-Memory-Key": TEST_MEMORY_KEY})
    assert r3.json()["status"] in ("duplicate_skipped_5min", "duplicate_title_30s")


async def test_device_key_default_deny_allowlist(client, memory_db):  # noqa: F811
    # Codex#302-3tur #3 (klipper default-deny mimari-onerisi): device-key allowlist-DISI
    # route'larda 403 — maintenance/webhooks gibi admin-islevleri device-key'le ACILAMAZ.
    dev_key = (await _mint(client, "surer")).json()["key"]
    assert (await client.post("/api/v1/memory/maintenance/archive-stale", headers={"X-Memory-Key": dev_key})).status_code == 403
    r_wh = await client.post("/api/v1/memory/webhooks", json={"url": "http://x/evil"}, headers={"X-Memory-Key": dev_key})
    assert r_wh.status_code == 403
    assert (await client.get("/api/v1/memory/webhooks/telegram-status", headers={"X-Memory-Key": dev_key})).status_code == 403
    assert (await client.post("/api/v1/memory/devices", json={"name": "sahte"}, headers={"X-Memory-Key": dev_key})).status_code == 403
    # master ayni route'larda calismaya devam eder (davranis-korunumu)
    assert (await client.get("/api/v1/memory/webhooks/telegram-status", headers={"X-Memory-Key": TEST_MEMORY_KEY})).status_code == 200
    # Codex#302-4tur (CANLI-BULGU): search/dashboard/devices/device-projects/projects/discoveries
    # GET'i unscoped-global sorgu -- TUM cihazlarin verisini donduruyordu, allowlist'ten CIKARILDI
    assert (await client.get("/api/v1/memory/search?q=test", headers={"X-Memory-Key": dev_key})).status_code == 403
    assert (await client.get("/api/v1/memory/dashboard", headers={"X-Memory-Key": dev_key})).status_code == 403
    assert (await client.get("/api/v1/memory/devices", headers={"X-Memory-Key": dev_key})).status_code == 403
    assert (await client.get("/api/v1/memory/device-projects", headers={"X-Memory-Key": dev_key})).status_code == 403
    assert (await client.get("/api/v1/memory/projects", headers={"X-Memory-Key": dev_key})).status_code == 403
    assert (await client.get("/api/v1/memory/discoveries", headers={"X-Memory-Key": dev_key})).status_code == 403
    # allowlist-ICI route device-key ile calisir
    assert (await client.get("/api/v1/memory/notes", headers={"X-Memory-Key": dev_key})).status_code == 200


async def test_mark_read_identity_forced_from_key(client, memory_db):  # noqa: F811
    # Codex#302-3tur #2: device-key auth'ta ?device= iddiasi EZILIR — surer, klipper'in
    # notunu 'klipper okudu' yapamaz; global-read=1 de atamaz (#647 per-device garantisi).
    dev_key = (await _mint(client, "surer")).json()["key"]
    note_id = (
        await client.post(
            "/api/v1/memory/notes",
            json={"from_device": "klipper", "title": "okuma testi", "content": "x"},
            headers={"X-Memory-Key": TEST_MEMORY_KEY},
        )
    ).json()["id"]
    r = await client.put(f"/api/v1/memory/notes/{note_id}/read?device=klipper", headers={"X-Memory-Key": dev_key})
    assert r.json()["device"] == "surer"  # iddia degil, key-kimligi
    assert r.json()["read_by"] == ["surer"]
    # device-parametresiz device-key cagrisi da global-read'e DUSEMEZ (forced per-device)
    r = await client.put(f"/api/v1/memory/notes/{note_id}/read", headers={"X-Memory-Key": dev_key})
    assert r.json()["device"] == "surer"
    con = sqlite3.connect(memory_db)
    row = con.execute("SELECT read, read_by FROM notes WHERE id=?", (note_id,)).fetchone()
    con.close()
    assert row[0] == 0  # global read set edilmedi
    assert "klipper" not in (row[1] or "")


async def test_memories_source_device_forced_from_key(client, memory_db):  # noqa: F811
    # Codex#302-3tur #4: POST /memories source_device caller-iddiasiydi — key'den zorlanir
    dev_key = (await _mint(client, "surer")).json()["key"]
    r = await client.post(
        "/api/v1/memory/memories",
        json={"type": "project", "name": "kimlik-testi", "description": "d", "content": "c", "source_device": "klipper"},
        headers={"X-Memory-Key": dev_key},
    )
    assert r.status_code == 200
    con = sqlite3.connect(memory_db)
    sd = con.execute("SELECT source_device FROM memories WHERE id=?", (r.json()["id"],)).fetchone()[0]
    con.close()
    assert sd == "surer"


async def test_create_discovery_direct_call_without_di(client, memory_db):  # noqa: F811
    # Codex#302-3tur #1: main.py boot dead-gate-emit create_discovery'yi DI-DISI dogrudan
    # cagirir — forced_origin Depends-sentinel'i (truthy obje) kimlik sanilirsa SQL bind
    # patlar ve boot-discovery sessiz kaybolurdu. Dogrudan cagri calismali.
    from app.api.memory import DiscoveryCreate
    from app.api.memory.discoveries import create_discovery

    r = await create_discovery(
        DiscoveryCreate(project="claude-server", type="bug", title="dead-gate direct-call regresyon testi", details="di-disi cagri")
    )
    assert isinstance(r, dict)
    assert r.get("id")
    con = sqlite3.connect(memory_db)
    dn = con.execute("SELECT device_name FROM discoveries WHERE id=?", (r["id"],)).fetchone()[0]
    con.close()
    assert dn == "klipper"  # DiscoveryCreate default'u — sentinel DEGIL


async def test_sessions_tasks_discoveries_forced_origin(client, memory_db):  # noqa: F811
    # Codex#302-2tur #1: forced-origin genellemesi — sessions/tasks/discoveries yazimlari da
    # kimligi KEY'den turetir (body-iddiasi ezilir; create_note deseni).
    import sqlite3 as _sq

    dev_key = (await _mint(client, "surer")).json()["key"]
    r = await client.post(
        "/api/v1/memory/sessions",
        json={"device_name": "klipper", "summary": "spoof denemesi"},
        headers={"X-Memory-Key": dev_key},
    )
    assert r.status_code == 200
    r = await client.post(
        "/api/v1/memory/tasks",
        json={"device_name": "klipper", "project": "test-proj", "task": "spoof task"},
        headers={"X-Memory-Key": dev_key},
    )
    assert r.status_code == 200
    r = await client.post(
        "/api/v1/memory/discoveries",
        json={"device_name": "klipper", "project": "test-proj", "type": "bug", "title": "spoof disc", "details": "d"},
        headers={"X-Memory-Key": dev_key},
    )
    assert r.status_code == 200
    con = _sq.connect(memory_db)
    assert con.execute("SELECT device_name FROM sessions ORDER BY id DESC LIMIT 1").fetchone()[0] == "surer"
    assert con.execute("SELECT device_name FROM tasks_log ORDER BY id DESC LIMIT 1").fetchone()[0] == "surer"
    assert con.execute("SELECT device_name FROM discoveries ORDER BY id DESC LIMIT 1").fetchone()[0] == "surer"
    con.close()
