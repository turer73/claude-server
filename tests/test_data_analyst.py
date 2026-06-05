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


def test_run_writes_discovery_and_returns_ok(monkeypatch):
    """Mutlu yol: /claude rapor döner → discovery POST edilir, telegram best-effort."""
    keys = {"INTERNAL_API_KEY": "ik", "MEMORY_API_KEY": "mk"}
    monkeypatch.setattr(da, "_envget", lambda k: keys.get(k, ""))
    calls = []

    def _fake_post(url, body, headers, timeout):
        calls.append(url)
        if url.endswith("/claude/run"):
            return {"result": "BULGULAR: cpu ortalama %12. ÖNERİ: yok. GENEL: iyi."}
        return {"ok": True}

    monkeypatch.setattr(da, "_post_json", _fake_post)
    monkeypatch.setattr(da, "_send_telegram", lambda r: True)
    res = da.run()
    assert res["ok"] is True
    assert res["report_len"] > 0
    # hem claude hem discovery çağrıldı
    assert any("/claude/run" in c for c in calls)
    assert any("/memory/discoveries" in c for c in calls)


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
