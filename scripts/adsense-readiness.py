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


def detect_state_changes(prev: dict[str, str], cur: dict[str, str]) -> list[dict[str, str]]:
    """Önceki↔şimdiki AdSense durumları → geçişler (saf). READY'e dönüş = onay (good),
    READY'den çıkış/yeni-NEEDS_ATTENTION = regresyon (bad)."""
    changes: list[dict[str, str]] = []
    for domain, state in cur.items():
        old = prev.get(domain)
        if old is None or old == state:
            continue
        good = state == "READY"
        changes.append({"domain": domain, "from": old, "to": state, "kind": "good" if good else "bad"})
    return changes


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


def fetch_sites(token: str, account: str) -> dict[str, str]:
    """AdSense hesabındaki siteler → {domain: state}. Codex P2: nextPageToken ile
    sayfalama (>50 site olan hesapta eksik çekmeyi önle)."""
    sites: dict[str, str] = {}
    page_token = ""
    for _ in range(20):  # güvenlik üst-sınırı (≤1000 site); sonsuz-döngü koruması
        path = f"{account}/sites?pageSize=50"
        if page_token:
            path += f"&pageToken={page_token}"
        data = _adsense_get(token, path)
        for s in data.get("sites", []):
            if s.get("domain"):
                sites[s["domain"]] = s.get("state", "STATE_UNSPECIFIED")
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


def _load_state() -> dict[str, str]:
    try:
        with open(STATE_FILE) as fh:
            data: dict[str, str] = json.load(fh)
        return data
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict[str, str]) -> None:
    parent = os.path.dirname(STATE_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh)


def build_report(sites: dict[str, str], audits: dict[str, dict[str, Any]], changes: list[dict[str, str]]) -> str:
    out = ["📈 AdSense Hazırlık Denetçisi — monetizasyon durumu\n"]
    if changes:
        out.append("🔔 DURUM DEĞİŞİMİ:")
        for c in changes:
            mark = "✅" if c["kind"] == "good" else "⚠️"
            out.append(f"  {mark} {c['domain']}: {c['from']} → {c['to']}")
        out.append("")
    for domain, state in sites.items():
        a = audits.get(domain, {})
        cl: dict[str, Any] = readiness_checklist(a) if a else {"gaps": ["denetlenemedi"], "score": 0, "total": 6}
        flag = "🟢" if state == "READY" else "🔴"
        out.append(f"{flag} {domain} — durum: {state} | hazırlık: {cl['score']}/{cl['total']}")
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
    save_state = dict(sites)
    for c in changes:
        kind = "ONAY" if c["kind"] == "good" else "REGRESYON"
        werr = _write_discovery(
            f"AdSense {kind}: {c['domain']} {c['from']}→{c['to']}",
            f"AdSense site durumu değişti: {c['domain']} {c['from']} → {c['to']}. "
            + (
                "Reklam serve etmeye başladı olabilir — gelir izle."
                if c["kind"] == "good"
                else "Reklam durdu/red — konsolda sebep kontrol et."
            ),
            dtype="bug",
        )
        if werr and c["domain"] in prev:
            save_state[c["domain"]] = prev[c["domain"]]  # alert yazılamadı → eski state koru
    _save_state(save_state)

    ready = sum(1 for s in sites.values() if s == "READY")
    note = f"{len(changes)} durum-değişimi" if changes else "değişim yok"
    if derr:
        print(f"\nOUTCOME: partial | {len(sites)} site ({ready} READY), {note}, DISCOVERY-FAIL: {derr}")
    else:
        print(f"\nOUTCOME: pass | {len(sites)} site ({ready} READY), {note} → ortak-hafıza (mail yok)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
