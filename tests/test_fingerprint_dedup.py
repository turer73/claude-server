"""F1 fingerprint-dedup (topic-4 kararı) testleri.

Tekrar-eden bulgu (AUTO-alert) TEK kanonik discovery'de toplanır: aynı fingerprint
açıkken yeni-satır AÇILMAZ (occurrence_count++), resolved olan fingerprint pencere-içi
tekrar tetiklenince AYNI kayıt reopen edilir. fingerprint=None → mevcut davranış AYNEN
korunur (güvenli-varsayılan).

memory_db fixture'i test_memory_api.py'den; dedup-privacy testiyle aynı desen.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import TEST_MEMORY_KEY
from tests.test_memory_api import memory_db  # noqa: F401


@pytest.fixture(autouse=True)
def _memory_client(client):
    client.headers["X-Memory-Key"] = TEST_MEMORY_KEY


def _col(db_path, did, col):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT {col} FROM discoveries WHERE id=?", (did,)).fetchone()[0]
    finally:
        conn.close()


# ───────────────────────── güvenli-varsayılan: fingerprint yok ─────────────────────────


@pytest.mark.anyio
async def test_no_fingerprint_behavior_unchanged(client, memory_db):
    """fingerprint verilmezse F1 bloğu ATLANIR — normal create + occurrence_count=1 default."""
    r = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "x", "type": "bug", "title": "no-fp", "details": "ilk"},
    )
    assert r.json()["status"] == "created"
    did = r.json()["id"]
    assert _col(memory_db, did, "fingerprint") is None
    assert _col(memory_db, did, "occurrence_count") == 1
    assert _col(memory_db, did, "first_seen") is not None
    assert _col(memory_db, did, "last_seen") is not None


# ───────────────────────── occurrence_incremented (aktif) ─────────────────────────


@pytest.mark.anyio
async def test_active_fingerprint_increments_occurrence(client, memory_db):
    """Aynı fingerprint aktifken 2. POST yeni-satır açmaz — occurrence_count++ + aynı id."""
    fp = "escalation:docker:n8n"
    r1 = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "linux-ai-server", "type": "bug", "title": f"AUTO-alert: {fp}", "details": "down (t1)", "fingerprint": fp},
    )
    assert r1.json()["status"] == "created"
    did = r1.json()["id"]
    assert _col(memory_db, did, "fingerprint") == fp

    # 2. tetik — details FARKLI (5dk exact-window'a takılmasın, gerçek alarm gibi)
    r2 = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "linux-ai-server", "type": "bug", "title": f"AUTO-alert: {fp}", "details": "down (t2)", "fingerprint": fp},
    )
    assert r2.json()["status"] == "occurrence_incremented"
    assert r2.json()["id"] == did
    assert _col(memory_db, did, "occurrence_count") == 2

    # 3. tetik → 3
    r3 = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "linux-ai-server", "type": "bug", "title": f"AUTO-alert: {fp}", "details": "down (t3)", "fingerprint": fp},
    )
    assert r3.json()["id"] == did
    assert _col(memory_db, did, "occurrence_count") == 3
    # Tek kanonik satır — hiç yeni-satır açılmadı
    conn = sqlite3.connect(str(memory_db))
    n = conn.execute("SELECT COUNT(*) FROM discoveries WHERE fingerprint=?", (fp,)).fetchone()[0]
    conn.close()
    assert n == 1


# ───────────────────────── reopened_fingerprint (resolved → tekrar) ─────────────────────────


@pytest.mark.anyio
async def test_resolved_fingerprint_reopens_same_row(client, memory_db):
    """Resolved fingerprint pencere-içi tekrar tetiklenince AYNI kayıt reopen — flap-collapse."""
    fp = "escalation:docker:dozzle"
    r1 = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "linux-ai-server", "type": "bug", "title": f"AUTO-alert: {fp}", "details": "down", "fingerprint": fp},
    )
    did = r1.json()["id"]

    # Slice-A resolve simülasyonu (notify-cron: status='completed', resolved=1)
    conn = sqlite3.connect(str(memory_db))
    conn.execute("UPDATE discoveries SET status='completed', resolved=1 WHERE id=?", (did,))
    conn.commit()
    conn.close()

    # Kaynak tekrar bozuldu → AYNI kayıt reopen (yeni-satır DEĞİL)
    r2 = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "linux-ai-server", "type": "bug", "title": f"AUTO-alert: {fp}", "details": "down yine", "fingerprint": fp},
    )
    assert r2.json()["status"] == "reopened_fingerprint"
    assert r2.json()["id"] == did
    assert _col(memory_db, did, "status") == "active"
    assert _col(memory_db, did, "resolved") == 0
    assert _col(memory_db, did, "occurrence_count") == 2


@pytest.mark.anyio
async def test_stale_resolved_fingerprint_opens_new_row(client, memory_db):
    """Pencereden ESKİ resolved fingerprint (uzun-sessiz) regression = TAZE satır."""
    fp = "escalation:docker:uptime-kuma"
    r1 = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "linux-ai-server", "type": "bug", "title": f"AUTO-alert: {fp}", "details": "down", "fingerprint": fp},
    )
    old_id = r1.json()["id"]

    # 40 gün önce resolved + tüm zaman-sinyalleri eski (pencere=30g dışı)
    conn = sqlite3.connect(str(memory_db))
    conn.execute(
        "UPDATE discoveries SET status='completed', resolved=1, "
        "created_at=datetime('now','-40 days'), valid_at=datetime('now','-40 days'), "
        "last_seen=datetime('now','-40 days'), invalid_at=NULL WHERE id=?",
        (old_id,),
    )
    conn.commit()
    conn.close()

    r2 = await client.post(
        "/api/v1/memory/discoveries",
        json={
            "project": "linux-ai-server",
            "type": "bug",
            "title": f"AUTO-alert: {fp}",
            "details": "aylar sonra yine",
            "fingerprint": fp,
        },
    )
    assert r2.json()["status"] == "created"
    assert r2.json()["id"] != old_id


# ───────────────────────── pre-migration NULL fingerprint eşleme ─────────────────────────


@pytest.mark.anyio
async def test_premigration_null_fingerprint_matched_by_title(client, memory_db):
    """fingerprint=NULL eski kayıt (migration-öncesi) title ile yakalanır + fingerprint backfill."""
    fp = "escalation:docker:grafana"
    title = f"AUTO-alert: {fp}"
    # fingerprint'siz oluştur (eski üretici gibi)
    r1 = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "linux-ai-server", "type": "bug", "title": title, "details": "down"},
    )
    did = r1.json()["id"]
    assert _col(memory_db, did, "fingerprint") is None

    # Şimdi fingerprint'li POST (yeni üretici) → NULL-satırı title ile yakala + backfill
    r2 = await client.post(
        "/api/v1/memory/discoveries",
        json={"project": "linux-ai-server", "type": "bug", "title": title, "details": "down t2", "fingerprint": fp},
    )
    assert r2.json()["status"] == "occurrence_incremented"
    assert r2.json()["id"] == did
    assert _col(memory_db, did, "fingerprint") == fp  # backfill oldu
    assert _col(memory_db, did, "occurrence_count") == 2
