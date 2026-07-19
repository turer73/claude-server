"""Gundem Panosu (GET /api/v1/memory/agenda) — topic-3/P-D.

Salt-okunur agrega (discoveries/tasks_log/notes/sessions/devices) session-context'e
de gomulu. Regresyon-kilidi: master/admin/otonom-only (device-key testi
test_memory_device_keys.py::test_device_key_default_deny_allowlist icinde) +
devices.hostname ajan_saglik'ten hic donmez (Codex#302-4tur P0 sinifi).
"""

from __future__ import annotations

import sqlite3

from tests.conftest import TEST_MEMORY_KEY
from tests.test_memory_api import memory_db  # noqa: F401 (fixture)


def _hdr(key=TEST_MEMORY_KEY):
    return {"X-Memory-Key": key}


async def test_agenda_shape_and_sections(client, memory_db):  # noqa: F811
    con = sqlite3.connect(memory_db)
    con.execute(
        "INSERT INTO discoveries (project, type, title, device_name, status) VALUES (?,?,?,?,?)",
        ("linux-ai-server", "bug", "test bug", "klipper", "active"),
    )
    con.execute(
        "INSERT INTO devices (name, platform, hostname) VALUES (?,?,?)",
        ("klipper", "linux", "klipper-secret-hostname"),
    )
    con.commit()
    con.close()

    r = await client.get("/api/v1/memory/agenda", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    for key in ("ne_oldu", "yapilacaklar", "kontrol_edilecekler", "ajan_saglik"):
        assert key in body
    assert any(b["title"] == "test bug" for b in body["yapilacaklar"]["active_bugs"])

    # hostname hicbir ajan_saglik kaydinda donmemeli (unscoped-cross-device sizinti sinifi)
    assert "hostname" not in body["ajan_saglik"]["devices"][0]
    assert "klipper-secret-hostname" not in r.text


async def test_agenda_claims_table_missing_is_safe(client, memory_db):  # noqa: F811
    # test schema'sinda claims tablosu yok — _safe_claims exception'i yutup [] donmeli
    r = await client.get("/api/v1/memory/agenda", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["yapilacaklar"]["open_claims"] == []
