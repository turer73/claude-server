"""Gundem Panosu (GET /api/v1/memory/agenda) — topic-3/P-D.

Salt-okunur agrega (discoveries/tasks_log/notes/sessions/devices) session-context'e
de gomulu. Regresyon-kilidi: master/admin/otonom-only (device-key testi
test_memory_device_keys.py::test_device_key_default_deny_allowlist icinde) +
devices.hostname ajan_saglik'ten hic donmez (Codex#302-4tur P0 sinifi).

PR#344 Codex-review (2026-07-19, 3×P2) fix'leri: open_claims yanlis tabloya
(claims/status) bakiyordu (gercegi active_claims/active) — hep-bos donerdi;
session-context'e gomulu agenda.notes device-filtresiz'di (baska cihaza ozel not
sizardi); notes.status kolonu _ensure_status cagrilmadan COALESCE'lenirdi (eski-DB 500).
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import TEST_MEMORY_KEY
from tests.test_memory_api import memory_db  # noqa: F401 (fixture)


@pytest.fixture(autouse=True)
def _reset_ensure_flags(monkeypatch):
    # Her test kendi tmp-DB'siyle basliyor ama _ensure_* modul-flag'leri global —
    # onceki testte "hazir" isaretlenirse burada no-op'a dusup yeni DB'de tablo/kolon
    # olusturulmaz (test_memory_claims.py::_claims_db ile ayni desen).
    from app.api import memory as mem_module
    from app.api.memory import claims as claims_module

    monkeypatch.setattr(mem_module, "_status_ready", False)
    monkeypatch.setattr(mem_module, "_thread_fields_ready", False)
    monkeypatch.setattr(claims_module, "_claims_ready", False)


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
    # active_claims tablosu henuz kurulmadan (hic claim acilmamis) — _ensure_claims
    # agenda basinda idempotent kurar, sorgu 0-satir doner, patlamaz.
    r = await client.get("/api/v1/memory/agenda", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["yapilacaklar"]["open_claims"] == []


async def test_agenda_surfaces_active_claims(client, memory_db):  # noqa: F811
    # Codex#344-P2: eski sorgu 'claims'/'status' (yanlis tablo/kolon) her zaman []
    # donduruyordu — gercek tablo active_claims/active. /claims ile gercek claim ac,
    # agenda'da gorunmeli.
    r = await client.post(
        "/api/v1/memory/claims",
        json={"task_key": "linux-ai-server:agenda-fix", "device": "klipper", "repo": "claude-server"},
        headers=_hdr(),
    )
    assert r.status_code == 200

    r = await client.get("/api/v1/memory/agenda", headers=_hdr())
    assert r.status_code == 200
    open_claims = r.json()["yapilacaklar"]["open_claims"]
    assert any(c["task_key"] == "linux-ai-server:agenda-fix" for c in open_claims)


async def test_session_context_agenda_notes_device_scoped(client, memory_db):  # noqa: F811
    # Codex#344-P2: embedded agenda.ne_oldu.notes device-filtresizdi — baska cihaza
    # ozel (to_device='surer') not klipper'in session-context'ine sizardi.
    await client.post(
        "/api/v1/memory/notes",
        json={"from_device": "surer", "to_device": "surer", "title": "sadece surer icin", "content": "gizli"},
        headers=_hdr(),
    )
    await client.post(
        "/api/v1/memory/notes",
        json={"from_device": "surer", "to_device": "klipper", "title": "klipper icin", "content": "acik"},
        headers=_hdr(),
    )
    con = sqlite3.connect(memory_db)
    con.execute("INSERT OR IGNORE INTO devices (name, platform) VALUES ('klipper','linux')")
    con.commit()
    con.close()

    r = await client.get("/api/v1/memory/onboard/klipper/session-context", headers=_hdr())
    assert r.status_code == 200
    note_titles = {n["title"] for n in r.json()["agenda"]["ne_oldu"]["notes"]}
    assert "klipper icin" in note_titles
    assert "sadece surer icin" not in note_titles
