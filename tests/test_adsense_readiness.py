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
    prev = {"a.com": "NEEDS_ATTENTION", "b.com": "READY", "c.com": "READY", "e.com": "NEEDS_ATTENTION", "f.com": "GETTING_READY"}
    cur = {
        "a.com": {"state": "READY", "auto_ads": True},
        "b.com": {"state": "READY", "auto_ads": True},
        "c.com": {"state": "NEEDS_ATTENTION", "auto_ads": False},
        "d.com": {"state": "REQUIRES_REVIEW", "auto_ads": False},
        "e.com": {"state": "GETTING_READY", "auto_ads": True},  # iyileşme (re-review) — eskiden yanlış 'bad'
        "f.com": {"state": "NEEDS_ATTENTION", "auto_ads": False},  # kötüleşme
    }
    changes = ar.detect_state_changes(prev, cur)
    by = {c["domain"]: c for c in changes}
    assert by["a.com"]["kind"] == "good"  # NEEDS_ATTENTION→READY onay
    assert by["c.com"]["kind"] == "bad"  # READY→NEEDS_ATTENTION regresyon
    assert by["e.com"]["kind"] == "good"  # NEEDS_ATTENTION→GETTING_READY = İYİLEŞME (#1146/#1147 fix)
    assert by["f.com"]["kind"] == "bad"  # GETTING_READY→NEEDS_ATTENTION regresyon
    assert "b.com" not in by  # değişmedi
    assert "d.com" not in by  # yeni (prev'de yok) → değişim sayılmaz


def test_detect_state_changes_empty_prev():
    # ilk koşu: önceki durum yok → değişim raporlanmaz (gürültü önle)
    assert ar.detect_state_changes({}, {"a.com": {"state": "READY", "auto_ads": True}}) == []


def test_fetch_sites_extracts_auto_ads(monkeypatch):
    # #1326: API v2 Site kaynağı reason döndürmez — tek ayırt-edici sinyal autoAdsEnabled.
    # proto3 false'u atlar → alan YOKSA auto-ads KAPALI kabul edilmeli.
    fake = {
        "sites": [
            {"domain": "on.com", "state": "GETTING_READY", "autoAdsEnabled": True},
            {"domain": "off.com", "state": "NEEDS_ATTENTION"},  # autoAdsEnabled alanı YOK → False
            {"domain": "x.com"},  # state bile yok → STATE_UNSPECIFIED, auto_ads False
        ]
    }
    monkeypatch.setattr(ar, "_adsense_get", lambda token, path: fake)
    sites = ar.fetch_sites("tok", "accounts/pub-1")
    assert sites["on.com"] == {"state": "GETTING_READY", "auto_ads": True}
    assert sites["off.com"] == {"state": "NEEDS_ATTENTION", "auto_ads": False}
    assert sites["x.com"] == {"state": "STATE_UNSPECIFIED", "auto_ads": False}
    # eski ölü alanlar sızmamalı
    assert "reason" not in sites["off.com"]


def test_build_report_flags_auto_ads_off():
    # auto-ads KAPALI yalnız PROBLEM-state'te vurgulanmalı, açık olan gürültü yapmamalı.
    sites = {
        "off.com": {"state": "NEEDS_ATTENTION", "auto_ads": False},
        "on.com": {"state": "GETTING_READY", "auto_ads": True},
    }
    report = ar.build_report(sites, audits={}, changes=[])
    off_line = next(ln for ln in report.splitlines() if ln.startswith("🔴 off.com"))
    on_line = next(ln for ln in report.splitlines() if ln.startswith("🔴 on.com"))
    assert "auto-ads KAPALI" in off_line
    assert "auto-ads KAPALI" not in on_line


def test_build_report_no_auto_ads_warning_when_not_problem_state():
    # Codex-P2 (#329): READY/GETTING_READY sitede auto-ads-off meşru config (manuel/opt-out) →
    # YANLIŞ-ALARM yapma. Yalnız NEEDS_ATTENTION/REQUIRES_REVIEW'da uyar.
    sites = {
        "ready.com": {"state": "READY", "auto_ads": False},  # off ama sorun-değil
        "getting.com": {"state": "GETTING_READY", "auto_ads": False},  # off ama sorun-değil
        "flagged.com": {"state": "REQUIRES_REVIEW", "auto_ads": False},  # off + problem → uyar
    }
    report = ar.build_report(sites, audits={}, changes=[])
    lines = {ln.split(" — ")[0].split()[-1]: ln for ln in report.splitlines() if " — durum:" in ln}
    assert "auto-ads KAPALI" not in lines["ready.com"]
    assert "auto-ads KAPALI" not in lines["getting.com"]
    assert "auto-ads KAPALI" in lines["flagged.com"]


def test_state_roundtrip_persists_auto_ads(tmp_path, monkeypatch):
    # Codex-P2 (#329): auto_ads state-dosyasında persist edilmeli (koşumlar-arası karşılaştırma).
    sf = tmp_path / "state.json"
    monkeypatch.setattr(ar, "STATE_FILE", str(sf))
    ar._save_state({"a.com": {"state": "GETTING_READY", "auto_ads": True}})
    loaded = ar._load_state()
    assert loaded == {"a.com": {"state": "GETTING_READY", "auto_ads": True}}


def test_load_state_legacy_string_format(tmp_path, monkeypatch):
    # Geriye-uyumluluk: eski {domain: "STATE"} formatı → auto_ads=None (bilinmiyor, düşüş-alarmı tetiklemez).
    sf = tmp_path / "state.json"
    sf.write_text('{"a.com": "READY", "b.com": "NEEDS_ATTENTION"}')
    monkeypatch.setattr(ar, "STATE_FILE", str(sf))
    loaded = ar._load_state()
    assert loaded == {"a.com": {"state": "READY", "auto_ads": None}, "b.com": {"state": "NEEDS_ATTENTION", "auto_ads": None}}


def test_detect_auto_ads_drops():
    prev = {
        "drop.com": {"state": "GETTING_READY", "auto_ads": True},  # True→False = düşüş
        "stay.com": {"state": "READY", "auto_ads": True},  # değişmez
        "legacy.com": "GETTING_READY",  # legacy: auto_ads bilinmiyor → alarm YOK
        "off2on.com": {"state": "READY", "auto_ads": False},  # False→True = düşüş değil
    }
    cur = {
        "drop.com": {"state": "GETTING_READY", "auto_ads": False},
        "stay.com": {"state": "READY", "auto_ads": True},
        "legacy.com": {"state": "GETTING_READY", "auto_ads": False},
        "off2on.com": {"state": "READY", "auto_ads": True},
        "new.com": {"state": "READY", "auto_ads": False},  # prev'de yok → alarm YOK
    }
    assert ar.detect_auto_ads_drops(prev, cur) == ["drop.com"]


def test_pending_auto_ads_drops_filters_only_regressions():
    # Codex-P2 re-review (#329): düşüş REGRESYON'la çakışırsa bastır (regresyon-notu kapsar);
    # İYİLEŞEN veya state-değişmeyen geçişte düşüş AYRI alarm almalı (yoksa kalıcı-görünmez).
    prev = {
        "reg.com": {"state": "GETTING_READY", "auto_ads": True},  # bad geçiş + düşüş → bastır
        "imp.com": {"state": "NEEDS_ATTENTION", "auto_ads": True},  # good geçiş + düşüş → ALARM
        "flat.com": {"state": "READY", "auto_ads": True},  # değişmez + düşüş → ALARM
    }
    cur = {
        "reg.com": {"state": "NEEDS_ATTENTION", "auto_ads": False},
        "imp.com": {"state": "GETTING_READY", "auto_ads": False},
        "flat.com": {"state": "READY", "auto_ads": False},
    }
    changes = ar.detect_state_changes(prev, cur)
    pending = ar.pending_auto_ads_drops(prev, cur, changes)
    assert "reg.com" not in pending  # regresyon-notu zaten auto-ads'i kapsıyor
    assert "imp.com" in pending  # ONAY-mesajı auto-ads'ten bahsetmez → ayrı alarm ŞART
    assert "flat.com" in pending  # state değişmedi → ayrı alarm


def test_detect_state_changes_accepts_dict_prev():
    # Yeni persist formatı: prev artık {domain: {state, auto_ads}} — state-değişimi hâlâ doğru.
    prev = {"a.com": {"state": "NEEDS_ATTENTION", "auto_ads": False}}
    cur = {"a.com": {"state": "READY", "auto_ads": True}}
    changes = ar.detect_state_changes(prev, cur)
    assert changes == [{"domain": "a.com", "from": "NEEDS_ATTENTION", "to": "READY", "kind": "good"}]


def test_main_field_independent_rollback_on_drop_alert_fail(monkeypatch):
    # Codex-P2 re-review (#329): site AYNI koşumda state-iyileşir + auto_ads düşerse ve state-alert
    # BAŞARILI / drop-alert BAŞARISIZ olursa → yalnız auto_ads geri alınmalı (state DEĞİL). Aksi halde
    # başarılı ONAY sonraki koşuda tekrar eder. Alan-bağımsız rollback'i canlı main()-yolunda doğrula.
    monkeypatch.setattr(ar, "_acquire_adsense_token", lambda: ("tok", ""))
    monkeypatch.setattr(ar.gsc, "_envget", lambda k: "accounts/pub-1" if k == "ADSENSE_ACCOUNT" else "x")
    monkeypatch.setattr(ar, "fetch_sites", lambda t, a: {"imp.com": {"state": "GETTING_READY", "auto_ads": False}})
    monkeypatch.setattr(ar, "audit_site", lambda d, p: {"ads_txt": True, "snippet": True, "pages": 30, "home_chars": 3000})
    monkeypatch.setattr(ar, "quality_note", lambda *a, **k: "")
    # prev: bad-state + auto_ads AÇIK → cur'da state-iyileşme (good) + auto_ads düşüşü ÇAKIŞIR
    monkeypatch.setattr(ar, "_load_state", lambda: {"imp.com": {"state": "NEEDS_ATTENTION", "auto_ads": True}})
    saved = {}
    monkeypatch.setattr(ar, "_save_state", lambda s: saved.update(s))

    # ONAY/özet discovery BAŞARILI, yalnız auto-ads-KAPANDI discovery'si transient FAIL
    written = []

    def fake_write(title, details, dtype="learning"):
        written.append((title, details))
        return "boom" if "auto-ads KAPANDI" in title else ""

    monkeypatch.setattr(ar, "_write_discovery", fake_write)
    ar.main()
    # state CURRENT kalmalı (GETTING_READY) → başarılı ONAY tekrar ETMEZ;
    # auto_ads prev'e (True) geri alınmalı → düşüş sonraki koşu re-algılanır.
    assert saved["imp.com"] == {"state": "GETTING_READY", "auto_ads": True}
    # Codex-P2 (#329): iyileşen-geçişte drop-mesajı "state sabit" DEMEMELİ, gerçek geçişi yazmalı.
    drop_detail = next(d for t, d in written if "auto-ads KAPANDI" in t)
    assert "NEEDS_ATTENTION→GETTING_READY" in drop_detail
    assert "state sabit" not in drop_detail
