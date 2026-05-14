"""Tests for memory POST endpoint enhancements:
- Privacy filter (app.core.privacy.redact) applied to user-content fields
- 5-min exact-match dedup window — agentmemory pattern adaptation

memory_db fixture conftest'te degil test_memory_api.py icinde tanimli; ayni
fixture'i import edip kullaniyoruz.
"""

from __future__ import annotations

import sqlite3

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
