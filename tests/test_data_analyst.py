"""scripts/data-analyst.py — haftalık salt-okunur veri-analisti.

Saf-mantık testleri: opt-in kapı, prompt-güvenliği (db-query.sh + ISO-T format uyarısı),
key-yok kısa-devre. HTTP/claude çağrısı monkeypatch ile izole (gerçek /claude çağrılmaz).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("data_analyst", ROOT / "scripts" / "data-analyst.py")
da = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(da)


def test_prompt_forces_db_query_helper_and_isot_caveat():
    p = da._prompt()
    # Yalnız güvenli helper'a yönlendirir (analist ham sqlite/dosya açamaz)
    assert "scripts/db-query.sh" in p
    assert "SALT-OKUMA" in p
    # ISO-T format tuzağı promptta uyarılmış (analist aynı bug'a düşmesin)
    assert "replace(datetime('now'" in p
    assert "ISO-T" in p


def test_disabled_gate_skips_without_http(monkeypatch, capsys):
    """DATA_ANALYST_ENABLED!=true → /claude ÇAĞRILMAZ, OUTCOME: pass (kasıtlı)."""
    monkeypatch.setattr(da, "_envget", lambda k: "" if k == "DATA_ANALYST_ENABLED" else "x")

    def _boom(*a, **k):
        raise AssertionError("disabled iken _post_json çağrılmamalı")

    monkeypatch.setattr(da, "_post_json", _boom)
    rc = da.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "OUTCOME: pass" in out
    assert "opt-in kapalı" in out


def test_run_short_circuits_without_keys(monkeypatch):
    """key yoksa pahalı /claude run başlatılmaz."""
    monkeypatch.setattr(da, "_envget", lambda k: "")

    def _boom(*a, **k):
        raise AssertionError("key yokken _post_json çağrılmamalı")

    monkeypatch.setattr(da, "_post_json", _boom)
    res = da.run()
    assert res["ok"] is False
    assert "INTERNAL_API_KEY" in res["skipped"]


_VALID_DISCOVERY_TYPES = {"bug", "fix", "learning", "config", "workaround", "architecture", "plan"}


def test_run_writes_discovery_and_returns_ok(monkeypatch):
    """Mutlu yol: /claude rapor döner → discovery POST edilir (GEÇERLİ type), telegram best-effort."""
    keys = {"INTERNAL_API_KEY": "ik", "MEMORY_API_KEY": "mk"}
    monkeypatch.setattr(da, "_envget", lambda k: keys.get(k, ""))
    calls = []
    disc_body = {}

    def _fake_post(url, body, headers, timeout):
        calls.append(url)
        if url.endswith("/claude/run"):
            return {"result": "BULGULAR: cpu ortalama %12. ÖNERİ: yok. GENEL: iyi."}
        if url.endswith("/memory/discoveries"):
            disc_body.update(body)
        return {"ok": True}

    monkeypatch.setattr(da, "_post_json", _fake_post)
    monkeypatch.setattr(da, "_send_telegram", lambda r: True)
    res = da.run()
    assert res["ok"] is True
    assert res["report_len"] > 0
    assert res["discovery_err"] == ""  # başarılı yazım
    # hem claude hem discovery çağrıldı
    assert any("/claude/run" in c for c in calls)
    assert any("/memory/discoveries" in c for c in calls)
    # Regresyon: discovery type API'nin kabul ettiği değerlerden olmalı ("note" REDDEDİLİR)
    assert disc_body.get("type") in _VALID_DISCOVERY_TYPES


def test_discovery_failure_is_visible_not_silent(monkeypatch, capsys):
    """Discovery yazılamazsa OUTCOME: partial (sessiz yutulmaz) — sistem teması."""
    keys = {"INTERNAL_API_KEY": "ik", "MEMORY_API_KEY": "mk", "DATA_ANALYST_ENABLED": "true"}
    monkeypatch.setattr(da, "_envget", lambda k: keys.get(k, ""))

    def _fake_post(url, body, headers, timeout):
        if url.endswith("/claude/run"):
            return {"result": "rapor"}
        raise RuntimeError("422 geçersiz tip")  # discovery POST patlar

    monkeypatch.setattr(da, "_post_json", _fake_post)
    monkeypatch.setattr(da, "_send_telegram", lambda r: True)
    rc = da.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "OUTCOME: partial" in out
    assert "DISCOVERY-FAIL" in out


def test_run_empty_report_is_failure(monkeypatch):
    keys = {"INTERNAL_API_KEY": "ik", "MEMORY_API_KEY": "mk"}
    monkeypatch.setattr(da, "_envget", lambda k: keys.get(k, ""))
    monkeypatch.setattr(da, "_post_json", lambda *a, **k: {"result": "   "})
    res = da.run()
    assert res["ok"] is False
    assert "boş" in res["error"]


def test_claude_run_request_is_read_only(monkeypatch):
    """En kritik güvenlik invariant'ı: /claude/run read_only=True ile çağrılır."""
    keys = {"INTERNAL_API_KEY": "ik", "MEMORY_API_KEY": "mk"}
    monkeypatch.setattr(da, "_envget", lambda k: keys.get(k, ""))
    captured = {}

    def _fake_post(url, body, headers, timeout):
        if url.endswith("/claude/run"):
            captured.update(body)
            return {"result": "rapor"}
        return {}

    monkeypatch.setattr(da, "_post_json", _fake_post)
    monkeypatch.setattr(da, "_send_telegram", lambda r: True)
    da.run()
    assert captured.get("read_only") is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
