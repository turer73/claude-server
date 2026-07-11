"""Tartışma platformu MVP — kör-tur API-zorlaması + şablon + sentez/karar akışı.

Konu-2 sentezi (#100561, Turgut onaylı): konvansiyonel kör-tur bugün 4 kez delindi →
görünürlük-gate'i API'de. Bu testler o garantinin regresyon-kilidi.

Codex#305 2.tur sonrası (canlı-exploit doğrulamalı): kör-tur artık HER yolda kanıtlı-kimlik
ister — yazım device-key-only (squat-önlemi), görünürlük key-kimliğinden (as_device iddiası
yok sayılır), açılım verified=1 satırlarla, sentez/karar faz-sıralı.
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


async def _mint(client, name):
    return (await client.post(f"/api/v1/memory/devices/{name}/key", headers=_hdr())).json()["key"]


async def test_question_position_leak_rejected(client, discussions_db):
    # Canlı-bulgu #100609 (surer'in kendi kullanım-hatası): question/title'a katılımcı-atıflı
    # sıralama gömmek kör-turu deler (bu alanlar herkese açık). Tam-o-metin reddedilmeli.
    leak = "opencode sirasi: 4->3->5. surer sirasi: 4a->5->3."
    r = await client.post(
        "/api/v1/memory/discussions",
        json={"title": "Oncelik konusu", "question": leak, "device": "turgut"},
        headers=_hdr(),
    )
    assert r.status_code == 422  # kör-tur sızıntı reddi
    # title'da da: rakam-ok-dizisi
    r = await client.post(
        "/api/v1/memory/discussions",
        json={"title": "3->5 sirasi mi", "question": "Bu tasarim dogru mu?", "device": "turgut"},
        headers=_hdr(),
    )
    assert r.status_code == 422


async def test_neutral_question_with_device_name_allowed(client, discussions_db):
    # Defense-in-depth ama tek-cihaz meta-soruyu GEÇİRMELİ (atıflı-pozisyon değil, ':' yok, ok yok)
    r = await client.post(
        "/api/v1/memory/discussions",
        json={"title": "Klipper onerisi degerlendirmesi", "question": "klipper'in onerisi dogru mu, tartisalim?", "device": "turgut"},
        headers=_hdr(),
    )
    assert r.status_code == 200


async def _pos(client, tid, device, key=TEST_MEMORY_KEY, **over):
    return await client.post(f"/api/v1/memory/discussions/{tid}/positions", json={"device": device, **_TPL, **over}, headers=_hdr(key))


async def test_blind_round_gate_enforced(client, discussions_db):
    # 4/4 yakinsamanin kalbi: open-fazda B, A'nin pozisyonunu GOREMEZ (API-zorlama).
    # Kimlik device-key'den — kor-turda as_device iddiasi yok (Codex#305 #6).
    tid = await _new_topic(client)
    surer_key = await _mint(client, "surer")
    klipper_key = await _mint(client, "klipper")
    assert (await _pos(client, tid, "surer", key=surer_key)).status_code == 200
    r = await client.get(f"/api/v1/memory/discussions/{tid}/positions", headers=_hdr(klipper_key))
    body = r.json()
    assert body["blind"] is True
    assert body["positions"] == []  # klipper, surer'inkini goremiyor
    r = await client.get(f"/api/v1/memory/discussions/{tid}/positions", headers=_hdr(surer_key))
    assert len(r.json()["positions"]) == 1  # kendi pozisyonunu goruyor (key-kimligiyle)


async def test_blind_opens_when_all_written(client, discussions_db):
    tid = await _new_topic(client, expected="surer,klipper")
    surer_key = await _mint(client, "surer")
    klipper_key = await _mint(client, "klipper")
    await _pos(client, tid, "surer", key=surer_key)
    r = await _pos(client, tid, "klipper", key=klipper_key)
    assert r.json()["topic_status"] == "discussion"  # herkes (verified) yazdi -> otomatik acilim
    r = await client.get(f"/api/v1/memory/discussions/{tid}/positions?as_device=opencode", headers=_hdr())
    assert r.json()["blind"] is False
    assert len(r.json()["positions"]) == 2


async def test_blind_single_position_per_device(client, discussions_db):
    tid = await _new_topic(client)
    surer_key = await _mint(client, "surer")
    await _pos(client, tid, "surer", key=surer_key)
    r = await _pos(client, tid, "surer", key=surer_key, position="fikrimi degistirdim ama kor-turda")
    assert r.status_code == 409  # kor-turda tek pozisyon; duzeltme round-2'de


async def test_template_validation_rejects_lazy_fields(client, discussions_db):
    tid = await _new_topic(client)
    r = await _pos(client, tid, "surer", objection="yok")  # <10 karakter formalite-itiraz
    assert r.status_code == 422


async def test_deadline_lazy_opens_and_silence_is_abstain(client, discussions_db):
    tid = await _new_topic(client, expected="surer,klipper,opencode")
    surer_key = await _mint(client, "surer")
    await _pos(client, tid, "surer", key=surer_key)
    con = sqlite3.connect(discussions_db)
    con.execute("UPDATE discussion_topics SET blind_deadline=datetime('now','-1 hours') WHERE id=?", (tid,))
    con.commit()
    con.close()
    r = await client.get(f"/api/v1/memory/discussions/{tid}/positions?as_device=klipper", headers=_hdr())
    assert r.json()["blind"] is False  # 24h doldu -> lazy acilim; yazmayanlar cekimser
    assert len(r.json()["positions"]) == 1


async def test_round2_appends_after_open(client, discussions_db):
    tid = await _new_topic(client, expected="surer")
    surer_key = await _mint(client, "surer")
    await _pos(client, tid, "surer", key=surer_key)  # tek beklenen (verified) yazinca acilir
    r = await _pos(client, tid, "surer", key=surer_key, position="round-2 revizyonu: fikrim evrildi")
    assert r.status_code == 200
    assert r.json()["round"] == 2  # append-only fikir-evrimi


async def test_synthesizer_cannot_be_creator(client, discussions_db):
    tid = await _new_topic(client, expected="surer")
    surer_key = await _mint(client, "surer")
    await _pos(client, tid, "surer", key=surer_key)  # kor-tur acilir (verified)
    syn = {"synthesis": "Yakinsama: X ve Y. Ihtilaf: Z konusunda ayrisma.", "device": "turgut"}
    r = await client.post(f"/api/v1/memory/discussions/{tid}/synthesize", json=syn, headers=_hdr())
    assert r.status_code == 403  # konuyu turgut acti -> sentezleyemez
    syn["device"] = "klipper"
    r = await client.post(f"/api/v1/memory/discussions/{tid}/synthesize", json=syn, headers=_hdr())
    assert r.json()["status"] == "needs_turgut"


async def test_decide_master_only_and_finalizes(client, discussions_db):
    tid = await _new_topic(client, expected="surer")
    surer_key = await _mint(client, "surer")
    await _pos(client, tid, "surer", key=surer_key)
    await client.post(
        f"/api/v1/memory/discussions/{tid}/synthesize",
        json={"synthesis": "Yakinsama: A. Ihtilaf: yok denecek kadar az.", "device": "klipper"},
        headers=_hdr(),
    )
    # Device-key karar VEREMEZ (esitlik soz-hakkinda, yetkide degil). decide route-allowlist'te
    # DEGIL (PR#302-3tur default-deny) -> router-seviyesinde 403 ile daha erken reddedilir
    # (route'un kendi verify_master_key'ine hic ulasmaz — 401 degil 403, ama sonuc ayni: red).
    r = await client.post(f"/api/v1/memory/discussions/{tid}/decide", json={"decision": "Onaylandi"}, headers=_hdr(surer_key))
    assert r.status_code == 403
    r = await client.post(f"/api/v1/memory/discussions/{tid}/decide", json={"decision": "Onaylandi, uygulansin"}, headers=_hdr())
    assert r.json()["status"] == "decided"
    r = await _pos(client, tid, "klipper")
    assert r.status_code == 409  # kapali konuya pozisyon yok (append-only arsiv)


async def test_device_key_identity_wins_in_positions(client, discussions_db):
    # P0 entegrasyonu: device-key ile pozisyon -> kimlik KEY'den, verified=1
    tid = await _new_topic(client)
    surer_key = await _mint(client, "surer")
    r = await _pos(client, tid, "klipper", key=surer_key)  # sahte iddia
    assert r.json()["device"] == "surer"
    assert r.json()["verified"] is True


# ── Codex#305 2.tur exploit-regresyon kilitleri ──────────────────────────────


async def test_synthesize_blocked_during_blind_round(client, discussions_db):
    # Exploit #1: taze/bos konu 'sentezlenmis' gosterilemez — kor-tur bitmeden 409
    tid = await _new_topic(client, expected="surer,klipper")
    r = await client.post(
        f"/api/v1/memory/discussions/{tid}/synthesize",
        json={"synthesis": "Yakinsama: bos konu uzerinde sahte sentez denemesi. Ihtilaf: yok.", "device": "opencode"},
        headers=_hdr(),
    )
    assert r.status_code == 409


async def test_master_key_blind_write_rejected(client, discussions_db):
    # Exploit #7 + squat-onlemi: master-key (unverified) kor-turda pozisyon YAZAMAZ —
    # ne written-set'i kirletebilir ne UNIQUE-slotu isgal edip gercek cihazi bloklayabilir
    tid = await _new_topic(client, expected="surer,klipper")
    r = await _pos(client, tid, "surer")  # master-key, sahte-device iddiasi
    assert r.status_code == 401
    # gercek surer hala yazabiliyor (slot isgal edilmedi)
    surer_key = await _mint(client, "surer")
    assert (await _pos(client, tid, "surer", key=surer_key)).status_code == 200


async def test_master_as_device_cannot_peek_blind_positions(client, discussions_db):
    # Exploit #6: master + ?as_device=surer ile kor-tur pozisyonu tam-metin OKUNAMAZ
    tid = await _new_topic(client)
    surer_key = await _mint(client, "surer")
    await _pos(client, tid, "surer", key=surer_key)
    r = await client.get(f"/api/v1/memory/discussions/{tid}/positions?as_device=surer", headers=_hdr())
    assert r.json()["blind"] is True
    assert r.json()["positions"] == []  # iddia kanit degil; key-kimligi yoksa hicbir sey gorunmez


async def test_decide_before_synthesis_rejected(client, discussions_db):
    # Bulgu #2: sentez-asamasi atlanamaz — discussion-fazinda decide 409
    tid = await _new_topic(client, expected="surer")
    surer_key = await _mint(client, "surer")
    await _pos(client, tid, "surer", key=surer_key)  # kor-tur acildi -> discussion
    r = await client.post(f"/api/v1/memory/discussions/{tid}/decide", json={"decision": "Erken karar denemesi"}, headers=_hdr())
    assert r.status_code == 409


async def test_expired_open_topics_swept_from_listing(client, discussions_db):
    # Bulgu #9: deadline'i gecmis 'open' konu ?status=open listesinde sonsuza dek gorunmez
    tid = await _new_topic(client)
    con = sqlite3.connect(discussions_db)
    con.execute("UPDATE discussion_topics SET blind_deadline=datetime('now','-1 hours') WHERE id=?", (tid,))
    con.commit()
    con.close()
    r = await client.get("/api/v1/memory/discussions?status=open", headers=_hdr())
    assert tid not in [t["id"] for t in r.json()["topics"]]
    r = await client.get("/api/v1/memory/discussions?status=discussion", headers=_hdr())
    assert tid in [t["id"] for t in r.json()["topics"]]


async def test_ui_position_requires_write_jwt(client, discussions_db, read_headers, auth_headers):
    # Bulgu #3: read-only JWT dashboard-formundan pozisyon YARATAMAZ; #4: yazinca acilim tetiklenir
    tid = await _new_topic(client, expected="turgut")
    body = {"device": "turgut", **_TPL}
    r = await client.post(f"/api/v1/discussions-ui/topics/{tid}/position", json=body, headers=read_headers)
    assert r.status_code == 403
    r = await client.post(f"/api/v1/discussions-ui/topics/{tid}/position", json=body, headers=auth_headers)
    assert r.status_code == 200
    # Bulgu #4: turgut SON (tek) beklenen yazan -> durum 'open'da takili kalmaz
    assert r.json()["topic_status"] == "discussion"
