#!/usr/bin/env python3
"""SEO Ajanı — Plausible Analytics bağlantısı (gerçek-trafik verisi → davranış bulguları).

seo-gsc.py arama-TALEP tarafını çeker (sorgu/CTR/pozisyon/index — Google Search Console);
bu script gerçek-DAVRANIŞ tarafını çeker (ziyaretçi/sayfa-görüntüleme/bounce/süre/trafik-kaynağı
— Plausible). İkisi birleşince bulgu zenginleşir: ör. yüksek-gösterim-düşük-CTR bir sayfanın
bounce'u DÜŞÜKSE sorun snippet (başlık/meta), YÜKSEKSE landing/intent uyumsuzluğu → fix doğru
yere gider.

AUTH: Plausible Stats API v2 (POST /api/v2/query), Bearer API-key (.env'den PLAUSIBLE_3DLABX_KEY,
secret commit'siz). Account Settings > API Keys'ten üretilir; hesabın sahip olduğu tüm site'leri
kapsar. Salt-okunur. google-* / plausible-* kütüphanesi GEREKMEZ (urllib).

Kullanım: seo-plausible.py [site_id...]   (default PLAUSIBLE_SITES; ör. 'bilgearena.com')
HATALAR Telegram YERİNE ortak-hafızaya (type=bug → SessionStart'ta görünür → açılan oturumda
düzeltilir) yazılır — mail/Telegram yok (seo-gsc ile aynı desen).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

ENV_FILE = os.environ.get("NOTIFY_ENV_FILE", "/opt/linux-ai-server/.env")
API_BASE = os.environ.get("API_BASE", "http://localhost:8420")
DAYS = int(os.environ.get("PLAUSIBLE_DAYS", "28"))  # GSC ile hizalı (seo-gsc DAYS=28)

# Bu instance'ın (analytics.3d-labx.com) kapsadığı site'ler — anahtar hepsine erişir.
DEFAULT_SITES = [
    "bilgearena.com",
    "3d-labx.com",
    "kokenakademi.com",
    "renderhane.com",
    "panola.app",
]

# Organik-arama kaynakları (Plausible visit:source etiketleri) — organik-pay hesabı için.
ORGANIC_SOURCES = {"google", "bing", "yahoo!", "yahoo", "duckduckgo", "yandex.com.tr", "yandex", "ecosia"}


def _envget(key: str) -> str:
    v = os.environ.get(key)
    if v:
        return v
    try:
        with open(ENV_FILE) as fh:
            for line in fh:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def _date_range() -> list[str]:
    """[start, end] — son DAYS gün (GSC ile aynı pencere). Plausible açık-tarih dizisi."""
    from datetime import UTC, datetime, timedelta

    end = datetime.now(UTC).date()
    start = end - timedelta(days=DAYS)
    return [str(start), str(end)]


def _query(url: str, key: str, body: dict, timeout: int = 30) -> dict:
    """Plausible Stats API v2 — POST /api/v2/query, Bearer auth."""
    data = json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode() or "{}")


# ── Saf analiz fonksiyonları (mock'la test edilir) ──────────────────────────


def analyze_pages(rows: list[dict]) -> list[tuple[str, str]]:
    """Top-sayfa satırları (dimensions=[page], metrics=[visitors,bounce_rate,visit_duration]) →
    yüksek-bounce / düşük-etkileşim bulguları. Eşikler muhafazakâr (gürültü-az):
    bounce ≥%80 + ≥50 ziyaretçi = landing/intent uyumsuzluğu sinyali."""
    f: list[tuple[str, str]] = []
    for r in rows:
        dims = r.get("dimensions") or []
        mets = r.get("metrics") or []
        if not dims or len(mets) < 3:
            continue
        page = dims[0]
        vis = mets[0] or 0
        bounce = mets[1] or 0
        dur = mets[2] or 0
        if vis >= 50 and bounce >= 80:
            f.append(("P2", f"{page}: bounce %{bounce} ({vis} ziyaretçi, ort {dur:.0f}s) → landing/intent uyumsuz ya da yavaş, incele"))
        elif vis >= 50 and dur < 15:
            f.append(("P3", f"{page}: düşük etkileşim (ort {dur:.0f}s, {vis} ziyaretçi) → içerik/iç-link güçlendir"))
    return f


def analyze_sources(rows: list[dict], total_visitors: int) -> list[tuple[str, str]]:
    """Kaynak satırları (dimensions=[source], metrics=[visitors]) → organik-pay.
    Düşük organik pay = SEO büyüme fırsatı (yeterli toplam trafik varsa)."""
    f: list[tuple[str, str]] = []
    if total_visitors < 50:
        return f
    organic = 0
    for r in rows:
        dims = r.get("dimensions") or []
        mets = r.get("metrics") or []
        if not dims or not mets:
            continue
        if dims[0].strip().lower() in ORGANIC_SOURCES:
            organic += mets[0] or 0
    share = (organic / total_visitors * 100) if total_visitors else 0
    if share < 25:
        f.append(
            ("P3", f"organik-arama payı %{share:.0f} (toplam {total_visitors} ziyaretçi) → SEO büyüme fırsatı, içerik/anahtar-kelime artır")
        )
    return f


# ── Çekim + denetim ─────────────────────────────────────────────────────────


def audit_site(url: str, key: str, site: str) -> dict:
    dr = _date_range()
    findings: list[tuple[str, str]] = []
    qurl = f"{url}/api/v2/query"
    visitors = pageviews = 0
    bounce = dur = 0.0
    # 1) Aggregate (site-geneli özet)
    try:
        agg = _query(
            qurl,
            key,
            {
                "site_id": site,
                "metrics": ["visitors", "pageviews", "bounce_rate", "visit_duration"],
                "date_range": dr,
            },
        )
        res = (agg.get("results") or [{}])[0].get("metrics") or [0, 0, 0, 0]
        visitors, pageviews, bounce, dur = res[0] or 0, res[1] or 0, res[2] or 0, res[3] or 0
    except Exception as e:
        findings.append(("P1", f"Plausible aggregate çekilemedi: {str(e)[:100]}"))
        return {"site": site, "visitors": 0, "pageviews": 0, "bounce": 0, "duration": 0, "findings": findings}
    # 2) Top sayfalar (bounce/süre)
    try:
        pg = _query(
            qurl,
            key,
            {
                "site_id": site,
                "metrics": ["visitors", "bounce_rate", "visit_duration"],
                "date_range": dr,
                "dimensions": ["event:page"],
                "order_by": [["visitors", "desc"]],
                "pagination": {"limit": 10},
            },
        )
        findings += analyze_pages(pg.get("results", []))
    except Exception as e:
        findings.append(("P2", f"Plausible sayfa-verisi çekilemedi: {str(e)[:100]}"))
    # 3) Trafik kaynakları (organik pay)
    try:
        src = _query(
            qurl,
            key,
            {
                "site_id": site,
                "metrics": ["visitors"],
                "date_range": dr,
                "dimensions": ["visit:source"],
                "order_by": [["visitors", "desc"]],
                "pagination": {"limit": 12},
            },
        )
        findings += analyze_sources(src.get("results", []), visitors)
    except Exception as e:
        findings.append(("P3", f"Plausible kaynak-verisi çekilemedi: {str(e)[:100]}"))
    return {"site": site, "visitors": visitors, "pageviews": pageviews, "bounce": bounce, "duration": dur, "findings": findings}


def build_report(results: list[dict]) -> str:
    lines = [f"📊 Plausible Analytics — Trafik Denetimi ({DAYS}g)\n"]
    for r in results:
        p1 = sum(1 for s, _ in r["findings"] if s == "P1")
        emoji = "🔴" if p1 else ("🟡" if r["findings"] else "🟢")
        head = f"{emoji} {r['site']} — {r['visitors']} ziyaretçi / {r['pageviews']} görüntüleme"
        lines.append(f"{head} · bounce %{r['bounce']:.0f} · ort {r['duration']:.0f}s")
        for sev, msg in r["findings"][:12]:
            lines.append(f"   [{sev}] {msg}")
        if not r["findings"]:
            lines.append("   ✓ belirgin trafik/davranış sorunu yok")
        lines.append("")
    return "\n".join(lines).strip()


def _post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode() or "{}")


def _write_bug(site: str, findings: list[tuple[str, str]]) -> str:
    """Hata/fırsat içeren site → type=bug discovery (SessionStart-görünür). Telegram/mail YOK.
    Dedup: 'Plausible: <site>' başlığı (seo-gsc 'GSC: <prop>' deseniyle paralel)."""
    mkey = _envget("MEMORY_API_KEY")
    if not mkey:
        return "no MEMORY_API_KEY"
    body = "📊 Trafik/davranış bulguları (seo-plausible):\n" + "\n".join(f"[{s}] {m}" for s, m in findings)
    try:
        _post_json(
            f"{API_BASE}/api/v1/memory/discoveries",
            {
                "device_name": "klipper",
                "project": "linux-ai-server",
                "type": "bug",
                "title": f"Plausible: {site}",
                "details": body[:3800],
                "rationale": "seo-plausible.py — Telegram yok; düzeltme açılan oturumda (ortak-hafıza).",
            },
            {"X-Memory-Key": mkey},
            15,
        )
        return ""
    except Exception as e:
        return str(e)[:150]


def main() -> int:
    url = _envget("PLAUSIBLE_3DLABX_URL")
    key = _envget("PLAUSIBLE_3DLABX_KEY")
    if not url or not key:
        print("OUTCOME: fail | PLAUSIBLE_3DLABX_URL + PLAUSIBLE_3DLABX_KEY gerekli (.env)")
        return 0

    sites = sys.argv[1:] or (_envget("PLAUSIBLE_SITES").split(",") if _envget("PLAUSIBLE_SITES") else DEFAULT_SITES)
    sites = [s.strip() for s in sites if s.strip()]
    results = [audit_site(url, key, s) for s in sites]
    report = build_report(results)
    print(report)

    # Bulgular (P1+P2) → ortak hafıza (type=bug → SessionStart). MAIL/Telegram YOK.
    raised, errs = 0, []
    for r in results:
        actionable = [(s, m) for s, m in r["findings"] if s in ("P1", "P2")]
        if actionable:
            e = _write_bug(r["site"], actionable)
            errs.append(e) if e else None
            raised += 0 if e else 1
    if errs:
        print(f"\nOUTCOME: partial | {len(sites)} site, {raised} bug→ortak-hafıza, MEMORY-FAIL: {errs[0]}")
    else:
        print(f"\nOUTCOME: pass | {len(sites)} site, {raised} bug→ortak-hafıza (SessionStart, mail yok)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
