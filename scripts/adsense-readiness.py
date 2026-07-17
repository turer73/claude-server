#!/usr/bin/env python3
"""adsense-readiness.py — AdSense monetizasyon-hazırlık denetçisi + durum-watcher.

"Reklam alma" uzmanı. Her AdSense sitesi için:
  (1) AdSense API'den site DURUMU (READY / NEEDS_ATTENTION / REQUIRES_REVIEW / ...)
  (2) İçerik denetimi: sitemap envanteri, anasayfa derinliği, güven sayfaları
      (Hakkımızda/İletişim/Gizlilik — locale-aware), ads.txt doğruluğu, AdSense snippet
  (3) /claude ile içerik-KALİTE notu (özgün/doyurucu mu, ince/şablon mu — best-effort)
  → hazırlık-checklist + somut öneriler → ortak-hafıza (type=learning, mail yok).

DURUM DEĞİŞİMİ takibi (data/adsense-readiness-state.json): bir site NEEDS_ATTENTION→READY
(onay!) veya yeni red → ayrı discovery (type=bug) → SessionStart'ta görünür.

NEDEN yok (#1326): API v2 Site kaynağı düşürme-nedeni döndürmez (yalnız state +
autoAdsEnabled). Kesin bayrak sadece konsolda. Watcher `state` dışında auto-ads sinyalini
yakalar (kapalı = güçlü ipucu) ve regresyonda konsola yönlendirir.

SINIR (dürüst): salt-okunur. İçerik ÜRETMEZ/yayınlamaz, yeniden-inceleme TETİKLEMEZ
(AdSense API yok) — bunlar insan/editöryel iş. Ajan = takip + öneri + ilk-onay anını yakala.

Auth: adsense-oauth-setup.py'nin ürettiği user-OAuth (adsense.readonly); seo-gsc.py'nin
kanıtlanmış OAuth client'ı reuse edilir (kod tekrarı yok).
Env: ADSENSE_OAUTH_CLIENT, ADSENSE_OAUTH_TOKEN, ADSENSE_ACCOUNT.
Cron: haftalık (Pzt 09:00). OUTCOME marker → cron_outcomes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

# seo-gsc.py'yi yol-ile yükle (tire içerir) → OAuth client + _post_json + _envget reuse.
_GSC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seo-gsc.py")
_spec = importlib.util.spec_from_file_location("seo_gsc", _GSC_PATH)
gsc = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(gsc)  # type: ignore[union-attr]

API_BASE = os.environ.get("API_BASE", "http://localhost:8420")
ADSENSE_BASE = "https://adsense.googleapis.com/v2"
STATE_FILE = os.environ.get("ADSENSE_STATE_FILE", "/opt/linux-ai-server/data/adsense-readiness-state.json")
CLAUDE_TIMEOUT = int(os.environ.get("ADSENSE_CLAUDE_TIMEOUT", "120"))

# İçerik-hazırlık eşikleri (env-tunable; AdSense "düşük değerli içerik" reddine karşı proxy).
MIN_PAGES = int(os.environ.get("ADSENSE_MIN_PAGES", "15"))
MIN_HOME_CHARS = int(os.environ.get("ADSENSE_MIN_HOME_CHARS", "2000"))
UA = "Mozilla/5.0 (AdSense-Readiness-Audit)"

# Güven-sayfası adayları (locale-aware: kök + /tr + /en denenecek).
TRUST = {
    "hakkimizda": ["/hakkimizda", "/hakkinda", "/about", "/about-us"],
    "iletisim": ["/iletisim", "/contact", "/contact-us"],
    "gizlilik": ["/gizlilik", "/gizlilik-politikasi", "/privacy", "/privacy-policy"],
}
LOCALE_PREFIXES = ["", "/tr", "/en"]


# ── saf fonksiyonlar (test edilebilir, ağ yok) ──────────────────────────


def visible_text(html: str) -> str:
    """Görünür GÖVDE metni: script/style/noscript İÇERİĞİ ve <head> tamamen çıkarılır,
    kalan tag'ler düşürülüp boşluk normalize edilir. Hem içerik-derinliği ölçümü hem de
    kalite-LLM'ine giden örnek BUNDAN üretilir (tek-kaynak) — aksi halde ölçüm gövdeyi,
    LLM ise head-script'lerini görür ve SPA'larda yanlış 'ince içerik' verdiği çıkar."""
    h = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    h = re.sub(r"(?is)<head[^>]*>.*?</head>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", h).strip()


def text_len(html: str) -> int:
    """Görünür-gövde metni uzunluğu (içerik-derinliği proxy'si)."""
    return len(visible_text(html))


def has_snippet(html: str, pub: str) -> bool:
    """AdSense reklam kodu sayfada var mı (adsbygoogle/googlesyndication veya pub-ID)."""
    return bool(re.search(r"adsbygoogle|googlesyndication", html)) or (bool(pub) and pub in html)


def ads_txt_ok(body: str, status: int | None, pub: str) -> bool:
    """ads.txt doğru servis ediliyor mu: HTTP 200 + doğru pub-ID DIRECT satırı (redirect değil)."""
    if status != 200 or not pub:
        return False
    return bool(re.search(rf"{re.escape(pub)}\s*,\s*DIRECT", body, re.IGNORECASE))


def readiness_checklist(audit: dict[str, Any]) -> dict[str, Any]:
    """Denetim verisi → hazırlık-checklist + skor + somut eksik-listesi (saf)."""
    gaps: list[str] = []
    if audit.get("pages", 0) < MIN_PAGES:
        gaps.append(f"az içerik sayfası ({audit.get('pages', 0)}<{MIN_PAGES}) — özgün içerik katmanı ekle")
    if audit.get("home_chars", 0) < MIN_HOME_CHARS:
        gaps.append(f"anasayfa ince ({audit.get('home_chars', 0)} char) — açıklayıcı içerik ekle")
    for key, label in (("hakkimizda", "Hakkımızda"), ("iletisim", "İletişim"), ("gizlilik", "Gizlilik")):
        if not audit.get("trust", {}).get(key):
            gaps.append(f"{label} sayfası YOK — ekle (AdSense güven şartı)")
    if not audit.get("ads_txt"):
        gaps.append("ads.txt eksik/yanlış/redirect — kökte 200 + doğru pub-ID DIRECT olmalı")
    if not audit.get("snippet"):
        gaps.append("AdSense reklam kodu sayfada YOK")
    # 6 kontrol: sayfa-sayısı, anasayfa-derinlik, 3 güven sayfası, ads.txt+snippet birleşik.
    # Skor = geçen kontrol sayısı (gaps her biri 1 eksik). 6/6 = içerik-tarafı hazır.
    checks_total = 6
    score = max(0, checks_total - len(gaps))
    return {"score": score, "total": checks_total, "gaps": gaps, "ready": not gaps}


# AdSense durum sıralaması (kötü→iyi). 'good' = READY'e DOĞRU hareket (iyileşme),
# 'bad' = READY'den UZAK (regresyon). Bilinmeyen durum = en-düşük (0).
_STATE_RANK: dict[str, int] = {"NEEDS_ATTENTION": 0, "REQUIRES_REVIEW": 0, "GETTING_READY": 1, "READY": 2}


# auto-ads'in KAPALI olması yalnız bu durumlarda anlamlı-sinyal (aksi halde manuel-reklam/
# opt-out meşru bir seçim → gürültü). Codex-P2 (#329): READY/GETTING_READY siteyi işaretleme.
_PROBLEM_STATES: frozenset[str] = frozenset({"NEEDS_ATTENTION", "REQUIRES_REVIEW"})


def _entry_state(v: Any) -> str | None:
    """Girdiden (dict {state,..} | legacy str | None) state-string'i çıkar."""
    s = v.get("state") if isinstance(v, dict) else v
    return s if isinstance(s, str) else None


def detect_state_changes(prev: dict[str, Any], cur: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """Önceki↔şimdiki AdSense durumları → geçişler (saf). YÖN-DUYARLI: rank artışı (READY'e
    yaklaşma) = onay (good), rank düşüşü = regresyon (bad). prev hem yeni {domain: {state,..}}
    hem legacy {domain: "STATE"} formatını kabul eder (geriye-uyumlu).

    ESKİ HATA: good = (state==READY) → NEEDS_ATTENTION→GETTING_READY (problem-durumundan
    re-review'e = İYİLEŞME) yanlışça 'bad'/regresyon sayılıyordu (sahte-alarm #1146/#1147)."""
    changes: list[dict[str, str]] = []
    for domain, info in cur.items():
        state = info["state"] if isinstance(info, dict) else info
        old = _entry_state(prev.get(domain))
        if old is None or old == state:
            continue
        good = _STATE_RANK.get(state, 0) > _STATE_RANK.get(old, 0)
        changes.append({"domain": domain, "from": old, "to": state, "kind": "good" if good else "bad"})
    return changes


def detect_auto_ads_drops(prev: dict[str, Any], cur: dict[str, dict[str, Any]]) -> list[str]:
    """auto-ads True→False düşen domainler (state-değişiminden BAĞIMSIZ izlenir → Codex-P2 #329:
    sinyal artık gerçek-watcher, yalnız rapor-kozmetiği değil). prev'de auto_ads bilinmiyorsa
    (None/legacy string) alarm YOK — ilk-gözlemde sahte-düşüş üretme (gürültü önle)."""
    drops: list[str] = []
    for domain, info in cur.items():
        cur_auto = info.get("auto_ads", False) if isinstance(info, dict) else False
        old = prev.get(domain)
        old_auto = old.get("auto_ads") if isinstance(old, dict) else None
        if old_auto is True and cur_auto is False:
            drops.append(domain)
    return drops


def pending_auto_ads_drops(prev: dict[str, Any], cur: dict[str, dict[str, Any]], changes: list[dict[str, str]]) -> list[str]:
    """Ayrı auto-ads-düşüşü alarmı gerektiren domainler = düşüş var AMA REGRESYON (bad) geçişiyle
    çakışmıyor. Regresyon-discovery'si auto-ads'i kendi notunda zaten kapsar; İYİLEŞEN (good) veya
    state-değişmeyen geçiş kapsamaz → aksi halde düşüş kalıcı-görünmez olur (Codex-P2 re-review #329)."""
    regressed = {c["domain"] for c in changes if c.get("kind") == "bad"}
    return [d for d in detect_auto_ads_drops(prev, cur) if d not in regressed]


# ── ağ / I/O ────────────────────────────────────────────────────────────


def _adsense_get(token: str, path: str) -> dict[str, Any]:
    req = urllib.request.Request(f"{ADSENSE_BASE}/{path}", headers={"Authorization": f"Bearer {token}"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        out: dict[str, Any] = json.loads(resp.read().decode() or "{}")
    return out


def _acquire_adsense_token() -> tuple[str, str]:
    """ADSENSE_OAUTH_CLIENT + ADSENSE_OAUTH_TOKEN → access_token (seo-gsc OAuth reuse)."""
    cpath = gsc._envget("ADSENSE_OAUTH_CLIENT")
    tpath = gsc._envget("ADSENSE_OAUTH_TOKEN")
    if not cpath or not tpath:
        return "", "ADSENSE_OAUTH_CLIENT/TOKEN env yok (adsense-oauth-setup.py çalıştır)"
    try:
        with open(cpath) as fh:
            client = json.load(fh)
        with open(tpath) as fh:
            rt = json.load(fh).get("refresh_token", "")
        if not rt:
            return "", "refresh_token boş"
        return gsc.get_access_token_oauth(client, rt), ""
    except Exception as e:  # noqa: BLE001
        return "", str(e)[:120]


def _fetch(url: str, timeout: int = 12) -> tuple[int | None, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})  # noqa: S310
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001
        return None, ""


def fetch_sites(token: str, account: str) -> dict[str, dict[str, Any]]:
    """AdSense hesabındaki siteler → {domain: {state, auto_ads}}. Codex P2: nextPageToken ile
    sayfalama (>50 site olan hesapta eksik çekmeyi önle).

    NOT (#1326): AdSense Management API v2 Site kaynağı düşürme-NEDENİ döndürmez — alanlar
    yalnız name/reportingDimensionId/domain/state/autoAdsEnabled. Eski `reason`/`approvalState`
    lookup ölüydü (asla dolmuyordu). Kesin bayrak (low-value-content/ads.txt/policy) yalnız
    AdSense konsolunda. `state` dışında tek ayırt-edici makine-sinyali = autoAdsEnabled
    (proto3 false'u atlar → alan yoksa auto-ads KAPALI)."""
    sites: dict[str, dict[str, Any]] = {}
    page_token = ""
    for _ in range(20):  # güvenlik üst-sınırı (≤1000 site); sonsuz-döngü koruması
        path = f"{account}/sites?pageSize=50"
        if page_token:
            path += f"&pageToken={page_token}"
        data = _adsense_get(token, path)
        for s in data.get("sites", []):
            if s.get("domain"):
                sites[s["domain"]] = {
                    "state": s.get("state", "STATE_UNSPECIFIED"),
                    "auto_ads": bool(s.get("autoAdsEnabled", False)),
                }
        page_token = data.get("nextPageToken", "")
        if not page_token:
            break
    return sites


def audit_site(domain: str, pub: str) -> dict[str, Any]:
    """Tek site içerik denetimi (ağ): sitemap, anasayfa, güven sayfaları, ads.txt, snippet."""
    _, sm = _fetch(f"https://{domain}/sitemap.xml")
    locs = re.findall(r"<loc>(.*?)</loc>", sm)
    pages = len(locs)
    hs, home = _fetch(f"https://{domain}/")
    home_chars = text_len(home) if hs == 200 else 0
    # İÇERİK ÖRNEKLE (PR#118 dersi + Codex: ana sayfa ≠ site içeriği). SaaS ana sayfası
    # landing/satış-kopyasıdır; editöryel içerik /blog,/rehberler'de. Quality-note ana
    # sayfayı görüp hep "ince" sanıyordu → sitemap'ten gerçek bir makale çek, onu değerlendir.
    content_url, content_sample = "", ""
    # NOT: 'rehberler' önce (alternation longest-first) → /rehberler/<slug> eşleşir; /rehber/ değil.
    _art_re = r"/(blog|rehberler|rehber|makale|article|guide|post|haber|3d-baski|sorun-cozumleri|anleitungen|guides)/"
    arts = [u for u in locs if re.search(_art_re, u, re.I)]
    for u in arts[:4]:
        cs, ch = _fetch(u)
        if cs == 200 and text_len(ch) > 800:
            content_url, content_sample = u, visible_text(ch)[:3000]
            break
    trust: dict[str, bool] = {}
    for key, paths in TRUST.items():
        found = False
        for pre in LOCALE_PREFIXES:
            for p in paths:
                st, _ = _fetch(f"https://{domain}{pre}{p}")
                if st == 200:
                    found = True
                    break
            if found:
                break
        trust[key] = found
    ax_s, ax_b = _fetch(f"https://{domain}/ads.txt")
    return {
        "domain": domain,
        "pages": pages,
        "home_chars": home_chars,
        "home_html": home if hs == 200 else "",
        "content_url": content_url,
        "content_sample": content_sample,
        "content_pages": len(arts),
        "trust": trust,
        "ads_txt": ads_txt_ok(ax_b, ax_s, pub),
        "snippet": has_snippet(home, pub) if hs == 200 else False,
    }


def quality_note(domain: str, home_html: str, content_sample: str = "", content_url: str = "") -> str:
    """/claude ile SİTE-GENELİ içerik-kalite notu (best-effort; 3d-labx/PR#118 dersi: metrik
    yetmez, özgünlük önemli + ana sayfa ≠ site içeriği). Hata/timeout → boş döner."""
    ikey = gsc._envget("INTERNAL_API_KEY")
    if not ikey or text_len(home_html) < 200:
        return ""
    home_snip = visible_text(home_html)[:1500]
    if content_sample:
        # Editöryel içerik VAR → onu birincil değerlendir; ana sayfa landing'i normal say.
        prompt = (
            f"{domain} sitesini AdSense 'düşük değerli içerik' reddi açısından değerlendir. "
            "Ana sayfa çoğu SaaS'ta landing/satış kopyasıdır — bu TEK BAŞINA sorun değil; "
            "asıl soru sitenin EDİTÖRYEL içeriğinin (blog/rehber makaleleri) özgün ve doyurucu olup "
            "olmadığı. Aşağıda ana sayfa ÖZETİ + GERÇEK BİR MAKALE örneği var. Makale özgün/doyurucu "
            f"mu? AdSense'e hazır mı? 1 cümle verdict + en kritik 2 eksik (varsa). Kısa, dürüst.\n\n"
            f"--- ANA SAYFA (landing) ---\n{home_snip}\n\n--- MAKALE ({content_url}) ---\n{content_sample}"
        )
    else:
        prompt = (
            f"{domain} sitesinin içeriği aşağıda (sitemap'te makale/blog sayfası BULUNAMADI — "
            "editöryel içerik yok olabilir). AdSense 'düşük değerli içerik' açısından değerlendir: "
            "özgün/doyurucu mu yoksa ince/şablon mı? 1 cümle verdict + en kritik 2 eksik. Kısa, dürüst.\n\n"
            f"İÇERİK:\n{visible_text(home_html)[:2500]}"
        )
    try:
        out = gsc._post_json(
            f"{API_BASE}/api/v1/claude/run",
            {"prompt": prompt, "read_only": True, "max_turns": 1},
            {"X-API-Key": ikey},
            CLAUDE_TIMEOUT,
        )
        return (out.get("result") or "").strip()[:400]
    except Exception:  # noqa: BLE001
        return ""


def _norm_state_entry(v: Any) -> dict[str, Any]:
    """State-dosyası girdisini {state, auto_ads} sözlüğüne normalize et. Legacy format
    {domain: "STATE"} (string) → auto_ads=None (bilinmiyor → düşüş-alarmı tetiklemez)."""
    if isinstance(v, dict):
        return {"state": v.get("state", "STATE_UNSPECIFIED"), "auto_ads": v.get("auto_ads")}
    return {"state": v, "auto_ads": None}


def _load_state() -> dict[str, dict[str, Any]]:
    """Kayıtlı durumu {domain: {state, auto_ads}} olarak döndür. Codex-P2 (#329): auto_ads
    da persist edilir → koşumlar-arası karşılaştırılabilir (yalnız rapor-satırında kozmetik değil)."""
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return {}
    return {d: _norm_state_entry(v) for d, v in data.items()}


def _save_state(state: dict[str, Any]) -> None:
    """Durum dosyasına yaz — {domain: {state, auto_ads}}. {domain: dict} veya legacy {domain: str}
    kabul eder; her ikisini de {state, auto_ads} sözlüğüne düzleştirir (auto_ads persist edilir)."""
    flat = {d: _norm_state_entry(info) for d, info in state.items()}
    parent = os.path.dirname(STATE_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(flat, fh)


def build_report(sites: dict[str, dict[str, Any]], audits: dict[str, dict[str, Any]], changes: list[dict[str, str]]) -> str:
    out = ["📈 AdSense Hazırlık Denetçisi — monetizasyon durumu\n"]
    if changes:
        out.append("🔔 DURUM DEĞİŞİMİ:")
        for c in changes:
            mark = "✅" if c["kind"] == "good" else "⚠️"
            out.append(f"  {mark} {c['domain']}: {c['from']} → {c['to']}")
        out.append("")
    for domain, info in sites.items():
        state = info["state"] if isinstance(info, dict) else info
        auto_ads = info.get("auto_ads", False) if isinstance(info, dict) else False
        a = audits.get(domain, {})
        cl: dict[str, Any] = readiness_checklist(a) if a else {"gaps": ["denetlenemedi"], "score": 0, "total": 6}
        flag = "🟢" if state == "READY" else "🔴"
        # auto-ads KAPALI'yı yalnız PROBLEM-state'te vurgula (Codex-P2 #329): READY/GETTING_READY
        # sitede auto-ads-off meşru config-seçimi (manuel-reklam/opt-out) → yanlış-alarm yapma.
        ads_flag = " | ⚠️ auto-ads KAPALI" if (not auto_ads and state in _PROBLEM_STATES) else ""
        out.append(f"{flag} {domain} — durum: {state}{ads_flag} | hazırlık: {cl['score']}/{cl['total']}")
        ax = "✓" if a.get("ads_txt") else "✗"
        sn = "✓" if a.get("snippet") else "✗"
        out.append(f"   sayfa:{a.get('pages', '?')} anasayfa:{a.get('home_chars', '?')}c ads.txt:{ax} snippet:{sn}")
        for g in cl["gaps"]:
            out.append(f"   • {g}")
        if a.get("quality"):
            out.append(f"   🧠 kalite: {a['quality']}")
        out.append("")
    return "\n".join(out).strip()


def _write_discovery(title: str, details: str, dtype: str = "learning") -> str:
    mkey = gsc._envget("MEMORY_API_KEY")
    if not mkey:
        return "no MEMORY_API_KEY"
    try:
        gsc._post_json(
            f"{API_BASE}/api/v1/memory/discoveries",
            {
                "device_name": "klipper",
                "project": "linux-ai-server",
                "type": dtype,
                "title": title,
                "details": details[:3800],
                "rationale": "adsense-readiness.py — AdSense durum+içerik denetçisi (salt-okunur, mail yok).",
            },
            {"X-Memory-Key": mkey},
            15,
        )
        return ""
    except Exception as e:  # noqa: BLE001
        return str(e)[:150]


def main() -> int:
    token, err = _acquire_adsense_token()
    if err:
        print(f"OUTCOME: fail | AdSense kimlik: {err}")
        return 0
    account = gsc._envget("ADSENSE_ACCOUNT")
    if not account:
        print("OUTCOME: fail | ADSENSE_ACCOUNT env yok")
        return 0
    pub = account.split("pub-")[-1] if "pub-" in account else ""
    pub = f"pub-{pub}" if pub else ""

    try:
        sites = fetch_sites(token, account)
    except Exception as e:  # noqa: BLE001
        print(f"OUTCOME: fail | sites.list: {str(e)[:120]}")
        return 0
    if not sites:
        print("OUTCOME: partial | AdSense hesabında site yok")
        return 0

    audits: dict[str, dict[str, Any]] = {}
    for domain in sites:
        try:
            a = audit_site(domain, pub)
            a["quality"] = quality_note(domain, a.pop("home_html", ""), a.get("content_sample", ""), a.get("content_url", ""))
            audits[domain] = a
        except Exception as e:  # noqa: BLE001
            audits[domain] = {"domain": domain, "gaps": [f"denetlenemedi: {str(e)[:60]}"]}

    prev = _load_state()
    changes = detect_state_changes(prev, sites)
    report = build_report(sites, audits, changes)
    print(report)

    derr = _write_discovery(f"AdSense hazırlık ({len(sites)} site)", report)
    # durum-değişimi → ayrı, yüksek-sinyal discovery (type=bug → SessionStart).
    # Codex P2: alert yazımı FAIL olursa o site için state'i İLERLETME (prev'de bırak)
    # → sonraki koşu değişimi yeniden algılar, alert sessizce kaybolmaz.
    # alert-FAIL geri-alımı ALAN-BAĞIMSIZ (Codex-P2 #329): state-alert fail → yalnız state, drop-alert
    # fail → yalnız auto_ads geri alınır. Tüm-girdi-revert, aynı koşumda BAŞARILI olan diğer high-signal
    # discovery'yi sonraki koşuda tekrar ettiriyordu (state↑ + auto_ads↓ çakışmasında ONAY-duplikasyonu).
    save_state: dict[str, Any] = {d: dict(_norm_state_entry(info)) for d, info in sites.items()}
    for c in changes:
        kind = "ONAY" if c["kind"] == "good" else "REGRESYON"
        a = audits.get(c["domain"], {})
        info = sites.get(c["domain"], {})
        auto_ads = info.get("auto_ads", False) if isinstance(info, dict) else False
        if c["kind"] == "good":
            detail_suffix = "Reklam serve etmeye başladı olabilir — gelir izle."
        else:
            # API v2 düşürme-nedeni vermez (#1326) → kesin bayrak sadece konsolda.
            # Elimizdeki tek makine-sinyali auto-ads: kapalıysa gözlem (Codex-P2 #329: kasıtlı
            # manuel-reklam/opt-out olabilir → "re-enable" DAYATMA, yalnız kontrol öner).
            auto_note = "" if auto_ads else " Gözlem: auto-ads KAPALI (kasıtlı-manuel değilse konsolda kontrol et)."
            # (b) stale ads.txt FP'yi ayırt et: canlı HTTP check ile teyit
            if "ads_txt" in a:
                if a["ads_txt"]:
                    ads_note = "ads.txt canlı-HTTP: SAĞLAM (konsol stale olabilir — Google re-check bekle)"
                else:
                    ads_note = "ads.txt canlı-HTTP: SORUNLU — köke HTTP 200 + pub-ID DIRECT satırı gerekli"
            else:
                ads_note = "ads.txt denetlenemedi"
            detail_suffix = f"Reklam durdu/red — API neden-vermez, konsolda sebep kontrol et.{auto_note} {ads_note}"
        werr = _write_discovery(
            f"AdSense {kind}: {c['domain']} {c['from']}→{c['to']}",
            f"AdSense site durumu değişti: {c['domain']} {c['from']} → {c['to']}. {detail_suffix}",
            dtype="bug",
        )
        if werr and c["domain"] in prev:
            # state-alert yazılamadı → YALNIZ state'i prev'e geri al (auto_ads'e dokunma) → sonraki
            # koşu state-değişimini re-algılar; aynı koşumda başarılı auto-ads alarmı tekrarlanmaz.
            save_state[c["domain"]]["state"] = _entry_state(prev[c["domain"]])

    # Codex-P2 (#329): auto-ads düşüşü (True→False), yalnız REGRESYON geçişiyle çakışmayanlar
    # (regresyon-notu auto-ads'i zaten kapsar; iyileşen/değişmeyen kapsamaz → düşüş kaybolmasın).
    ads_drops = pending_auto_ads_drops(prev, sites, changes)
    change_by_domain = {c["domain"]: c for c in changes}
    for domain in ads_drops:
        # Codex-P2 (#329): mesaj GERÇEK state-bağlamını versin. ads_drops REGRESYON'u dışlar ama
        # İYİLEŞEN geçişi (good) tutar → o domainlerde "state sabit" YALAN olur; gerçek geçişi yaz.
        ch = change_by_domain.get(domain)
        state_ctx = f"state {ch['from']}→{ch['to']} (iyileşme)" if ch else f"state sabit: {_entry_state(sites.get(domain))}"
        werr = _write_discovery(
            f"AdSense auto-ads KAPANDI: {domain}",
            f"{domain} auto-ads açıkken kapandı ({state_ctx}). "
            "Google flaglerken kapatmış ya da konsolda değişmiş olabilir; kasıtlı-manuel-reklam "
            "değilse konsolda kontrol et.",
            dtype="bug",
        )
        if werr and domain in prev:
            # drop-alert yazılamadı → YALNIZ auto_ads'i prev'e (True) geri al (state'e dokunma) →
            # sonraki koşu düşüşü re-algılar; başarılı state/ONAY discovery'si tekrarlanmaz.
            save_state[domain]["auto_ads"] = _norm_state_entry(prev[domain]).get("auto_ads")
    _save_state(save_state)

    ready = sum(1 for info in sites.values() if (info.get("state") if isinstance(info, dict) else info) == "READY")
    parts = []
    if changes:
        parts.append(f"{len(changes)} durum-değişimi")
    if ads_drops:
        parts.append(f"{len(ads_drops)} auto-ads-düşüşü")
    note = ", ".join(parts) if parts else "değişim yok"
    if derr:
        print(f"\nOUTCOME: partial | {len(sites)} site ({ready} READY), {note}, DISCOVERY-FAIL: {derr}")
    else:
        print(f"\nOUTCOME: pass | {len(sites)} site ({ready} READY), {note} → ortak-hafıza (mail yok)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
