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
    ok = await client.get("/api/v1/memory/devices", headers={"X-Memory-Key": new_key})
    assert ok.status_code == 200
    stale = await client.get("/api/v1/memory/devices", headers={"X-Memory-Key": old_key})
    assert stale.status_code == 401  # rotate = eski key aninda gecersiz


async def test_revoke_key(client, memory_db):  # noqa: F811
    key = (await _mint(client, "opencode")).json()["key"]
    resp = await client.delete("/api/v1/memory/devices/opencode/key", headers={"X-Memory-Key": TEST_MEMORY_KEY})
    assert resp.json()["revoked"] is True
    assert (await client.get("/api/v1/memory/devices", headers={"X-Memory-Key": key})).status_code == 401


async def test_mint_requires_master_key(client, memory_db):  # noqa: F811
    # Device-key mint EDEMEZ (onboarding-leak dersi: key-uretimi yalniz master/admin)
    dev_key = (await _mint(client, "surer")).json()["key"]
    resp = await _mint(client, "hacker-device", key=dev_key)
    assert resp.status_code == 401


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
