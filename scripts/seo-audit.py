#!/usr/bin/env python3
"""SEO Ajanı — çok-domain teknik-SEO denetimi (deterministik, salt-okunur).

Multi-uzman vizyon 3/4 (SEO). data-analyst kardeşi AMA /claude KULLANMAZ — SEO
kontrolleri kural-tabanlı (title/meta/h1/JSON-LD/OG/canonical/SSR-içerik/robots/
sitemap) → LLM yerine deterministik script: ucuz, tekrarlanabilir, ağ-dışı bağımsız.

Her domain: HTML + robots.txt + sitemap.xml çek → sinyalleri çıkar → 0-100 skor +
önceliklendirilmiş bulgular (P1/P2/P3). Çıktı: discovery (SessionStart) + Telegram özet.

Kullanım:
  seo-audit.py                      # default flagship domain listesi
  seo-audit.py panola.app kuafor.panola.app   # belirli domainler
On-demand (cron yok). Salt-okunur HTTP GET — yan etki yok.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://localhost:8420")
ENV_FILE = os.environ.get("NOTIFY_ENV_FILE", "/opt/linux-ai-server/.env")
TG_HELPER = os.environ.get("SEO_TG_HELPER", "/opt/linux-ai-server/automation/telegram-alert.sh")
TIMEOUT = int(os.environ.get("SEO_FETCH_TIMEOUT", "15"))
UA = "Mozilla/5.0 (compatible; klipper-seo-audit/1.0)"

DEFAULT_DOMAINS = [
    "panola.app",
    "kuafor.panola.app",
    "petvet.panola.app",
    "en.bilgearena.com",
]


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


def _fetch(url: str) -> tuple[int, str]:
    """(status, body). Hata → (0, '')."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def _status(url: str) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
            return resp.status
    except urllib.error.HTTPError as e:
        # Codex P2: sunucu HEAD'i reddedebilir (405/403/501) → GET ile yeniden dene,
        # yoksa erişilebilir robots/sitemap'i "yok" sanarız (false-positive).
        if e.code in (403, 405, 501):
            return _fetch(url)[0]
        return e.code
    except Exception:
        return _fetch(url)[0]


def _attr(html: str, pattern: str) -> str | None:
    m = re.search(pattern, html, re.I | re.S)
    return m.group(1).strip() if m else None


def _tag_attrs(tag: str) -> dict[str, str]:
    """Bir HTML tag'inin attribute'larını sıra-bağımsız dict'e çevir (küçük-harf key)."""
    return {k.lower(): v for k, v in re.findall(r'([a-zA-Z:_-]+)\s*=\s*["\']([^"\']*)["\']', tag)}


def _meta(html: str, key: str, value: str) -> str | None:
    """<meta> tag'inde key(name/property)==value olanın content'ini döndür — ATTRIBUTE
    SIRASINDAN BAĞIMSIZ (Codex P2: ters-sıralı <meta content=.. name=robots> kaçırılmasın)."""
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        a = _tag_attrs(tag)
        if a.get(key, "").lower() == value.lower() and a.get("content"):
            return a["content"].strip()
    return None


def _link_href(html: str, rel: str) -> str | None:
    """<link rel=X href=...> href'i — sıra-bağımsız."""
    for tag in re.findall(r"<link\b[^>]*>", html, re.I):
        a = _tag_attrs(tag)
        if a.get("rel", "").lower() == rel.lower() and a.get("href"):
            return a["href"].strip()
    return None


def _visible_text_len(html: str) -> int:
    """script/style çıkar, tag-sök → görünür (SSR) metin uzunluğu. SPA-kabuğu tespiti."""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return len(t)


def audit_domain(domain: str) -> dict:
    base = f"https://{domain}"
    status, html = _fetch(base)
    if status == 0 or not html:
        return {
            "domain": domain,
            "ok": False,
            "status": status,
            "score": 0,
            "findings": [("P1", f"Erişilemedi / boş yanıt (HTTP {status})")],
        }

    title = _attr(html, r"<title[^>]*>(.*?)</title>")
    desc = _meta(html, "name", "description")
    canonical = _link_href(html, "canonical")
    viewport = bool(_meta(html, "name", "viewport"))
    lang = _attr(html, r'<html[^>]*\blang=["\'](.*?)["\']')
    og_title = _meta(html, "property", "og:title")
    og_image = _meta(html, "property", "og:image")
    tw_card = _meta(html, "name", "twitter:card")
    robots_meta = _meta(html, "name", "robots")
    h1 = len(re.findall(r"<h1[\s>]", html, re.I))
    jsonld = len(re.findall(r'type=["\']application/ld\+json["\']', html, re.I))
    hreflang = len(re.findall(r"hreflang=", html, re.I))
    vtext = _visible_text_len(html)
    robots_txt = _status(f"{base}/robots.txt")
    sitemap = _status(f"{base}/sitemap.xml")

    score = 100
    f: list[tuple[str, str]] = []

    # P1 — organik trafiği doğrudan vuran
    if robots_meta and "noindex" in robots_meta.lower():
        score -= 30
        f.append(("P1", "meta robots=noindex — sayfa indekslenmiyor! (kaza?)"))
    if vtext < 200:
        score -= 20
        f.append(("P1", f"SPA-kabuğu: render-öncesi görünür metin {vtext} char — crawler/sosyal boş görür → SSR/pre-render gerek"))
    if not title:
        score -= 15
        f.append(("P1", "<title> yok"))
    if og_image and (og_image.endswith(".svg") or not og_image.startswith("http")):
        score -= 6
        f.append(("P1", f"og:image paylaşıma uygun değil ({og_image}) → mutlak 1200×630 PNG/JPG"))

    # P2 — önemli ama dolaylı
    if not desc:
        score -= 12
        f.append(("P2", "meta description yok"))
    elif not (50 <= len(desc) <= 170):
        score -= 4
        f.append(("P2", f"meta description uzunluğu {len(desc)} (ideal 120-160)"))
    if h1 != 1:
        score -= 10
        f.append(("P2", f"H1 sayısı {h1} (tam 1 olmalı)"))
    if jsonld == 0:
        score -= 8
        f.append(("P2", "JSON-LD yapısal-veri yok (Organization/SoftwareApplication şeması ekle)"))
    if not canonical:
        score -= 6
        f.append(("P2", "canonical yok"))
    if not viewport:
        score -= 8
        f.append(("P2", "viewport meta yok (mobil)"))
    if sitemap >= 400 or sitemap == 0:
        score -= 5
        f.append(("P2", f"sitemap.xml erişilemiyor (HTTP {sitemap})"))
    if robots_txt >= 400 or robots_txt == 0:
        score -= 5
        f.append(("P2", f"robots.txt erişilemiyor (HTTP {robots_txt})"))

    # P3 — cila
    if title and not (10 <= len(title) <= 65):
        score -= 3
        f.append(("P3", f"title uzunluğu {len(title)} (ideal 30-60)"))
    if not lang:
        score -= 4
        f.append(("P3", "<html lang> yok"))
    if not og_title:
        score -= 3
        f.append(("P3", "og:title yok"))
    if tw_card == "summary":
        score -= 2
        f.append(("P3", "twitter:card=summary → summary_large_image (büyük önizleme)"))
    elif not tw_card:
        score -= 2
        f.append(("P3", "twitter:card yok"))

    return {
        "domain": domain,
        "ok": True,
        "status": status,
        "score": max(0, score),
        "title": title,
        "vtext": vtext,
        "h1": h1,
        "jsonld": jsonld,
        "hreflang": hreflang,
        "findings": f,
    }


def build_report(results: list[dict], days: str = "") -> str:
    lines = ["🔎 Teknik-SEO Denetimi\n"]
    for r in sorted(results, key=lambda x: x["score"]):
        if not r["ok"]:
            lines.append(f"❌ {r['domain']} — ERİŞİLEMEDİ (HTTP {r['status']})")
            continue
        emoji = "🔴" if r["score"] < 60 else ("🟡" if r["score"] < 80 else "🟢")
        lines.append(f"{emoji} {r['domain']} — skor {r['score']}/100 (SSR-metin {r['vtext']}c, h1={r['h1']}, json-ld={r['jsonld']})")
        for sev, msg in r["findings"]:
            lines.append(f"   [{sev}] {msg}")
        if not r["findings"]:
            lines.append("   ✓ temel sinyaller sağlam")
        lines.append("")
    return "\n".join(lines).strip()


def _post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode() or "{}")


def _send_telegram(report: str) -> bool:
    if not os.path.exists(TG_HELPER):
        return False
    safe = report.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = "🔎 <b>SEO Denetimi</b>\n<pre>" + safe[:3500] + "</pre>"
    try:
        r = subprocess.run([TG_HELPER, "--kind", "generic", "--text", text], capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def _save_discovery(report: str, n: int) -> str:
    mkey = _envget("MEMORY_API_KEY")
    if not mkey:
        return "no MEMORY_API_KEY"
    try:
        _post_json(
            f"{API_BASE}/api/v1/memory/discoveries",
            {
                "device_name": "klipper",
                "project": "linux-ai-server",
                "type": "learning",
                "title": "Teknik-SEO denetimi (seo-audit)",
                "details": f"🔎 {n} domain denetlendi:\n{report[:3800]}",
                "rationale": "seo-audit.py (on-demand, deterministik, salt-okunur).",
            },
            {"X-Memory-Key": mkey},
            15,
        )
        return ""
    except Exception as e:
        return str(e)[:150]


def main() -> int:
    domains = sys.argv[1:] or DEFAULT_DOMAINS
    results = [audit_domain(d) for d in domains]
    report = build_report(results)
    print(report)
    print()
    disc_err = _save_discovery(report, len(domains))
    tg = _send_telegram(report)
    avg = sum(r["score"] for r in results) // max(1, len(results))
    if disc_err:
        print(f"OUTCOME: partial | {len(domains)} domain, ort-skor {avg}, telegram={tg}, DISCOVERY-FAIL: {disc_err}")
    else:
        print(f"OUTCOME: pass | {len(domains)} domain, ort-skor {avg}, telegram={tg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
