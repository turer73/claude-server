"""admin.py testleri — secrets yönetimi + autonomous-timeline sınıflandırma
(P0: hiç testi yoktu; secrets = en yüksek riskli admin yüzeyi).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api import admin as adm

# ── _classify_memory prefix sırası ──


def test_classify_longer_prefix_wins():
    """Sıra KRİTİK: autonomous-spawn-poison-* dlq'dur, spawn DEĞİL."""
    assert adm._classify_memory("autonomous-spawn-poison-12-x")[0] == "dlq"
    assert adm._classify_memory("autonomous-spawn-12-x")[0] == "spawn"


def test_classify_threat_critical():
    etype, _label, severity = adm._classify_memory("autonomous-threat-detect-99-y")
    assert etype == "threat"
    assert severity == "critical"


def test_classify_unknown_falls_back_memory():
    etype, label, severity = adm._classify_memory("baska-bir-sey")
    assert (etype, label, severity) == ("memory", None, "info")


# ── _extract_note_id ──


def test_extract_note_id_basic():
    assert adm._extract_note_id("autonomous-spawn-173-20260518", "spawn") == 173


def test_extract_note_id_skips_dateonly_types():
    """health/daily_summary'de tarih note-id zannedilmez."""
    assert adm._extract_note_id("autonomous-health-fail-20260518", "health") is None
    assert adm._extract_note_id("autonomous-daily-summary-20260518", "daily_summary") is None


# ── SecretSet validasyonu ──


def test_secret_key_charset_enforced():
    with pytest.raises(ValidationError):
        adm.SecretSet(key="kucuk-harf", value="v")
    with pytest.raises(ValidationError):
        adm.SecretSet(key="A B", value="v")
    with pytest.raises(ValidationError):
        adm.SecretSet(key="PATH=x;evil", value="v")
    assert adm.SecretSet(key="MY_KEY_2", value="v").key == "MY_KEY_2"


def test_secret_value_limits():
    with pytest.raises(ValidationError):
        adm.SecretSet(key="K", value="   ")
    with pytest.raises(ValidationError):
        adm.SecretSet(key="K", value="x" * 4001)


# ── endpoint'ler ──


async def test_list_secrets_never_returns_values(client, auth_headers, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("GIZLI_TOKEN=cok-gizli-deger-123\n# yorum\nbozuk satir\nIKINCI_KEY=abc\n")
    monkeypatch.setattr(adm, "ENV_PATH", str(env))
    resp = await client.get("/api/v1/admin/secrets", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    keys = {k["key"] for k in body["keys"]}
    assert keys == {"GIZLI_TOKEN", "IKINCI_KEY"}
    # KRİTİK: value response'un HİÇBİR yerinde olmamalı
    assert "cok-gizli-deger-123" not in resp.text
    assert body["keys"][0]["length"] > 0


async def test_secrets_require_admin_not_read(client, read_headers):
    """read-JWT secrets'a erişemez (privilege-escalation fix gate'i)."""
    r1 = await client.get("/api/v1/admin/secrets", headers=read_headers)
    assert r1.status_code in (401, 403)
    r2 = await client.post("/api/v1/admin/secrets", json={"key": "K", "value": "v"}, headers=read_headers)
    assert r2.status_code in (401, 403)


async def test_secrets_require_auth_at_all(client):
    resp = await client.get("/api/v1/admin/secrets")
    assert resp.status_code in (401, 403)


async def test_set_secret_helper_missing_500(client, auth_headers, monkeypatch):
    monkeypatch.setattr(adm, "HELPER_PATH", "/yok/boyle/helper.sh")
    resp = await client.post("/api/v1/admin/secrets", json={"key": "MY_KEY", "value": "v"}, headers=auth_headers)
    assert resp.status_code == 500


async def test_set_secret_helper_value_via_stdin(client, auth_headers, tmp_path, monkeypatch):
    """Value helper'a STDIN ile gider (argv'de sızmaz); helper rc=0 → 200."""
    helper = tmp_path / "helper.sh"
    helper.write_text('#!/bin/bash\nread -r v\necho "set:$1:len=${#v}"\n')
    helper.chmod(0o755)
    monkeypatch.setattr(adm, "HELPER_PATH", str(helper))
    resp = await client.post("/api/v1/admin/secrets", json={"key": "MY_KEY", "value": "gizli"}, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "MY_KEY"
    assert body["value_length"] == 5
    assert "gizli" not in body["action"]  # value response'a yansımaz


async def test_set_secret_helper_failure_400(client, auth_headers, tmp_path, monkeypatch):
    helper = tmp_path / "helper.sh"
    helper.write_text('#!/bin/bash\necho "hata" >&2\nexit 3\n')
    helper.chmod(0o755)
    monkeypatch.setattr(adm, "HELPER_PATH", str(helper))
    resp = await client.post("/api/v1/admin/secrets", json={"key": "MY_KEY", "value": "v"}, headers=auth_headers)
    assert resp.status_code == 400
