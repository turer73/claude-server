"""Tests for Telegram bot webhook + polling-shared core (process_update).

process_update auth-bagimsiz; webhook endpoint secret_token ile korunur.
"""

from unittest.mock import patch

import pytest


@pytest.mark.anyio
async def test_webhook_route_registered(client):
    resp = await client.get("/openapi.json")
    paths = resp.json()["paths"]
    assert "/webhooks/telegram/update" in paths


def _fake_update(text: str, chat_id: int = 123, msg_id: int = 1) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": msg_id,
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def test_process_update_skips_non_research():
    from app.api.telegram_bot import process_update

    with patch("app.api.telegram_bot._send_message") as snd, patch("app.api.telegram_bot.research_ask") as ask:
        out = process_update(_fake_update("merhaba"))
    assert out["ok"]
    assert out["skipped"] == "not /research"
    snd.assert_not_called()
    ask.assert_not_called()


def test_process_update_help_when_empty_question():
    from app.api.telegram_bot import process_update

    with patch("app.api.telegram_bot._send_message") as snd, patch("app.api.telegram_bot.research_ask") as ask:
        out = process_update(_fake_update("/research"))
    assert out["action"] == "help"
    snd.assert_called_once()
    # Help text gonderildi, research_ask cagirilmamali
    ask.assert_not_called()
    args, kwargs = snd.call_args
    assert "Kullanim" in args[1]


def test_process_update_calls_research_and_sends_reply():
    from app.api.telegram_bot import process_update

    fake_result = {
        "answer": "petvet'te HSTS eksik [discovery:323]",
        "engine": "claude",
        "source_count": 1,
        "citations": {"used": ["discovery:323"], "hallucinated": [], "unused": []},
        "duration_ms": {"total": 1234, "retrieval": 5, "synthesis": 1229},
    }
    with (
        patch("app.api.telegram_bot.research_ask", return_value=fake_result) as ask,
        patch("app.api.telegram_bot._send_message") as snd,
        patch("app.api.telegram_bot._send_typing"),
    ):
        out = process_update(_fake_update("/research petvet headers"))
    assert out["action"] == "answered"
    ask.assert_called_once()
    snd.assert_called_once()
    # Yanit metninde answer + citation tag bulunmali
    sent_text = snd.call_args[0][1]
    assert "petvet'te HSTS eksik" in sent_text
    assert "discovery:323" in sent_text
    assert "claude" in sent_text  # engine footer
    assert "1234ms" in sent_text


def test_process_update_handles_bot_mention():
    from app.api.telegram_bot import process_update

    with (
        patch(
            "app.api.telegram_bot.research_ask",
            return_value={
                "answer": "x",
                "engine": "local",
                "source_count": 0,
                "citations": {"used": [], "hallucinated": [], "unused": []},
                "duration_ms": {"total": 1},
            },
        ),
        patch("app.api.telegram_bot._send_message"),
        patch("app.api.telegram_bot._send_typing"),
    ):
        # /research@botname formati
        out = process_update(_fake_update("/research@vps_backup_3dlabx_bot test"))
    assert out["action"] == "answered"


def test_process_update_no_chat_id():
    """Update'te chat_id yoksa skip."""
    from app.api.telegram_bot import process_update

    bad = {"update_id": 1, "message": {"text": "/research test"}}
    out = process_update(bad)
    assert out["skipped"] == "no chat_id"


@pytest.mark.anyio
async def test_webhook_secret_rejected_when_wrong(client, monkeypatch):
    """TELEGRAM_WEBHOOK_SECRET set ise wrong header -> 403."""
    monkeypatch.setattr("app.api.telegram_bot.TELEGRAM_WEBHOOK_SECRET", "expected-secret")
    resp = await client.post(
        "/webhooks/telegram/update",
        json={"update_id": 1, "message": {}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_webhook_secret_accepted_when_correct(client, monkeypatch):
    monkeypatch.setattr("app.api.telegram_bot.TELEGRAM_WEBHOOK_SECRET", "expected-secret")
    resp = await client.post(
        "/webhooks/telegram/update",
        json={"update_id": 1, "message": {}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "expected-secret"},
    )
    assert resp.status_code == 200


def test_process_update_callback_ack_marks_event(tmp_path, monkeypatch):
    """Inline '✅ Gördüm' callback (owner-chat) -> events.acked=1."""
    import sqlite3

    from app.api.telegram_bot import process_update

    db = tmp_path / "ev.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, acked INTEGER DEFAULT 0)")
    con.execute("INSERT INTO events (id, acked) VALUES (5, 0)")
    con.commit()
    con.close()
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    upd = {"callback_query": {"id": "cb1", "data": "ack:5", "message": {"chat": {"id": 777}}}}
    with patch("app.api.telegram_bot._answer_callback") as ans:
        out = process_update(upd)
    assert out["action"] == "ack"
    assert out["marked"] is True
    con = sqlite3.connect(db)
    v = con.execute("SELECT acked FROM events WHERE id=5").fetchone()[0]
    con.close()
    assert v == 1
    ans.assert_called()


def test_process_update_callback_unauthorized(monkeypatch):
    """Sahip-DIŞI chat'ten callback -> reddedilir, acked YAPILMAZ."""
    from app.api.telegram_bot import process_update

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    with patch("app.api.telegram_bot._answer_callback"), patch("app.api.telegram_bot._mark_event_acked") as mk:
        out = process_update({"callback_query": {"id": "cb2", "data": "ack:5", "message": {"chat": {"id": 999}}}})
    assert out["skipped"] == "unauthorized callback"
    mk.assert_not_called()


def test_process_update_callback_unknown_action(monkeypatch):
    """Owner-chat ama bilinmeyen callback_data -> skip (ack YAPILMAZ)."""
    from app.api.telegram_bot import process_update

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    with patch("app.api.telegram_bot._answer_callback") as ans, patch("app.api.telegram_bot._mark_event_acked") as mk:
        out = process_update({"callback_query": {"id": "cb3", "data": "frobnicate:9", "message": {"chat": {"id": 777}}}})
    assert "unknown callback" in out["skipped"]
    mk.assert_not_called()
    ans.assert_called()


def test_mark_event_acked_rejects_non_numeric_id():
    """SQL-injection yüzeyi yok: numeric-olmayan event_id -> False (DB'ye dokunmaz)."""
    from app.api.telegram_bot import _mark_event_acked

    assert _mark_event_acked("5 OR 1=1") is False
    assert _mark_event_acked("abc") is False


def test_mark_event_acked_missing_row(tmp_path, monkeypatch):
    """Var-olmayan event id -> rowcount 0 -> False."""
    import sqlite3

    from app.api.telegram_bot import _mark_event_acked

    db = tmp_path / "ev.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, acked INTEGER DEFAULT 0)")
    con.commit()
    con.close()
    monkeypatch.setenv("DB_PATH", str(db))
    assert _mark_event_acked("404") is False


def test_answer_callback_noop_without_token(monkeypatch):
    """Token yoksa _answer_callback sessiz no-op (network'e gitmez)."""
    import app.api.telegram_bot as tb

    monkeypatch.setattr(tb, "TELEGRAM_BOT_TOKEN", "")
    with patch("app.api.telegram_bot.requests.post") as post:
        tb._answer_callback("cbX", "merhaba")
    post.assert_not_called()


def test_answer_callback_posts_with_token(monkeypatch):
    """Token varsa answerCallbackQuery POST edilir (best-effort)."""
    import app.api.telegram_bot as tb

    monkeypatch.setattr(tb, "TELEGRAM_BOT_TOKEN", "T")
    with patch("app.api.telegram_bot.requests.post") as post:
        tb._answer_callback("cbY", "ok")
    post.assert_called_once()


def test_answer_callback_swallows_network_error(monkeypatch):
    """POST hata atsa bile _answer_callback sessiz (best-effort, raise YOK)."""
    import app.api.telegram_bot as tb

    monkeypatch.setattr(tb, "TELEGRAM_BOT_TOKEN", "T")
    with patch("app.api.telegram_bot.requests.post", side_effect=RuntimeError("net")):
        tb._answer_callback("cbZ", "ok")  # raise etmemeli


def test_mark_event_acked_db_error_returns_false(tmp_path, monkeypatch):
    """Bozuk DB yolu (dizin) -> sqlite3 connect/exec hata -> False (raise YOK)."""
    from app.api.telegram_bot import _mark_event_acked

    monkeypatch.setenv("DB_PATH", str(tmp_path))  # dizin -> connect/exec başarısız
    assert _mark_event_acked("5") is False


# ── Slice-2: [🔧 Uygula] fix callback ──────────────────────────


def test_process_update_callback_fix_owner_executes(monkeypatch):
    """Owner-chat [🔧 Uygula] -> _force_remediate çağrılır, verify-pass toast'u döner."""
    from app.api.telegram_bot import process_update

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    upd = {"callback_query": {"id": "cb9", "data": "fix:12", "message": {"chat": {"id": 777}}}}
    with (
        patch("app.api.telegram_bot._answer_callback") as ans,
        patch("app.api.telegram_bot._force_remediate", return_value={"ok": True, "executed": True, "verify": "pass"}) as fr,
    ):
        out = process_update(upd)
    assert out["action"] == "fix"
    fr.assert_called_once_with("12")
    assert "doğrulandı" in ans.call_args[0][1]


def test_process_update_callback_fix_unauthorized(monkeypatch):
    """Sahip-DIŞI chat'ten [🔧 Uygula] -> RCE-yüzeyi kapalı: _force_remediate ÇAĞRILMAZ."""
    from app.api.telegram_bot import process_update

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    with patch("app.api.telegram_bot._answer_callback"), patch("app.api.telegram_bot._force_remediate") as fr:
        out = process_update({"callback_query": {"id": "cbA", "data": "fix:12", "message": {"chat": {"id": 999}}}})
    assert out["skipped"] == "unauthorized callback"
    fr.assert_not_called()


def test_process_update_callback_fix_bad_id(monkeypatch):
    """fix:<non-numeric> -> _force_remediate ÇAĞRILMAZ (id-guard)."""
    from app.api.telegram_bot import process_update

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    with patch("app.api.telegram_bot._answer_callback"), patch("app.api.telegram_bot._force_remediate") as fr:
        out = process_update({"callback_query": {"id": "cbB", "data": "fix:1;rm", "message": {"chat": {"id": 777}}}})
    assert out["skipped"] == "bad fix id"
    fr.assert_not_called()


def test_process_update_callback_fix_no_actionable(monkeypatch):
    """force-remediate executed=False -> 'otomatik aksiyon yok' toast'u."""
    from app.api.telegram_bot import process_update

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    with (
        patch("app.api.telegram_bot._answer_callback") as ans,
        patch("app.api.telegram_bot._force_remediate", return_value={"ok": True, "executed": False, "reason": "no_actionable_playbook"}),
    ):
        process_update({"callback_query": {"id": "cbC", "data": "fix:5", "message": {"chat": {"id": 777}}}})
    assert "otomatik aksiyon yok" in ans.call_args[0][1]


def test_force_remediate_http_error(monkeypatch):
    """force endpoint 503 -> {ok:False, http:503} (raise YOK)."""
    import app.api.telegram_bot as tb

    class _Resp:
        status_code = 503

    with patch("app.api.telegram_bot.requests.post", return_value=_Resp()):
        res = tb._force_remediate("7")
    assert res["ok"] is False
    assert res["http"] == 503


def test_force_remediate_network_error(monkeypatch):
    """force endpoint network hata -> {ok:False, error:...} (raise YOK)."""
    import app.api.telegram_bot as tb

    with patch("app.api.telegram_bot.requests.post", side_effect=RuntimeError("conn refused")):
        res = tb._force_remediate("7")
    assert res["ok"] is False
    assert "conn refused" in res["error"]


_CC = "app.api.telegram_bot._handle_claude"


def test_process_update_claude_unauthorized(monkeypatch):
    """/claude SAHİP-DIŞI chat'ten -> reddedilir, _handle_claude ÇAĞRILMAZ (agent-RCE guard)."""
    from app.api.telegram_bot import process_update

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    with patch(_CC) as h, patch("app.api.telegram_bot._send_message"):
        out = process_update(_fake_update("/claude rm test", chat_id=999))
    assert out["skipped"] == "claude unauthorized"
    h.assert_not_called()


def test_process_update_claude_owner_routes(monkeypatch):
    """/claude owner-chat -> _handle_claude(prompt, fresh=False)."""
    from app.api.telegram_bot import process_update

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    with patch(_CC, return_value={"ok": True, "action": "claude-spawned"}) as h:
        process_update(_fake_update("/claude disk doluluk ne?", chat_id=777))
    h.assert_called_once()
    assert h.call_args[0][1] == "disk doluluk ne?"
    assert h.call_args.kwargs.get("fresh") is False


def test_process_update_claude_new_is_fresh(monkeypatch):
    """/claude-new -> fresh=True (oturum sıfırla)."""
    from app.api.telegram_bot import process_update

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    with patch(_CC, return_value={}) as h:
        process_update(_fake_update("/claude-new sıfırdan başla", chat_id=777))
    assert h.call_args.kwargs.get("fresh") is True
    assert h.call_args[0][1] == "sıfırdan başla"


def test_send_message_falls_back_to_plain_on_markdown_error(monkeypatch):
    """Codex P2: Markdown 400 -> parse_mode'suz düz metin retry (mesaj kaybolmaz)."""
    import app.api.telegram_bot as tb

    monkeypatch.setattr(tb, "TELEGRAM_BOT_TOKEN", "T")
    calls = []

    class _R:
        ok = False  # ilk Markdown denemesi başarısız

    def _post(url, json=None, timeout=None):
        calls.append(dict(json))  # kopya — payload reuse/mutate ediliyor
        return _R()

    with patch("app.api.telegram_bot.requests.post", side_effect=_post):
        tb._send_message(123, "dengesiz `backtick ve _alt")
    assert len(calls) == 2  # markdown + plain retry
    assert "parse_mode" in calls[0]
    assert "parse_mode" not in calls[1]  # fallback düz metin


def test_send_message_no_retry_when_ok(monkeypatch):
    """Markdown başarılıysa tek POST (gereksiz retry yok)."""
    import app.api.telegram_bot as tb

    monkeypatch.setattr(tb, "TELEGRAM_BOT_TOKEN", "T")
    calls = []

    class _R:
        ok = True

    def _post(url, json=None, timeout=None):
        calls.append(json)
        return _R()

    with patch("app.api.telegram_bot.requests.post", side_effect=_post):
        tb._send_message(123, "temiz mesaj")
    assert len(calls) == 1


def test_claude_worker_success_saves_and_replies(monkeypatch):
    """Worker: başarılı run -> bulgu kaydet + footer + oturum güncelle + yanıt gönder."""
    import app.api.telegram_bot as tb

    monkeypatch.setattr(tb, "_send_typing", lambda c: None)
    monkeypatch.setattr(
        tb, "_run_claude", lambda p, session_id=None: {"ok": True, "result": "disk %80", "session_id": "s9", "model": "opus"}
    )
    saved = {}
    monkeypatch.setattr(tb, "_save_finding", lambda p, a: saved.setdefault("called", (p, a)) or True)
    sent = {}
    monkeypatch.setattr(tb, "_send_message", lambda c, t, reply_to=None: sent.update(text=t))
    tb._CLAUDE_SESSION.pop(555, None)
    tb._claude_worker(555, "disk durumu", 1, fresh=False)
    assert "disk %80" in sent["text"]
    assert "hafızaya kaydedildi" in sent["text"]
    assert "Max-plan" in sent["text"]
    assert tb._CLAUDE_SESSION[555] == "s9"  # oturum güncellendi
    assert saved["called"][0] == "disk durumu"


def test_claude_worker_error_path(monkeypatch):
    """Worker: run hata -> hata mesajı, kayıt YOK."""
    import app.api.telegram_bot as tb

    monkeypatch.setattr(tb, "_send_typing", lambda c: None)
    monkeypatch.setattr(tb, "_run_claude", lambda p, session_id=None: {"ok": False, "error": "http 500"})
    calls = []
    monkeypatch.setattr(tb, "_save_finding", lambda p, a: calls.append((p, a)) or True)
    sent = {}
    monkeypatch.setattr(tb, "_send_message", lambda c, t, reply_to=None: sent.update(text=t))
    tb._claude_worker(556, "x", 1, fresh=True)
    assert "Claude hatası" in sent["text"]
    assert calls == []  # hata yolunda kayıt YOK


def test_handle_claude_empty_shows_help(monkeypatch):
    """Boş prompt -> kullanım yardımı (thread spawn YOK)."""
    from app.api.telegram_bot import _handle_claude

    with patch("app.api.telegram_bot._send_message") as snd:
        out = _handle_claude(777, "", 1)
    assert out["action"] == "claude-help"
    snd.assert_called_once()


def test_run_claude_http_error():
    """run endpoint non-200 -> {ok:False} (raise YOK)."""
    import app.api.telegram_bot as tb

    class _R:
        status_code = 500

    with patch("app.api.telegram_bot.requests.post", return_value=_R()):
        res = tb._run_claude("x")
    assert res["ok"] is False


def test_run_claude_passes_session_id():
    """session_id verilince body'ye eklenir (--resume süreklilik)."""
    import app.api.telegram_bot as tb

    captured = {}

    class _R:
        status_code = 200

        def json(self):
            return {"ok": True, "result": "tamam"}

    def _post(url, json=None, headers=None, timeout=None):
        captured.update(json or {})
        return _R()

    with patch("app.api.telegram_bot.requests.post", side_effect=_post):
        res = tb._run_claude("selam", session_id="sess-123")
    assert captured["session_id"] == "sess-123"
    assert captured["read_only"] is True  # Telegram -> salt-okunur allowlist
    assert captured["cwd"] == "/opt/linux-ai-server"  # repo bağlamı (git/dosya soruları)
    assert res["ok"] is True


def test_save_finding_posts_discovery(monkeypatch):
    """Bulgu discoveries'e POST edilir (köprü-yazar, agent değil)."""
    import app.api.telegram_bot as tb

    monkeypatch.setenv("MEMORY_API_KEY", "mk-test")
    captured = {}

    class _R:
        status_code = 200

    def _post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        captured["key"] = (headers or {}).get("X-Memory-Key")
        return _R()

    with patch("app.api.telegram_bot.requests.post", side_effect=_post):
        ok = tb._save_finding("disk neden doldu", "docker image'ları şişmiş")
    assert ok is True
    assert captured["url"].endswith("/api/v1/memory/discoveries")
    assert captured["body"]["type"] == "learning"
    assert captured["key"] == "mk-test"


def test_save_finding_redacts_title(monkeypatch):
    """Codex P2: title prompt'tan türüyor -> client-side redact (token sızması engeli)."""
    import app.api.telegram_bot as tb

    monkeypatch.setenv("MEMORY_API_KEY", "mk-test")
    monkeypatch.setattr("app.core.privacy.redact", lambda s: ("CLEAN-TITLE", ["secret"]))
    captured = {}

    class _R:
        status_code = 200

    def _post(url, json=None, headers=None, timeout=None):
        captured["body"] = json
        return _R()

    with patch("app.api.telegram_bot.requests.post", side_effect=_post):
        tb._save_finding("token sk-DEADBEEF değeri ne", "cevap")
    assert captured["body"]["title"] == "CLEAN-TITLE"


def test_save_finding_no_key_is_noop(monkeypatch):
    """MEMORY_API_KEY yoksa kayıt denemez (sessiz False)."""
    import app.api.telegram_bot as tb

    monkeypatch.setattr(tb, "read_env_var", lambda k: "")
    monkeypatch.delenv("MEMORY_API_KEY", raising=False)
    with patch("app.api.telegram_bot.requests.post") as post:
        ok = tb._save_finding("x", "y")
    assert ok is False
    post.assert_not_called()


def test_process_update_callback_fix_endpoint_error(monkeypatch):
    """force-remediate ok=False -> '❌ Uygulanamadı' toast'u."""
    from app.api.telegram_bot import process_update

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    with (
        patch("app.api.telegram_bot._answer_callback") as ans,
        patch("app.api.telegram_bot._force_remediate", return_value={"ok": False, "http": 503}),
    ):
        process_update({"callback_query": {"id": "cbD", "data": "fix:5", "message": {"chat": {"id": 777}}}})
    assert "Uygulanamadı" in ans.call_args[0][1]
