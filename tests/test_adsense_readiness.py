"""adsense-readiness.py saf fonksiyon testleri (ağ yok)."""

from __future__ import annotations

import importlib.util
import os

_P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "adsense-readiness.py")
_spec = importlib.util.spec_from_file_location("adsense_readiness", _P)
ar = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(ar)  # type: ignore[union-attr]


def test_text_len_strips_script_style():
    html = "<html><head><style>x{}</style><script>var a=1</script></head><body>Merhaba dünya içerik</body></html>"
    n = ar.text_len(html)
    assert "Merhaba dünya içerik".replace(" ", "") not in str(n)  # sayı döner
    assert 15 < n < 40  # sadece görünür metin


def test_visible_text_excludes_head_scripts():
    # Regresyon: SPA'larda <head> script-yığını (consent/GTM/AdSense/tema) gövdeden ÖNCE
    # gelir. Eski snippet yalnız tag-strip yapıyor, script İÇERİĞİNİ tutuyordu -> ilk 2500
    # char hep head-JS oluyordu -> kalite-LLM'i "içerik yok, sadece script" sanıp yanlış
    # 'ince içerik' / 'AdSense hazır değil' veriyordu (3d-labx false-negative).
    html = (
        "<html><head>"
        "<script>window.gtag=function(){};var consent={ad_storage:'denied'};"
        "loadAdSense('pub-123');theme='dark';</script>"
        "<style>body{margin:0}</style><title>Yarım</title></head>"
        "<body><h1>3D Baskı Rehberi</h1>"
        "<p>Filament türleri ve flow kalibrasyonu üzerine özgün makale.</p></body></html>"
    )
    text = ar.visible_text(html)
    # Görünür gövde metni gelmeli
    assert "3D Baskı Rehberi" in text
    assert "Filament türleri" in text
    # Script/JS içeriği SIZMAMALI
    assert "gtag" not in text
    assert "loadAdSense" not in text
    assert "ad_storage" not in text
    # Snippet gövdeyle başlamalı (head-JS değil) — bug'ın tam tersi
    assert text.lstrip().startswith("3D Baskı")


def test_has_snippet():
    assert ar.has_snippet('<script src="...googlesyndication..."></script>', "pub-123")
    assert ar.has_snippet("<ins class='adsbygoogle'></ins>", "")
    assert ar.has_snippet("<div>pub-5103156785085864</div>", "pub-5103156785085864")
    assert not ar.has_snippet("<html>boş</html>", "pub-999")


def test_ads_txt_ok():
    good = "google.com, pub-5103156785085864, DIRECT, f08c47fec0942fa0"
    assert ar.ads_txt_ok(good, 200, "pub-5103156785085864")
    assert not ar.ads_txt_ok(good, 307, "pub-5103156785085864")  # redirect
    assert not ar.ads_txt_ok("Redirecting...", 200, "pub-5103156785085864")  # yanlış içerik
    assert not ar.ads_txt_ok(good, 200, "")  # pub yok


def test_readiness_checklist_full_ready():
    audit = {
        "pages": 50,
        "home_chars": 5000,
        "trust": {"hakkimizda": True, "iletisim": True, "gizlilik": True},
        "ads_txt": True,
        "snippet": True,
    }
    cl = ar.readiness_checklist(audit)
    assert cl["ready"] is True
    assert cl["score"] == 6
    assert cl["gaps"] == []


def test_readiness_checklist_thin_app():
    # bilgearena gibi: az sayfa, ince anasayfa, güven var, ads.txt+snippet var
    audit = {
        "pages": 5,
        "home_chars": 800,
        "trust": {"hakkimizda": True, "iletisim": True, "gizlilik": True},
        "ads_txt": True,
        "snippet": True,
    }
    cl = ar.readiness_checklist(audit)
    assert not cl["ready"]
    assert cl["score"] == 4  # 2 eksik (sayfa + anasayfa)
    assert any("içerik sayfası" in g for g in cl["gaps"])


def test_readiness_checklist_missing_trust():
    audit = {"pages": 50, "home_chars": 5000, "trust": {}, "ads_txt": True, "snippet": True}
    cl = ar.readiness_checklist(audit)
    assert cl["score"] == 3  # 3 güven sayfası eksik
    assert any("Hakkımızda" in g for g in cl["gaps"])


def test_detect_state_changes():
    prev = {"a.com": "NEEDS_ATTENTION", "b.com": "READY", "c.com": "READY"}
    cur = {
        "a.com": {"state": "READY", "reason": ""},
        "b.com": {"state": "READY", "reason": ""},
        "c.com": {"state": "NEEDS_ATTENTION", "reason": "low-value-content"},
        "d.com": {"state": "REQUIRES_REVIEW", "reason": ""},
    }
    changes = ar.detect_state_changes(prev, cur)
    by = {c["domain"]: c for c in changes}
    assert by["a.com"]["kind"] == "good"  # onay
    assert by["c.com"]["kind"] == "bad"  # regresyon
    assert "b.com" not in by  # değişmedi
    assert "d.com" not in by  # yeni (prev'de yok) → değişim sayılmaz


def test_detect_state_changes_empty_prev():
    # ilk koşu: önceki durum yok → değişim raporlanmaz (gürültü önle)
    assert ar.detect_state_changes({}, {"a.com": {"state": "READY", "reason": ""}}) == []
