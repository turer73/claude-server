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


def test_vps_health_reads_latest_sample(monkeypatch, tmp_path):
    """P1-a Faz-2a: vps_health get_conn(readonly=False) + Row dict-erisim paritesi.
    (Fonksiyon onceden HIC test edilmiyordu; migrasyon codecov-flag'ledi -> test-eklendi.)"""
    import sqlite3

    from app.core.digest.sources import vps_health

    db = tmp_path / "server.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE vps_metrics_history (timestamp TEXT, online INT, cpu_usage REAL, "
        "memory_usage REAL, disk_usage REAL, containers_total INT, containers_up INT)"
    )
    con.execute("INSERT INTO vps_metrics_history VALUES ('2026-07-03T12:00', 1, 45.5, 60.0, 30.0, 21, 20)")
    con.commit()
    con.close()
    monkeypatch.setattr("app.core.digest.sources._server_db_path", lambda: str(db))
    r = vps_health()
    assert r["online"] is True  # Row['online'] dict-erisim (get_conn row_factory paritesi)
    assert r["cpu"] == 45.5
    assert r["containers_up"] == 20


def test_vps_health_empty_returns_dict(monkeypatch, tmp_path):
    """Veri-yok -> {} (graceful degrade). get_conn dosya-yoksa normal-connect olusturur (readonly=False)."""
    from app.core.digest.sources import vps_health

    monkeypatch.setattr("app.core.digest.sources._server_db_path", lambda: str(tmp_path / "nope.db"))
    assert vps_health() == {}


def test_memory_delta_reads_bugs_and_unread_notes(monkeypatch, tmp_path):
    """P1-a Faz-2a: memory_delta get_conn + Row dict-erisim paritesi.
    get_conn row_factory olmadan dict(r) TypeError atar -> migrasyon geri alinirsa FAIL."""
    import sqlite3

    from app.core.digest.sources import memory_delta

    db = tmp_path / "claude_memory.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE discoveries (id INTEGER PRIMARY KEY, project TEXT, title TEXT, type TEXT, status TEXT, created_at TEXT)")
    con.execute(
        "INSERT INTO discoveries (project, title, type, status, created_at) VALUES "
        "('proj-a', 'eski acik bug', 'bug', 'active', '2026-01-01 10:00:00'), "
        "('proj-a', 'kapali bug', 'bug', 'resolved', '2026-01-01 10:00:00'), "
        "('proj-b', 'yeni bug', 'bug', 'active', '2099-01-01 10:00:00')"
    )
    con.execute(
        "CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT, content TEXT, to_device TEXT, read INT, read_by TEXT, status TEXT)"
    )
    con.execute(
        "INSERT INTO notes (title, content, to_device, read, read_by, status) VALUES "
        "('okunmamis', 'icerik', 'klipper', 0, NULL, 'active'), "
        "('klipper-okudu', 'icerik', NULL, 0, '|klipper|', 'active'), "
        "('held-not', 'icerik', NULL, 0, NULL, 'held')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr("app.core.digest.sources.DB_PATH", str(db))
    r = memory_delta(24)
    assert [b["title"] for b in r["open_bugs"]] == ["eski acik bug", "yeni bug"]
    assert [b["title"] for b in r["new_bugs"]] == ["yeni bug"]  # created_at > since penceresi
    assert [n["title"] for n in r["unread_notes"]] == ["okunmamis"]  # read_by + held filtreleri
