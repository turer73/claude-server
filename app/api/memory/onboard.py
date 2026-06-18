"""Onboarding prompt + session-context router handler'ları (public_router).

Gövdeler birebir taşındı (Faz 3). MEMORY_API_KEY test'lerce monkeypatch'lenen
bir VALUE olduğu için import-bind EDİLMEZ — _pkg.MEMORY_API_KEY ile call-time
okunur (yoksa patch bu modüldeki kopyaya ulaşmaz).
"""

import asyncio

import httpx
from fastapi import HTTPException
from fastapi.responses import PlainTextResponse

from app.api import memory as _pkg
from app.api.memory import _TOKEN_BUDGET, _ensure_read_by, _unread_pred, get_db, public_router


@public_router.get("/onboard/{device_name}")
async def get_onboard_prompt(device_name: str):
    db = get_db()
    try:
        device = db.execute("SELECT * FROM devices WHERE name=?", (device_name,)).fetchone()
        if not device:
            raise HTTPException(404, f"Device '{device_name}' not found")
        dev = dict(device)

        recent = db.execute(
            "SELECT session_num, date, device_name, substr(summary,1,80) as summary FROM sessions ORDER BY id DESC LIMIT 5"
        ).fetchall()
        recent_text = "\n".join(f"  - #{r[0]} ({r[2]}, {r[1]}): {r[3]}" for r in recent)

        bugs = db.execute("SELECT project, title, device_name FROM discoveries WHERE type='bug' AND status='active'").fetchall()
        bugs_text = "\n".join(f"  - [{r[0]}] {r[1]} (bulan: {r[2]})" for r in bugs) if bugs else "  Yok"

        _ensure_read_by(db)
        _pred, _pp = _unread_pred(device_name)  # PER-DEVICE okunmamış (#647)
        notes = db.execute(
            f"SELECT from_device, title, content FROM notes WHERE (to_device=? OR to_device IS NULL) AND {_pred}",
            (device_name, *_pp),
        ).fetchall()
        notes_text = "\n".join(f"  - {r[0]}: {r[1]} — {r[2]}" for r in notes) if notes else "  Yok"

        memories = db.execute("SELECT type, name, content FROM memories WHERE active=1 ORDER BY type").fetchall()
        mem_text = "\n".join(f"  [{r[0]}] {r[1]}: {r[2][:120]}" for r in memories)

        stats = db.execute("SELECT COUNT(*) FROM sessions WHERE device_name=?", (device_name,)).fetchone()[0]

        API = "http://127.0.0.1:8420/api/v1/memory"
        KEY = _pkg.MEMORY_API_KEY
        DN = device_name

        prompt = f"""# Merkezi Hafıza Sistemi — {dev["name"]} ({dev["platform"]})

Sen benim çoklu cihazda çalışan Claude asistanımsın. Klipper sunucumda merkezi bir hafıza sistemi var.

## Bağlantı
- **API:** `{API}`
- **Auth:** `X-Memory-Key: {KEY}`
- **Cihaz:** `{DN}` | **Platform:** `{dev["platform"]}` | **Oturum:** {stats}

## Durum
**Son oturumlar:**
{recent_text}

**Açık bug'lar:**
{bugs_text}

**Notlar:**
{notes_text}

## Hafıza
{mem_text}

## API Kullanımı

**Oturum başı:** `curl -s -H "X-Memory-Key: {KEY}" {API}/dashboard`
**Oturum sonu:** `curl -s -X POST {API}/sessions -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","summary":"OZET"}}'`

**Discovery (bug/fix/architecture/plan/workaround/learning/config):**
`curl -s -X POST {API}/discoveries -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","project":"PROJE","type":"TIP","title":"BASLIK","details":"DETAY"}}'`
Duplicate korumalı — aynı title varsa günceller.

**Status değiştir:** `curl -s -X PUT {API}/discoveries/ID -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"status":"completed"}}'`
Status: active, completed, obsolete, superseded

**Task:** `curl -s -X POST {API}/tasks -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","project":"PROJE","task":"NE_YAPILDI"}}'`
**Not:** `curl -s -X POST {API}/notes -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"from_device":"{DN}","title":"BASLIK","content":"ICERIK"}}'`
**Arama (FTS):** `curl -s -H "X-Memory-Key: {KEY}" "{API}/search?q=KELIME"`
**Proje detay:** `curl -s -H "X-Memory-Key: {KEY}" "{API}/projects/PROJE_ADI"`
**Health:** `curl -s -H "X-Memory-Key: {KEY}" "{API}/health"`

## Kurallar
- Türkçe konuş, onay bekleme, direkt çöz
- Bug bulursan HEMEN kaydet, oturum sonunda session kaydet
- Sadece git'ten çıkarılamayan bilgileri kaydet (kararların nedeni, workaround koşulları, projeler arası bağlantılar)
- Co-Authored-By EKLEME (Vercel engelliyor)
- Renderhane push: author turer73 olmalı

Önce dashboard'u kontrol et, sonra nasıl yardımcı olabileceğini sor.
"""

        # RAG context (aktif projeler için)
        _RAG_BASE = "http://localhost:8420/api/v1/rag"
        try:
            active_projects = list(
                {
                    r[0]
                    for r in db.execute(
                        "SELECT project FROM discoveries WHERE status='active' ORDER BY created_at DESC LIMIT 10"
                    ).fetchall()
                }
            )
            rag_sections = []
            for proj in active_projects[:3]:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.post(
                        f"{_RAG_BASE}/search",
                        json={"q": f"{proj} nedir ne durumda", "top_k": 2, "project": proj},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        results = data.get("results", []) if "results" in data else data
                        if results and isinstance(results, list):
                            texts = [r.get("text", str(r))[:200] for r in results[:2] if r.get("text")]
                            if texts:
                                rag_sections.append(f"# {proj}\n" + "\n".join(f"  - {t}" for t in texts))
            if rag_sections:
                prompt += "\n\n## Geçmiş Kararlar (RAG)\n" + "\n\n".join(rag_sections)
        except Exception:
            pass

        return {"device": device_name, "platform": dev["platform"], "prompt": prompt}
    finally:
        db.close()


@public_router.get("/onboard/{device_name}/raw")
async def get_onboard_prompt_raw(device_name: str):
    result = await get_onboard_prompt(device_name)
    return PlainTextResponse(result["prompt"])


@public_router.get("/onboard/{device_name}/project-scan")
async def get_project_scan_prompt(device_name: str):
    """Proje tarama prompt'u — proje klasöründe yapıştır, analiz + DB kayıt"""
    db = get_db()
    try:
        device = db.execute("SELECT * FROM devices WHERE name=?", (device_name,)).fetchone()
        if not device:
            raise HTTPException(404, f"Device '{device_name}' not found")

        API = "http://127.0.0.1:8420/api/v1/memory"
        KEY = _pkg.MEMORY_API_KEY
        DN = device_name

        projects = [r[0] for r in db.execute("SELECT DISTINCT project FROM discoveries ORDER BY project").fetchall()]
        proj_list = ", ".join(projects) if projects else "henüz yok"

        prompt = f"""Bu proje klasörünü analiz et, klipper hafıza DB'sine kaydet.

## Bağlantı
API: {API} | Auth: X-Memory-Key: {KEY} | Cihaz: {DN}
Mevcut projeler: {proj_list}

## Adımlar

**1. Analiz et:**
```bash
pwd && git remote -v 2>/dev/null && git log --oneline -20
```
Proje adı (kısa, küçük harf), stack, test sayısı belirle.

**2. Mevcut kayıt var mı kontrol et:**
```bash
curl -s -H "X-Memory-Key: {KEY}" "{API}/projects/PROJE_ADI"
```
Kayıt varsa sadece eksikleri tamamla.

**3. Cihaz-proje eşle:**
```bash
curl -s -X POST {API}/device-projects -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","project":"PROJE","local_path":"YOL"}}'
```

**4. Kaydet** (duplicate korumalı — tekrar göndersen sorun olmaz):

Mimari kararlar (stack seçimi, DB, deploy, tasarım — git'ten çıkarılamayan NEDEN bilgisi):
```bash
curl -s -X POST {API}/discoveries -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","project":"P","type":"architecture","title":"BASLIK","details":"DETAY"}}'
```

Planlar (aktif hedefler, roadmap):
```bash
curl -s -X POST {API}/discoveries -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","project":"P","type":"plan","title":"BASLIK","details":"DETAY"}}'
```

Bug (bilinen sorunlar): type="bug"
Fix (önemli düzeltmeler — her typo fix değil, sadece önemli olanlar): type="fix"
Workaround (geçici çözümler, neden geçici olduğu): type="workaround"

**5. Önemli task'ler** (son 2 ay, anlamlı iş birimleri — her commit değil):
```bash
curl -s -X POST {API}/tasks -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","project":"P","task":"NE_YAPILDI","details":"DETAY"}}'
```

**6. Oturum kaydet + özet ver.**

## Kurallar
- Title max 60 karakter
- Sadece git'ten çıkarılamayan bilgileri kaydet
- "fix: typo" gibi trivial şeyleri KAYDETME
- Onay bekleme, direkt çalış

Başla.
"""
        return PlainTextResponse(prompt)
    finally:
        db.close()


def _session_context_query(device_name: str):
    db = get_db()
    try:
        device = db.execute("SELECT * FROM devices WHERE name=?", (device_name,)).fetchone()
        if not device:
            raise HTTPException(404, f"Device '{device_name}' not found")

        recent = [
            dict(r)
            for r in db.execute(
                "SELECT session_num, date, device_name, substr(summary,1,120) as summary FROM sessions ORDER BY id DESC LIMIT 3"
            ).fetchall()
        ]

        active_bugs = [
            dict(r)
            for r in db.execute(
                "SELECT project, title FROM discoveries WHERE status='active' AND type='bug' ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
        ]

        _ensure_read_by(db)
        _pred, _pp = _unread_pred(device_name)
        unread_notes = [
            dict(r)
            for r in db.execute(
                f"SELECT from_device, title, substr(content,1,200) as content "
                f"FROM notes WHERE (to_device=? OR to_device IS NULL) AND {_pred} ORDER BY created_at DESC LIMIT 5",
                (device_name, *_pp),
            ).fetchall()
        ]

        projects = [
            dict(r)
            for r in db.execute(
                "SELECT project, COUNT(*) as total, "
                "SUM(CASE WHEN type='bug' THEN 1 ELSE 0 END) as bug_count "
                "FROM discoveries WHERE status='active' GROUP BY project ORDER BY total DESC LIMIT 10"
            ).fetchall()
        ]

        never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE read_count=0").fetchone()[0]
        stale_60 = db.execute(
            "SELECT COUNT(*) FROM discoveries WHERE status='active' AND read_count=0 AND created_at < datetime('now', '-60 days')"
        ).fetchone()[0]

        return {
            "device": device_name,
            "platform": device["platform"],
            "session_count": db.execute("SELECT COUNT(*) FROM sessions WHERE device_name=?", (device_name,)).fetchone()[0],
            "recent_sessions": recent,
            "active_bugs": active_bugs,
            "unread_notes": unread_notes,
            "projects": projects,
            "stale": {"never_read": never_read, "stale_60_days": stale_60},
            "token_budget": _TOKEN_BUDGET,
        }
    finally:
        db.close()


@public_router.get("/onboard/{device_name}/session-context")
async def get_session_context(device_name: str):
    """SessionStart hook için JSON context — budget: ~2000 token.
    Faz 2: sync DB to_thread'e offload (event-loop blokmaz)."""
    return await asyncio.to_thread(_session_context_query, device_name)
