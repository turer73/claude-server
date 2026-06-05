"""Slice B — scripts/auto-investigate.py: tekrarlayan-critical otonom salt-okunur inceleme.

Opt-in kapı + per-source rate-limit + /claude read_only + bulgu->discovery. HTTP mock'lanır.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "auto-investigate.py"


def _load():
    spec = importlib.util.spec_from_file_location("auto_investigate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ai(tmp_path, monkeypatch):
    monkeypatch.setenv("INVESTIGATE_STATE_DIR", str(tmp_path / "state"))
    mod = _load()
    monkeypatch.setattr(mod, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(mod, "_envget", lambda k: {"INTERNAL_API_KEY": "ik", "MEMORY_API_KEY": "mk"}.get(k, ""))
    return mod


def test_prompt_contains_source_recur_and_hint(ai):
    p = ai._prompt("cron:renderhane-balance", "4")
    assert "cron:renderhane-balance" in p
    assert "4 kez" in p
    assert "değiştirme" in p  # salt-okuma vurgusu
    # cron kaynağı -> somut log-yolu ipucu (claude keşifte tur harcamasın)
    assert "/var/log/linux-ai-server/renderhane-balance.log" in p


def test_investigate_calls_claude_then_discovery(ai, monkeypatch):
    calls = []

    def _fake_post(url, body, headers, timeout):
        calls.append((url, body, headers))
        if "claude/run" in url:
            return {"ok": True, "result": "Kök-neden: dış API timeout. Çözüm: retry+timeout artır."}
        return {"id": 1, "status": "created"}

    monkeypatch.setattr(ai, "_post_json", _fake_post)
    res = ai.investigate("cron:x", "3")
    assert res["ok"] is True
    urls = [c[0] for c in calls]
    assert any("claude/run" in u for u in urls)
    assert any("memory/discoveries" in u for u in urls)
    # claude read_only zorunlu
    run_body = next(c[1] for c in calls if "claude/run" in c[0])
    assert run_body["read_only"] is True
    # discovery bulguyu içerir
    disc_body = next(c[1] for c in calls if "discoveries" in c[0])
    assert disc_body["title"] == "AUTO-alert: cron:x"
    assert "Kök-neden" in disc_body["details"]


def test_rate_limited_skips(ai, monkeypatch):
    ai._mark("cron:x")  # az önce işaretlendi -> rate-limited
    called = []
    monkeypatch.setattr(ai, "_post_json", lambda *a, **k: called.append(1) or {})
    res = ai.investigate("cron:x", "3")
    assert res["skipped"] == "rate-limited"
    assert called == []  # claude çağrılmaz


def test_no_internal_key_skips(ai, monkeypatch):
    monkeypatch.setattr(ai, "_envget", lambda k: "")  # key yok
    called = []
    monkeypatch.setattr(ai, "_post_json", lambda *a, **k: called.append(1) or {})
    res = ai.investigate("cron:y", "3")
    assert "skipped" in res
    assert called == []


def test_missing_memory_key_skips_before_claude(ai, monkeypatch):
    """Codex P2: mkey yoksa pahalı /claude run BAŞLATILMAZ (boşa harcama önlenir)."""
    monkeypatch.setattr(ai, "_envget", lambda k: "ik" if k == "INTERNAL_API_KEY" else "")
    called = []
    monkeypatch.setattr(ai, "_post_json", lambda *a, **k: called.append(1) or {})
    res = ai.investigate("cron:y", "3")
    assert "skipped" in res
    assert called == []  # claude HİÇ çağrılmadı


def test_main_gated_off_by_default(ai, monkeypatch):
    """AUTO_INVESTIGATE_ENABLED true değilse investigate ÇAĞRILMAZ (opt-in)."""
    monkeypatch.setattr(ai, "_envget", lambda k: "")  # ENABLED yok
    called = []
    monkeypatch.setattr(ai, "investigate", lambda *a: called.append(1))
    monkeypatch.setattr(ai.sys, "argv", ["x", "cron:z", "3"])
    assert ai.main() == 0
    assert called == []


def test_empty_finding_no_discovery(ai, monkeypatch):
    """claude boş dönerse discovery yazılmaz."""
    calls = []

    def _fake_post(url, body, headers, timeout):
        calls.append(url)
        return {"ok": True, "result": ""}  # boş

    monkeypatch.setattr(ai, "_post_json", _fake_post)
    res = ai.investigate("cron:bos", "3")
    assert res["ok"] is False
    assert not any("discoveries" in u for u in calls)
