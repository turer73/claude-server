"""Telegram bot webhook — /research command routing.

Bot: vps_backup_3dlabx_bot (token .env -> TELEGRAM_BOT_TOKEN).
Webhook secret: TELEGRAM_WEBHOOK_SECRET (.env'de tanimli olmali).
Telegram setWebhook ile {PUBLIC_URL}/webhooks/telegram/update'e baglanir;
Telegram her update'i bu endpoint'e POST eder.

Akis: /research <soru> mesaji -> research_ask -> Markdown yanit + sendMessage.
Auth: secret_token header (Telegram'in setWebhook ile bekledigi mekanizma);
verify_key gerek yok (bu router public-mountlu).
"""

from __future__ import annotations

import re
import threading

import requests
from fastapi import APIRouter, Header, HTTPException

from app.api.research import AskRequest, research_ask
from app.core.config import read_env_var

TELEGRAM_BOT_TOKEN = read_env_var("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = read_env_var("TELEGRAM_WEBHOOK_SECRET")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

router = APIRouter(prefix="/webhooks/telegram", tags=["telegram-webhook"])


def _send_message(chat_id: int, text: str, reply_to: int | None = None) -> None:
    """Markdown sendMessage helper. Best-effort. Codex P2: Markdown parse hatasında
    (dengesiz `_`/`*`/`[`/backtick — Claude log/path çıktısında sık) Telegram 400 döner
    ve mesaj SESSİZCE kaybolur -> DÜZ-METİN fallback (yanıt asla kaybolmasın)."""
    if not TELEGRAM_BOT_TOKEN:
        return
    payload: dict = {
        "chat_id": chat_id,
        "text": text[:4000],  # Telegram limit 4096
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
        if not getattr(r, "ok", False):
            # Markdown parse başarısız -> parse_mode'suz düz metin olarak tekrar dene.
            payload.pop("parse_mode", None)
            requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
    except Exception:
        pass


def _send_typing(chat_id: int) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"{TELEGRAM_API}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=3,
        )
    except Exception:
        pass


def _answer_callback(callback_id: str, text: str) -> None:
    """answerCallbackQuery — butonun spinner'ını durdurur + toast gösterir. Best-effort."""
    if not TELEGRAM_BOT_TOKEN or not callback_id:
        return
    try:
        requests.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text[:200]},
            timeout=5,
        )
    except Exception:
        pass


def _mark_event_acked(event_id: str) -> bool:
    """events.acked=1 (server.db). poller klipperos + db grup-yazılabilir (#517).
    Escalation _escalate_persistent acked kaynakları atlar."""
    import os
    import sqlite3

    if not str(event_id).isdigit():
        return False
    db_path = os.environ.get("DB_PATH") or "/opt/linux-ai-server/data/server.db"
    try:
        con = sqlite3.connect(db_path, timeout=10)
        try:
            cur = con.execute("UPDATE events SET acked=1 WHERE id=?", (int(event_id),))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()
    except Exception:
        return False


def _force_remediate(event_id: str) -> dict:
    """[🔧 Uygula] -> localhost devops force-remediate (internal-key). Poller AYRI
    process -> in-app agent'a erişemez -> HTTP. Owner-auth ÇAĞRAN katmanda yapıldı."""
    import os

    key = read_env_var("INTERNAL_API_KEY") or os.environ.get("INTERNAL_API_KEY", "")
    try:
        r = requests.post(
            "http://localhost:8420/api/v1/devops/remediate/force",
            json={"event_id": int(event_id)},
            headers={"X-API-Key": key},
            timeout=90,
        )
        if r.status_code != 200:
            return {"ok": False, "http": r.status_code}
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# /claude oturum-sürekliliği: owner-chat -> son Claude Code session_id (poller process
# uzun-ömürlü, update'ler arası kalır). /claude devam ettirir, /claude-new sıfırlar.
_CLAUDE_SESSION: dict[int, str] = {}


def _run_claude(prompt: str, session_id: str | None = None) -> dict:
    """Owner-onaylı /claude -> localhost Claude Code run (internal-key, admin scope).
    Poller AYRI process -> in-app değil HTTP. session_id verilirse --resume (süreklilik)."""
    import os

    key = read_env_var("INTERNAL_API_KEY") or os.environ.get("INTERNAL_API_KEY", "")
    # SALT-OKUNUR (read_only): Claude okur+analiz eder, mutasyon yapmaz. cwd=sunucu
    # repo'su (git log/dosya soruları doğru bağlamda koşsun; default ~/ git-repo değil).
    cwd = read_env_var("CLAUDE_TG_CWD") or os.environ.get("CLAUDE_TG_CWD") or "/opt/linux-ai-server"
    body: dict = {"prompt": prompt, "max_turns": 40, "read_only": True, "cwd": cwd}
    if session_id:
        body["session_id"] = session_id
    try:
        r = requests.post(
            "http://localhost:8420/api/v1/claude/run",
            json=body,
            headers={"X-API-Key": key},
            timeout=320,  # run API 300s'de kill eder; biraz üstü
        )
        if r.status_code != 200:
            return {"ok": False, "error": f"http {r.status_code}"}
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _save_finding(prompt: str, answer: str) -> bool:
    """Salt-okunur /claude bulgusunu hafızaya (discoveries) kaydet — KÖPRÜ yazar,
    agent DEĞİL (read-only güvenlik korunur). Best-effort; hata reply'ı bozmaz.
    type=learning, project=linux-ai-server. Secret redaction + 5dk dedup server-side."""
    import os

    key = read_env_var("MEMORY_API_KEY") or os.environ.get("MEMORY_API_KEY", "")
    if not key or not answer:
        return False
    # GÜVENLİK (Codex P2): title prompt'tan türüyor; /memory/discoveries SADECE details'i
    # redact eder, title'ı değil -> prompt'taki token/şifre title'a sızabilir. Client-side redact.
    from app.core.privacy import redact

    title_raw = "Telegram /claude: " + prompt.strip().replace("\n", " ")
    title = redact(title_raw)[0][:90]
    body = {
        "device_name": "telegram-claude",
        "project": "linux-ai-server",
        "type": "learning",
        "title": title,
        "details": answer[:4000],
        "rationale": "Telegram salt-okunur /claude araştırma bulgusu (köprü-kayıt).",
    }
    try:
        r = requests.post(
            "http://localhost:8420/api/v1/memory/discoveries",
            json=body,
            headers={"X-Memory-Key": key},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


def _handle_claude(chat_id: int, prompt: str, msg_id: int | None, fresh: bool = False) -> dict:
    """Owner-onaylı /claude: Claude Code'u çağırır (threaded -> poller bloklanmaz),
    typing keep-alive, oturum-sürekliliği. Owner-auth ÇAĞRAN katmanda (process_update)."""
    if not prompt:
        _send_message(
            chat_id,
            "*Kullanım:*\n`/claude <soru>` — Claude Code araştırır (oturum devam eder)\n"
            "`/claude-new <...>` — yeni oturum (geçmişi sıfırla)\n\n"
            "_SALT-OKUNUR: log/dosya okur + analiz eder, değişiklik/komut-icra YAPMAZ._\n"
            "_Bulgular hafızaya (discoveries) kaydedilir · Max-plan (API faturası yok)._",
            reply_to=msg_id,
        )
        return {"ok": True, "action": "claude-help"}

    threading.Thread(target=_claude_worker, args=(chat_id, prompt, msg_id, fresh), daemon=True).start()
    return {"ok": True, "action": "claude-spawned"}


def _claude_worker(chat_id: int, prompt: str, msg_id: int | None, fresh: bool) -> None:
    """/claude arka-plan işçisi (thread'de koşar; modül-seviye -> test edilebilir).
    typing keep-alive + read-only run + oturum-güncelle + bulgu-kaydet + yanıt."""
    stop = threading.Event()

    def _keepalive():
        _send_typing(chat_id)
        while not stop.wait(4.0):
            _send_typing(chat_id)

    ka = threading.Thread(target=_keepalive, daemon=True)
    ka.start()
    try:
        sid = None if fresh else _CLAUDE_SESSION.get(chat_id)
        res = _run_claude(prompt, session_id=sid)
        if res.get("error") or not res.get("ok", False):
            reply = f"❌ *Claude hatası:* `{res.get('error') or res.get('stderr', 'bilinmeyen')[:200]}`"
        else:
            answer = res.get("result") or "(boş yanıt)"
            new_sid = res.get("session_id")
            if new_sid:
                _CLAUDE_SESSION[chat_id] = new_sid
            # Bulguyu hafızaya kaydet (köprü-yazar, read-only korunur).
            saved = _save_finding(prompt, answer) if res.get("result") else False
            # cost GÖSTERME: Max-plan abonelikte faturalanmaz; CLI'nin nosyonel
            # ($API-eşdeğeri) değeri kullanıcıyı yanıltır ("API istemiyorum").
            tag = "💾 hafızaya kaydedildi · " if saved else ""
            footer = f"\n\n{tag}`{res.get('model', 'claude')}` · Max-plan (salt-okunur)"
            reply = answer[:3800] + footer
    except Exception as e:
        reply = f"❌ *Claude hatası:* `{str(e)[:200]}`"
    finally:
        stop.set()
        ka.join(timeout=1.0)
    _send_message(chat_id, reply, reply_to=msg_id)


def _handle_callback(cb: dict) -> dict:
    """Inline-buton callback (ACK / Uygula). GÜVENLİK: yalnız sahip-chat (TELEGRAM_CHAT_ID)."""
    data = cb.get("data", "") or ""
    cb_id = cb.get("id")
    from_chat = (cb.get("message") or {}).get("chat", {}).get("id")
    owner = read_env_var("TELEGRAM_CHAT_ID")
    if not owner or str(from_chat) != str(owner):
        _answer_callback(cb_id, "yetkisiz")
        return {"ok": True, "skipped": "unauthorized callback"}
    if data.startswith("ack:"):
        eid = data.split(":", 1)[1]
        ok = _mark_event_acked(eid)
        _answer_callback(cb_id, "✅ ACK alındı — eskalasyon durdu" if ok else "ack uygulanamadı")
        return {"ok": True, "action": "ack", "event": eid, "marked": ok}
    if data.startswith("fix:"):
        eid = data.split(":", 1)[1]
        if not eid.isdigit():
            _answer_callback(cb_id, "geçersiz id")
            return {"ok": True, "skipped": "bad fix id"}
        res = _force_remediate(eid)
        if not res.get("ok"):
            _answer_callback(cb_id, f"❌ Uygulanamadı ({res.get('http') or res.get('error') or 'hata'})")
        elif not res.get("executed"):
            _answer_callback(cb_id, "ℹ️ Bu uyarı için otomatik aksiyon yok (sadece-inceleme)")
        else:
            verify = res.get("verify")
            toast = {
                "pass": "✅ Uygulandı + doğrulandı (düzeldi)",
                "fail": "⚠️ Uygulandı ama hâlâ kritik — eskale edildi",
                "n/a": "✅ Uygulandı (doğrulama yok)",
            }.get(verify, "✅ Uygulandı")
            _answer_callback(cb_id, toast)
        return {"ok": True, "action": "fix", "event": eid, "result": res}
    _answer_callback(cb_id, "bilinmeyen aksiyon")
    return {"ok": True, "skipped": f"unknown callback: {data[:40]}"}


def _md_escape(text: str) -> str:
    """Markdown v1 — sadece backtick/underscore/star kacir."""
    return re.sub(r"([`_*\[\]])", r"\\\1", text)


def _format_reply(result: dict, q: str) -> str:
    """Research API yanitini Telegram Markdown'a cevir.

    Yapı:
      *Q:* soru
      <answer (max 2500 char)>
      _Kaynaklar:_ tag1, tag2 (max 6)
      `engine` · N kaynak · Ms · (⚠ hallu varsa)
    """
    answer = result.get("answer", "") or "(bos cevap)"
    engine = result.get("engine", "?")
    src_count = result.get("source_count", 0)
    cites = result.get("citations", {})
    used = cites.get("used", [])
    halu = cites.get("hallucinated", [])
    dur = result.get("duration_ms", {}).get("total", 0)

    parts = [f"*Q:* {_md_escape(q[:200])}", "", answer[:2500]]
    if used:
        cite_str = ", ".join(used[:6]) + (f" +{len(used) - 6}" if len(used) > 6 else "")
        parts.extend(["", f"_Kaynaklar:_ {cite_str}"])
    footer = f"`{engine}` · {src_count} kaynak · {dur}ms"
    if halu:
        footer += f" · ⚠️ {len(halu)} hallu"
    parts.extend(["", footer])
    return "\n".join(parts)


def process_update(update: dict) -> dict:
    """Webhook + polling tarafindan paylasilan core handler — auth-bagimsiz."""
    # Inline-buton (ACK) callback'i — message'tan ÖNCE (owner-auth _handle_callback'te).
    if update.get("callback_query"):
        return _handle_callback(update["callback_query"])

    msg = update.get("message") or update.get("edited_message") or {}
    text = msg.get("text", "") or ""
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")

    if not chat_id:
        return {"ok": True, "skipped": "no chat_id"}

    # /claude, /claude-new — SAHİP-ONLY (tam-agent: sunucuda komut çalıştırır/dosya
    # düzenler). Max-plan abonelik (claude_code._build_env API-key'i strip eder).
    m_cc = re.match(r"^/claude(-new)?(@\w+)?(\s|$)", text)
    if m_cc:
        owner = read_env_var("TELEGRAM_CHAT_ID")
        if not owner or str(chat_id) != str(owner):
            _send_message(chat_id, "⛔ /claude yalnız sahip-chat'e açık.", reply_to=msg_id)
            return {"ok": True, "skipped": "claude unauthorized"}
        fresh = bool(m_cc.group(1))  # /claude-new -> oturum sıfırla
        prompt = re.sub(r"^/claude(-new)?(@\w+)?\s*", "", text).strip()
        return _handle_claude(chat_id, prompt, msg_id, fresh=fresh)

    # /research, /research-hi, /research-claude (or @bot_username variants)
    m = re.match(r"^/research(-hi|-claude)?(@\w+)?(\s|$)", text)
    if not m:
        return {"ok": True, "skipped": "not /research"}

    suffix = m.group(1) or ""
    engine = {"": "auto", "-hi": "local-hi", "-claude": "claude"}[suffix]

    question = re.sub(r"^/research(-hi|-claude)?(@\w+)?\s*", "", text).strip()
    if not question:
        _send_message(
            chat_id,
            (
                "*Kullanim:*\n"
                "`/research <soru>` — auto (qwen2.5:3b veya Claude, ~3-10s)\n"
                "`/research-hi <soru>` — aya:8b dogal Turkce (~15-30s, citation zayif)\n"
                "`/research-claude <soru>` — Claude Haiku (~3s, en tutarli citation)\n\n"
                "*Ornek:*\n`/research bilge-arena security header eksiklikleri`"
            ),
            reply_to=msg_id,
        )
        return {"ok": True, "action": "help"}

    # Periodic typing keep-alive — Telegram typing indicator ~5sn'de soner;
    # aya:8b (engine=local-hi) ~27sn surer, kullanici "stuck" sanir. Her 4sn'de
    # bir typing re-trigger. Threading.Event ile temiz cleanup.
    stop = threading.Event()

    def _keepalive():
        _send_typing(chat_id)
        while not stop.wait(4.0):
            _send_typing(chat_id)

    keepalive_thread = threading.Thread(target=_keepalive, daemon=True)
    keepalive_thread.start()

    try:
        req = AskRequest(q=question, engine=engine)
        result = research_ask(req)
        reply = _format_reply(result, question)
    except Exception as e:
        reply = f"❌ *Research hatasi:*\n`{str(e)[:300]}`"
    finally:
        stop.set()
        keepalive_thread.join(timeout=1.0)

    _send_message(chat_id, reply, reply_to=msg_id)
    return {"ok": True, "action": "answered", "engine": engine}


@router.post("/update")
def telegram_update(
    update: dict,
    x_telegram_bot_api_secret_token: str | None = Header(None),
):
    """Telegram webhook receiver. Secret_token ile auth."""
    # FAIL-CLOSED (güvenlik fix): secret yüklenmemişse endpoint'i AÇMA. Eski
    # 'if SECRET:' fail-open'dı -> secret yoksa public research_ask tetikleme
    # (dışarıdan LLM/RAG iş yükü). process_update'e auth'suz girilemez.
    if not TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(503, "Telegram webhook secret not configured (fail-closed)")
    if x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(403, "invalid webhook secret")
    return process_update(update)
