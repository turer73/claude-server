"""Tartışma platformu MVP — kör-tur API-zorlaması + şablon + sentez/karar akışı.

Konu-2 sentezi (#100561, Turgut onaylı): konvansiyonel kör-tur bugün 4 kez delindi →
görünürlük-gate'i API'de. Bu testler o garantinin regresyon-kilidi.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import TEST_MEMORY_KEY
from tests.test_memory_api import memory_db  # noqa: F401 (fixture)

@pytest.fixture(autouse=True)
def discussions_db(memory_db, monkeypatch):  # noqa: F811
    from app.api import memory as mem_module
    from app.api.memory import discussions as disc_module

    monkeypatch.setattr(disc_module, "_discussions_ready", False)
    monkeypatch.setattr(mem_module, "_device_keys_ready", False)
    return memory_db


def _hdr(key=TEST_MEMORY_KEY):
    return {"X-Memory-Key": key}


_TPL = {
    "position": "memory-API uzantisi dogru yaklasim",
    "evidence": "mevcut altyapi yeniden kullanilir, ayri sistem bakim-yuku",
    "confidence": 7,
    "persuadable_by": "auth-modelinin yetersiz kaldigini gosteren somut deneme",
    "objection": "kullanilmayan-forum riski olculmedi",
}


async def _new_topic(client, expected="surer,klipper"):
    r = await client.post(
        "/api/v1/memory/discussions",
        json={"title": "Test konusu basligi", "question": "Bu tasarim dogru mu?", "device": "turgut", "expected_devices": expected},
        headers=_hdr(),
    )
    assert r.status_code == 200
    return r.json()["id"]


async def _pos(client, tid, device, key=TEST_MEMORY_KEY, **over):
    return await client.post(f"/api/v1/memory/discussions/{tid}/positions", json={"device": device, **_TPL, **over}, headers=_hdr(key))


async def test_blind_round_gate_enforced(client, discussions_db):
    # 4/4 yakinsamanin kalbi: open-fazda B, A'nin pozisyonunu GOREMEZ (API-zorlama)
    tid = await _new_topic(client)
    assert (await _pos(client, tid, "surer")).status_code == 200
    r = await client.get(f"/api/v1/memory/discussions/{tid}/positions?as_device=klipper", headers=_hdr())
    body = r.json()
    assert body["blind"] is True
    assert body["positions"] == []  # klipper, surer'inkini goremiyor
    r = await client.get(f"/api/v1/memory/discussions/{tid}/positions?as_device=surer", headers=_hdr())
    assert len(r.json()["positions"]) == 1  # kendi pozisyonunu goruyor


async def test_blind_opens_when_all_written(client, discussions_db):
    tid = await _new_topic(client, expected="surer,klipper")
    await _pos(client, tid, "surer")
    r = await _pos(client, tid, "klipper")
    assert r.json()["topic_status"] == "discussion"  # herkes yazdi -> otomatik acilim
    r = await client.get(f"/api/v1/memory/discussions/{tid}/positions?as_device=opencode", headers=_hdr())
    assert r.json()["blind"] is False
    assert len(r.json()["positions"]) == 2


async def test_blind_single_position_per_device(client, discussions_db):
    tid = await _new_topic(client)
    await _pos(client, tid, "surer")
    r = await _pos(client, tid, "surer", position="fikrimi degistirdim ama kor-turda")
    assert r.status_code == 409  # kor-turda tek pozisyon; duzeltme round-2'de


async def test_template_validation_rejects_lazy_fields(client, discussions_db):
    tid = await _new_topic(client)
    r = await _pos(client, tid, "surer", objection="yok")  # <10 karakter formalite-itiraz
    assert r.status_code == 422


async def test_deadline_lazy_opens_and_silence_is_abstain(client, discussions_db):
    tid = await _new_topic(client, expected="surer,klipper,opencode")
    await _pos(client, tid, "surer")
    con = sqlite3.connect(discussions_db)
    con.execute("UPDATE discussion_topics SET blind_deadline=datetime('now','-1 hours') WHERE id=?", (tid,))
    con.commit()
    con.close()
    r = await client.get(f"/api/v1/memory/discussions/{tid}/positions?as_device=klipper", headers=_hdr())
    assert r.json()["blind"] is False  # 24h doldu -> lazy acilim; yazmayanlar cekimser
    assert len(r.json()["positions"]) == 1


async def test_round2_appends_after_open(client, discussions_db):
    tid = await _new_topic(client, expected="surer")
    await _pos(client, tid, "surer")  # tek beklenen yazinca acilir
    r = await _pos(client, tid, "surer", position="round-2 revizyonu: fikrim evrildi")
    assert r.status_code == 200
    assert r.json()["round"] == 2  # append-only fikir-evrimi


async def test_synthesizer_cannot_be_creator(client, discussions_db):
    tid = await _new_topic(client, expected="surer")
    await _pos(client, tid, "surer")
    syn = {"synthesis": "Yakinsama: X ve Y. Ihtilaf: Z konusunda ayrisma.", "device": "turgut"}
    r = await client.post(f"/api/v1/memory/discussions/{tid}/synthesize", json=syn, headers=_hdr())
    assert r.status_code == 403  # konuyu turgut acti -> sentezleyemez
    syn["device"] = "klipper"
    r = await client.post(f"/api/v1/memory/discussions/{tid}/synthesize", json=syn, headers=_hdr())
    assert r.json()["status"] == "needs_turgut"


async def test_decide_master_only_and_finalizes(client, discussions_db):
    tid = await _new_topic(client, expected="surer")
    await _pos(client, tid, "surer")
    await client.post(
        f"/api/v1/memory/discussions/{tid}/synthesize",
        json={"synthesis": "Yakinsama: A. Ihtilaf: yok denecek kadar az.", "device": "klipper"},
        headers=_hdr(),
    )
    # Device-key karar VEREMEZ (esitlik soz-hakkinda, yetkide degil)
    dev_key = (await client.post("/api/v1/memory/devices/surer/key", headers=_hdr())).json()["key"]
    r = await client.post(f"/api/v1/memory/discussions/{tid}/decide", json={"decision": "Onaylandi"}, headers=_hdr(dev_key))
    assert r.status_code == 401
    r = await client.post(f"/api/v1/memory/discussions/{tid}/decide", json={"decision": "Onaylandi, uygulansin"}, headers=_hdr())
    assert r.json()["status"] == "decided"
    r = await _pos(client, tid, "klipper")
    assert r.status_code == 409  # kapali konuya pozisyon yok (append-only arsiv)


async def test_device_key_identity_wins_in_positions(client, discussions_db):
    # P0 entegrasyonu: device-key ile pozisyon -> kimlik KEY'den, verified=1
    tid = await _new_topic(client)
    surer_key = (await client.post("/api/v1/memory/devices/surer/key", headers=_hdr())).json()["key"]
    r = await _pos(client, tid, "klipper", key=surer_key)  # sahte iddia
    assert r.json()["device"] == "surer"
    assert r.json()["verified"] is True
