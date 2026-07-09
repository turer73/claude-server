"""Tests for memory POST endpoint enhancements:
- Privacy filter (app.core.privacy.redact) applied to user-content fields
- 5-min exact-match dedup window — agentmemory pattern adaptation

memory_db fixture conftest'te degil test_memory_api.py icinde tanimli; ayni
fixture'i import edip kullaniyoruz.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import TEST_MEMORY_KEY


@pytest.fixture(autouse=True)
def _memory_client(client):
    client.headers["X-Memory-Key"] = TEST_MEMORY_KEY


import pytest

# memory_db fixture'i ayni dosyadan al
from tests.test_memory_api import memory_db  # noqa: F401

# ───────────────────────── privacy filter ─────────────────────────


@pytest.mark.anyio
async def test_create_memory_redacts_secret_in_content(client, memory_db):
    """memories.content icinde GH PAT varsa redact edilmis sekilde kaydedilir."""
    token = "ghp_" + "a" * 36
    resp = await client.post(
        "/api/v1/memory/memories",
        json={
            "type": "reference",
            "name": "test-redact-content",
            "description": "aciklama",
            "content": f"github key: {token}",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "created"
    assert "github_pat_classic" in body["secrets_redacted"]

    # DB'ye redact edilmis hali yazilmis mi
    conn = sqlite3.connect(str(memory_db))
    stored = conn.execute("SELECT content FROM memories WHERE id=?", (body["id"],)).fetchone()[0]
    conn.close()
    assert token not in stored
    assert "[REDACTED:github_pat_classic]" in stored


@pytest.mark.anyio
async def test_create_discovery_redacts_secret_in_details(client, memory_db):
    """discoveries.details icinde AWS key varsa redact."""
    token = "AKIA" + "Q" * 16
    resp = await client.post(
        "/api/v1/memory/discoveries",
        json={
            "project": "x",
            "type": "bug",
            "title": "test-redact-discovery",
            "details": f"export AWS_ACCESS_KEY={token}",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "aws_access_key" in body["secrets_redacted"]
    conn = sqlite3.connect(str(memory_db))
    stored = conn.execute("SELECT details FROM discoveries WHERE id=?", (body["id"],)).fetchone()[0]
    conn.close()
    assert token not in stored
    assert "[REDACTED:aws_access_key]" in stored


@pytest.mark.anyio
async def test_create_note_redacts_secret_in_content(client, memory_db):
    """notes.content secret redact + secrets_redacted alani."""
    token = "AIza" + "x" * 35
    resp = await client.post(
        "/api/v1/memory/notes",
        json={"from_device": "klipper", "title": "key paylasimi", "content": f"buyrun: {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "google_api" in body["secrets_redacted"]


@pytest.mark.anyio
async def test_create_memory_no_secret_returns_empty_redacted_list(client, memory_db):
    """Secret yoksa secrets_redacted = []."""
    resp = await client.post(
        "/api/v1/memory/memories",
        json={"type": "reference", "name": "temiz", "description": "yok", "content": "icerik"},
    )
    assert resp.json()["secrets_redacted"] == []


# ───────────────────────── 5-dakika dedup window ─────────────────────────


@pytest.mark.anyio
async def test_discovery_5min_dedup_skips_identical(client, memory_db):
    """Ayni project+type+title+details 5dk icinde tekrar gelirse skip."""
    payload = {
        "project": "x",
        "type": "learning",
        "title": "dup-test",
        "details": "tam ayni icerik",
    }
    r1 = await client.post("/api/v1/memory/discoveries", json=payload)
    assert r1.json()["status"] == "created"
    first_id = r1.json()["id"]

    r2 = await client.post("/api/v1/memory/discoveries", json=payload)
    assert r2.json()["status"] == "duplicate_skipped_5min"
    assert r2.json()["id"] == first_id


@pytest.mark.anyio
async def test_discovery_dedup_different_details_falls_through_to_upsert(client, memory_db):
    """Title ayni ama details farkli — dedup skip ETMEZ, mevcut upsert
    davranisi tetiklenir (already_exists)."""
    base = {"project": "x", "type": "bug", "title": "evolve", "details": "ilk hal"}
    r1 = await client.post("/api/v1/memory/discoveries", json=base)
    assert r1.json()["status"] == "created"

    base["details"] = "guncellenmis details"
    r2 = await client.post("/api/v1/memory/discoveries", json=base)
    assert r2.json()["status"] == "already_exists"  # upsert path
    assert r2.json()["id"] == r1.json()["id"]


@pytest.mark.anyio
async def test_memory_5min_dedup_skips_identical(client, memory_db):
    payload = {
        "type": "reference",
        "name": "mem-dup",
        "description": "ayni aciklama",
        "content": "ayni icerik",
    }
    r1 = await client.post("/api/v1/memory/memories", json=payload)
    r2 = await client.post("/api/v1/memory/memories", json=payload)
    assert r2.json()["status"] == "duplicate_skipped_5min"
    assert r2.json()["id"] == r1.json()["id"]


@pytest.mark.anyio
async def test_note_5min_dedup_skips_identical(client, memory_db):
    payload = {"from_device": "klipper", "title": "dup-note", "content": "ayni mesaj"}
    r1 = await client.post("/api/v1/memory/notes", json=payload)
    r2 = await client.post("/api/v1/memory/notes", json=payload)
    assert r2.json()["status"] == "duplicate_skipped_5min"
    assert r2.json()["id"] == r1.json()["id"]


@pytest.mark.anyio
async def test_dedup_respects_window_age(client, memory_db):
    """6 dakika once eklenmis kayitla yeni POST dedup ETMEZ — yeni record olur.

    SQLite created_at sutununu direkt elle eski tarihe set ederek 5dk sinirini
    asiyoruz; 'datetime now -5 minutes' window'unun disinda kalmali.
    """
    payload = {
        "project": "x",
        "type": "learning",
        "title": "yashli",
        "details": "icerik",
    }
    r1 = await client.post("/api/v1/memory/discoveries", json=payload)
    first_id = r1.json()["id"]

    # Time-machine: created_at'i 6 dk eskiyt + status='completed'
    # (status active ise upsert path yakalar, biz dedup'i test ediyoruz)
    conn = sqlite3.connect(str(memory_db))
    conn.execute(
        "UPDATE discoveries SET created_at=datetime('now','-6 minutes'), status='completed' WHERE id=?",
        (first_id,),
    )
    conn.commit()
    conn.close()

    r2 = await client.post("/api/v1/memory/discoveries", json=payload)
    assert r2.json()["status"] == "created"  # yeni row, eskiden olan etkiliyemedi
    assert r2.json()["id"] != first_id


# ───────────────────────── GAP-1 Kapsam-2 dispatch fail-open ─────────────────────────


@pytest.mark.anyio
async def test_create_note_dispatch_scan_failopen(client, memory_db, monkeypatch):
    """GAP-1 Kapsam-2: dispatch-scan cokerse bile not YINE INSERT edilir (fail-open)."""

    def _boom(*_a, **_k):
        raise RuntimeError("dispatch scan patladi")

    monkeypatch.setattr("app.api.memory.notes.scan_dispatch_note", _boom)
    resp = await client.post(
        "/api/v1/memory/notes",
        json={
            "from_device": "klipper",
            "to_device": "surer",
            "title": "dispatch-failopen-test",
            "content": '{"gorev_id":"X","adimlar":["is yap"]}',
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "created"


@pytest.mark.anyio
async def test_create_note_scanned_even_without_to_device(client, memory_db, monkeypatch):
    """Codex #1: dispatcher to_device SET ETMEZ -> to_device'siz not de scan_dispatch_note'a verilir."""
    calls = {"n": 0}

    def _spy(*_a, **_k):
        calls["n"] += 1
        return {"suspicious": False, "signals": [], "detail": {}}

    monkeypatch.setattr("app.api.memory.notes.scan_dispatch_note", _spy)
    resp = await client.post(
        "/api/v1/memory/notes",
        json={"from_device": "klipper", "title": "dispatcher", "content": '{"gorev":"x","alici":"surer-sonnet"}'},
    )
    assert resp.status_code == 200
    assert calls["n"] == 1  # to_device olmasa da taranir (dispatcher-notu kacmasin)


# ───────────────────────── GAP-1 item-D: otonom-key origin-tag HARD-enforce ─────────────────────────


@pytest.mark.anyio
async def test_create_note_autonomous_key_forces_from_device(client, memory_db, monkeypatch):
    """item-D: otonom-key ile auth -> from_device 'klipper-autonomous'a ZORLA-override (body-claim gozardi)."""
    import app.api.memory as mem_module

    monkeypatch.setattr(mem_module, "MEMORY_API_KEY_AUTONOMOUS", "auto-test-key-xyz")
    resp = await client.post(
        "/api/v1/memory/notes",
        headers={"X-Memory-Key": "auto-test-key-xyz"},
        json={
            "from_device": "klipper",  # body yalan-soyluyor; override edilmeli
            "to_device": "surer",
            "title": "otonom-dispatch",
            "content": '{"gorev":"is yap","alici":"surer-sonnet"}',
        },
    )
    assert resp.status_code == 200
    nid = resp.json()["id"]
    conn = sqlite3.connect(str(memory_db))
    row = conn.execute("SELECT from_device FROM notes WHERE id=?", (nid,)).fetchone()
    conn.close()
    assert row[0] == "klipper-autonomous"  # unforgeable server-side override


@pytest.mark.anyio
async def test_create_note_normal_key_preserves_from_device(client, memory_db, monkeypatch):
    """Normal-key (default header) -> body from_device KORUNUR (override yok)."""
    import app.api.memory as mem_module

    monkeypatch.setattr(mem_module, "MEMORY_API_KEY_AUTONOMOUS", "auto-test-key-xyz")
    resp = await client.post(
        "/api/v1/memory/notes",
        json={"from_device": "surer", "to_device": "klipper", "title": "normal-not", "content": "duz-prose"},
    )
    assert resp.status_code == 200
    nid = resp.json()["id"]
    conn = sqlite3.connect(str(memory_db))
    row = conn.execute("SELECT from_device FROM notes WHERE id=?", (nid,)).fetchone()
    conn.close()
    assert row[0] == "surer"


@pytest.mark.anyio
async def test_autonomous_key_dormant_when_unset(client, memory_db, monkeypatch):
    """MEMORY_API_KEY_AUTONOMOUS set-degilse: bos-key otonom sayilmaz, normal-akis (dormant)."""
    import app.api.memory as mem_module

    monkeypatch.setattr(mem_module, "MEMORY_API_KEY_AUTONOMOUS", None)
    resp = await client.post(
        "/api/v1/memory/notes",
        json={"from_device": "klipper", "to_device": "surer", "title": "dormant", "content": "x"},
    )
    assert resp.status_code == 200
    nid = resp.json()["id"]
    conn = sqlite3.connect(str(memory_db))
    row = conn.execute("SELECT from_device FROM notes WHERE id=?", (nid,)).fetchone()
    conn.close()
    assert row[0] == "klipper"


@pytest.mark.anyio
async def test_onboarding_rejects_autonomous_key(client, memory_db, monkeypatch):
    """Codex Tier-1 #1: otonom-key ONBOARDING'e erisemez (master-key sizdiriyor -> force-tag bypass)."""
    import app.api.memory as mem_module

    monkeypatch.setattr(mem_module, "MEMORY_API_KEY_AUTONOMOUS", "auto-test-key-xyz")
    # otonom-key ile onboarding -> 401 (master-only)
    resp = await client.get("/api/v1/memory/onboard/surer", headers={"X-Memory-Key": "auto-test-key-xyz"})
    assert resp.status_code == 401
    # master-key (default header) auth'u GECER (cihaz-kayitli-degilse 404 ama 401-DEGIL = auth-ok)
    resp2 = await client.get("/api/v1/memory/onboard/surer")
    assert resp2.status_code != 401


@pytest.mark.anyio
async def test_key_collision_disables_autonomous_mode(client, memory_db, monkeypatch):
    """Codex Tier-1 #2: otonom-key == master (config-hatasi) -> otonom-mod DORMANT (fail-closed)."""
    import app.api.memory as mem_module
    from tests.conftest import TEST_MEMORY_KEY

    monkeypatch.setattr(mem_module, "MEMORY_API_KEY_AUTONOMOUS", TEST_MEMORY_KEY)
    resp = await client.post(  # normal master-key (default) POST
        "/api/v1/memory/notes",
        json={"from_device": "surer", "to_device": "klipper", "title": "collision", "content": "x"},
    )
    assert resp.status_code == 200
    nid = resp.json()["id"]
    conn = sqlite3.connect(str(memory_db))
    row = conn.execute("SELECT from_device FROM notes WHERE id=?", (nid,)).fetchone()
    conn.close()
    assert row[0] == "surer"  # collision-guard: force-tag YOK
