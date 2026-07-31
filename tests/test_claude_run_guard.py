"""claude_result() — /api/v1/claude/run yanıt-guard'ı.

Regresyon çıpası: 429 haftalık-limit yanıtı BOŞ-OLMAYAN `result` + `is_error:true`
ile gelir; guard bunu elemezse hata metni gerçek teşhis diye DB'ye yazılır
(2026-07-24..31'de 17 zehirli kayıt böyle oluştu).
"""

from __future__ import annotations

import pytest

from app.core.claude_run import claude_result

# Canlı kanıt: data/hook-logs/autonomous-claude-spawn-*.log içinden birebir shape.
# DİKKAT: subtype "success" ama is_error true — endpoint bunu ok=False'a çevirir.
RATE_LIMITED = {
    "ok": False,
    "result": "You've hit your weekly limit · resets Jul 10, 7pm (Europe/Istanbul)",
    "cost": 0,
    "session_id": "9c640b71-98ca-44fe-bb34-59b50602cf7e",
}


def test_rate_limited_yanit_elenir():
    """Asıl bug: result boş değil, bu yüzden çağıranın boş-kontrolü geçiyordu."""
    assert RATE_LIMITED["result"], "fixture geçersiz: bug'ın şartı result'ın DOLU olması"
    assert claude_result(RATE_LIMITED) == ""


def test_basarili_yanit_gecer():
    payload = {"ok": True, "result": "  Kök-neden: disk dolu.  "}
    assert claude_result(payload) == "Kök-neden: disk dolu."


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "Claude Code CLI bulunamadi"},  # binary yok (ok ALANI YOK)
        {"error": "Zaman asimi (5dk)"},  # timeout (ok alanı YOK)
        {"ok": False, "raw": "çöp", "stderr": "boom"},  # JSONDecodeError yolu
        {},
        {"ok": True},  # result yok
        {"ok": True, "result": None},
        {"ok": True, "result": "   "},  # yalnız boşluk
        {"ok": True, "result": 42},  # str değil
    ],
)
def test_hatali_yanitlar_bos_doner(payload):
    assert claude_result(payload) == ""


@pytest.mark.parametrize("payload", [None, "string", [], 0])
def test_dict_olmayan_girdi(payload):
    """Fail-closed: beklenmedik şekil patlatmaz, eler."""
    assert claude_result(payload) == ""


def test_ok_eksikse_fail_closed():
    """`ok` yoksa result DOLU olsa bile güvenilmez sayılır."""
    assert claude_result({"result": "gerçek gibi görünen metin"}) == ""


def test_mesru_metin_sansurlenmez():
    """Sentinel-tarama YOK: 'weekly limit' geçen MEŞRU rapor elenmemeli
    (mentions ≠ is-an-error)."""
    payload = {"ok": True, "result": "Ajan weekly limit hatası aldı, quota artırılmalı."}
    assert claude_result(payload) == "Ajan weekly limit hatası aldı, quota artırılmalı."
