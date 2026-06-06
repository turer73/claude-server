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


def _patch(monkeypatch, html, robots_status=200, robots_body="User-agent: *\n", sitemap_ok=True):
    def _f(url):
        if url.endswith("robots.txt"):
            return (robots_status, robots_body)
        return (200, html)

    def _st(url):
        if "sitemap" in url:  # /sitemap.xml veya /sitemap-index.xml
            return 200 if sitemap_ok else 404
        return 200

    monkeypatch.setattr(seo, "_fetch", _f)
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


def test_noindex_reversed_attr_order(monkeypatch):
    """Codex P2: <meta content="noindex" name="robots"> (TERS sıra) da yakalanmalı —
    aksi halde deindex'li sayfa 'temiz' sanılır (en kötü SEO-miss)."""
    html = SPA_SHELL.replace("<head>", '<head><meta content="noindex,follow" name="robots">')
    _patch(monkeypatch, html)
    r = seo.audit_domain("x.example")
    assert any(s == "P1" and "noindex" in m for s, m in r["findings"])


def test_meta_helper_order_independent():
    """_meta content↔name sırasından bağımsız; _link_href rel↔href bağımsız."""
    assert seo._meta('<meta content="özet" name="description">', "name", "description") == "özet"
    assert seo._meta('<meta name="description" content="özet">', "name", "description") == "özet"
    assert seo._link_href('<link href="https://x/c" rel="canonical">', "canonical") == "https://x/c"


def test_status_falls_back_to_get_on_head_405(monkeypatch):
    """Codex P2: HEAD 405/403 → GET ile yeniden dene (false 'erişilemiyor' önle)."""

    def _raise_405(req, timeout=0):
        raise seo.urllib.error.HTTPError(req.full_url, 405, "Method Not Allowed", {}, None)

    monkeypatch.setattr(seo.urllib.request, "urlopen", _raise_405)
    monkeypatch.setattr(seo, "_fetch", lambda url: (200, "ok"))
    assert seo._status("https://x.example/robots.txt") == 200


def test_missing_robots_sitemap(monkeypatch):
    _patch(monkeypatch, GOOD, robots_status=404, robots_body="", sitemap_ok=False)
    r = seo.audit_domain("en.bilgearena.com")
    msgs = " ".join(m for _, m in r["findings"])
    assert "robots.txt" in msgs
    assert "sitemap" in msgs


def test_sitemap_index_not_false_flagged(monkeypatch):
    """False-positive fix: /sitemap.xml 404 ama /sitemap-index.xml 200 → sitemap VAR sayılmalı."""

    def _st(url):
        if url.endswith("sitemap.xml"):
            return 404
        if url.endswith("sitemap-index.xml"):
            return 200
        return 200

    monkeypatch.setattr(seo, "_status", _st)
    assert seo._has_sitemap("https://x.example", "User-agent: *\n") is True


def test_sitemap_via_robots_directive(monkeypatch):
    """robots.txt 'Sitemap:' direktifi reachable → sitemap VAR (path standart olmasa da)."""
    monkeypatch.setattr(seo, "_status", lambda url: 200 if "custom-sitemap" in url else 404)
    body = "User-agent: *\nSitemap: https://x.example/custom-sitemap.xml\n"
    assert seo._has_sitemap("https://x.example", body) is True


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
