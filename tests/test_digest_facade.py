"""app/core/digest facade (load_env / send_telegram / gather) — direkt testler.

Bu 3 fonksiyon paket-split öncesi HİÇ direkt test edilmiyordu (API/cron testlerinde
hep mock'lanıyordu). Split sonrası facade'ta yaşarlar; .env-parse, Telegram-guard ve
gather-orchestration gerçek-gövdesi burada kilitlenir.
"""

from __future__ import annotations

import urllib.request

from app.core import digest as core_digest


def test_load_env_parses_comments_quotes_blank(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# yorum\n\nTOKEN=\"abc123\"\nCHAT_ID=42\nQUOTED='single'\nNO_EQUALS_LINE\n  SPACED = x \n")
    monkeypatch.setattr(core_digest, "ENV_PATH", str(env_file))
    env = core_digest.load_env()
    assert env["TOKEN"] == "abc123"  # çift-tırnak soyulur
    assert env["CHAT_ID"] == "42"
    assert env["QUOTED"] == "single"  # tek-tırnak soyulur
    assert "NO_EQUALS_LINE" not in env  # '=' yok → atlanır
    assert env["SPACED"] == "x"  # key/value trim


def test_load_env_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(core_digest, "ENV_PATH", str(tmp_path / "yok.env"))
    assert core_digest.load_env() == {}  # OSError → {} (fail-safe)


def test_send_telegram_missing_creds_returns_false():
    assert core_digest.send_telegram("<b>x</b>", {}) is False  # token/chat_id yok
    assert core_digest.send_telegram("x", {"TELEGRAM_BOT_TOKEN": "t"}) is False  # chat_id yok


def test_send_telegram_success(monkeypatch):
    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    sent = {}

    def fake_urlopen(req, timeout=8):
        sent["url"] = req.full_url
        sent["data"] = req.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok = core_digest.send_telegram("<b>hi</b>", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "99"})
    assert ok is True
    assert "sendMessage" in sent["url"]
    assert b"chat_id=99" in sent["data"]  # HTML gövde encode edildi


def test_send_telegram_http_error_returns_false(monkeypatch):
    def boom(req, timeout=8):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert core_digest.send_telegram("x", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}) is False


def test_gather_orchestrates_all_sources(monkeypatch):
    # 9 collector'ı sentinel'le değiştir → gather'ın topladığı dict yapısını kilitle
    stubs = {
        "memory_delta": "M",
        "all_commits": "C",
        "cron_health": "CR",
        "cron_outcomes_health": "CO",
        "pr_review_health": "PR",
        "_liveness_health": "LV",
        "system_health": "SY",
        "vps_health": "VP",
        "ci_health": "CI",
    }
    for name, val in stubs.items():
        monkeypatch.setattr(core_digest, name, lambda *a, _v=val, **kw: _v)
    d = core_digest.gather(token="ghp_x")
    assert d == {
        "memory": "M",
        "commits": "C",
        "cron": "CR",
        "cron_jobs": "CO",
        "pr_review": "PR",
        "liveness": "LV",
        "system": "SY",
        "vps": "VP",
        "ci": "CI",
    }
