"""scripts/seo-audit.py — deterministik teknik-SEO denetimi.

Ağ yok: _fetch/_status monkeypatch'li. Skorlama + bulgu-mantığı + SPA-tespiti test edilir.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("seo_audit", ROOT / "scripts" / "seo-audit.py")
seo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seo)

GOOD = (
    '<html lang="tr"><head>'
    "<title>Bilge English · ücretsiz CEFR seviye testi 5 dakika</title>"
    '<meta name="description" content="' + ("x" * 140) + '">'
    '<link rel="canonical" href="https://en.bilgearena.com">'
    '<meta name="viewport" content="width=device-width">'
    '<meta property="og:title" content="Bilge English">'
    '<meta property="og:image" content="https://en.bilgearena.com/og.png">'
    '<meta name="twitter:card" content="summary_large_image">'
    '<script type="application/ld+json">{}</script>'
    "</head><body><h1>İngilizce seviye testi</h1>"
    "<p>" + ("Gerçek içerik. " * 60) + "</p></body></html>"
)

SPA_SHELL = (
    '<html lang="tr"><head>'
    "<title>Panola — İşletme Yönetim Sistemi</title>"
    '<meta name="description" content="İşletmenizi tek ekrandan yönetin sipariş üretim randevu stok crm">'
    '<link rel="canonical" href="https://panola.app">'
    '<meta name="viewport" content="width=device-width">'
    '<meta property="og:title" content="Panola">'
    '<meta property="og:image" content="/panola-icon.svg">'
    '<meta name="twitter:card" content="summary">'
    '</head><body><div id="root"></div></body></html>'
)


def _patch(monkeypatch, html, robots=200, sitemap=200):
    monkeypatch.setattr(seo, "_fetch", lambda url: (200, html))

    def _st(url):
        if url.endswith("robots.txt"):
            return robots
        if url.endswith("sitemap.xml"):
            return sitemap
        return 200

    monkeypatch.setattr(seo, "_status", _st)


def test_good_page_scores_high(monkeypatch):
    _patch(monkeypatch, GOOD)
    r = seo.audit_domain("en.bilgearena.com")
    assert r["ok"] is True
    assert r["h1"] == 1
    assert r["jsonld"] == 1
    assert r["score"] >= 90, f"iyi sayfa yüksek skor almalı: {r['score']} {r['findings']}"
    sevs = {s for s, _ in r["findings"]}
    assert "P1" not in sevs  # iyi sayfada P1 olmamalı


def test_spa_shell_flags_p1(monkeypatch):
    _patch(monkeypatch, SPA_SHELL)
    r = seo.audit_domain("panola.app")
    msgs = " ".join(m for _, m in r["findings"])
    assert "SPA-kabuğu" in msgs  # düşük SSR-metin → P1
    assert r["h1"] == 0
    assert "JSON-LD" in msgs  # yapısal-veri yok
    assert "og:image" in msgs  # svg/relatif → uygun değil
    assert r["score"] < 80  # SPA + eksikler skoru düşürür


def test_noindex_is_critical(monkeypatch):
    html = SPA_SHELL.replace("<head>", '<head><meta name="robots" content="noindex,follow">')
    _patch(monkeypatch, html)
    r = seo.audit_domain("x.example")
    assert any(s == "P1" and "noindex" in m for s, m in r["findings"])


def test_missing_robots_sitemap(monkeypatch):
    _patch(monkeypatch, GOOD, robots=404, sitemap=404)
    r = seo.audit_domain("en.bilgearena.com")
    msgs = " ".join(m for _, m in r["findings"])
    assert "robots.txt" in msgs
    assert "sitemap.xml" in msgs


def test_unreachable_domain(monkeypatch):
    monkeypatch.setattr(seo, "_fetch", lambda url: (0, ""))
    r = seo.audit_domain("dead.example")
    assert r["ok"] is False
    assert r["score"] == 0
    assert r["findings"][0][0] == "P1"


def test_report_orders_worst_first(monkeypatch):
    results = [
        {"domain": "good.com", "ok": True, "status": 200, "score": 92, "vtext": 5000, "h1": 1, "jsonld": 2, "findings": []},
        {
            "domain": "bad.com",
            "ok": True,
            "status": 200,
            "score": 45,
            "vtext": 30,
            "h1": 0,
            "jsonld": 0,
            "findings": [("P1", "SPA-kabuğu")],
        },
    ]
    rep = seo.build_report(results)
    assert rep.index("bad.com") < rep.index("good.com")  # kötü-skor önce
    assert "🔴" in rep
    assert "🟢" in rep
