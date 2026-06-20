"""scripts/seo-gsc.py — GSC analiz mantığı (gerçek-GSC'siz, mock).

Saf fonksiyonlar (analyze_search/sitemaps/inspection/build_report) + key-yok kısa-devre.
Auth/HTTP test edilmez (canlı GSC + SA-key gerektirir); analiz-kuralları test edilir.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("seo_gsc", ROOT / "scripts" / "seo-gsc.py")
gsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gsc)


def test_search_striking_distance_flagged():
    rows = [{"keys": ["kuaför randevu"], "impressions": 200, "ctr": 0.04, "position": 12.0}]
    f = gsc.analyze_search(rows)
    assert any("striking-distance" in m for _, _, m in f)
    assert all(k == "opportunity" for _, k, _ in f)  # arama bulguları FIRSAT (hata değil)


def test_search_high_impression_low_ctr_flagged():
    rows = [{"keys": ["salon yazılımı"], "impressions": 500, "ctr": 0.005, "position": 3.0}]
    f = gsc.analyze_search(rows)
    assert any("CTR" in m for _, _, m in f)
    assert all(k == "opportunity" for _, k, _ in f)


def test_search_healthy_query_not_flagged():
    rows = [{"keys": ["panola"], "impressions": 300, "ctr": 0.35, "position": 1.2}]
    assert gsc.analyze_search(rows) == []


def test_sitemap_missing_and_errors():
    miss = gsc.analyze_sitemaps([])
    assert any("GÖNDERİLMEMİŞ" in m for _, _, m in miss)
    errf = gsc.analyze_sitemaps([{"path": "https://x/sitemap.xml", "errors": 3}])
    assert any(s == "P1" and k == "error" and "HATA" in m for s, k, m in errf)  # sitemap = HATA


def test_sitemap_clean_no_finding():
    assert gsc.analyze_sitemaps([{"path": "https://x/sitemap.xml", "errors": 0, "warnings": 0}]) == []


def test_inspection_non_pass_verdict_is_p1_error():
    res = {"inspectionResult": {"indexStatusResult": {"verdict": "FAIL", "coverageState": "Excluded"}}}
    f = gsc.analyze_inspection(res, "https://x/page")
    assert any(s == "P1" and k == "error" for s, k, _ in f)  # coverage = HATA


def test_inspection_indexed_ok():
    res = {"inspectionResult": {"indexStatusResult": {"verdict": "PASS", "coverageState": "Submitted and indexed"}}}
    assert gsc.analyze_inspection(res, "https://x/page") == []


def test_property_root_derivation():
    assert gsc._property_root("sc-domain:bilgearena.com") == "https://bilgearena.com/"
    assert gsc._property_root("https://x.com/") == "https://x.com/"


def test_build_report_separates_errors_and_opportunities():
    results = [
        {
            "property": "sc-domain:x",
            "clicks": 10,
            "impressions": 1000,
            "findings": [("P1", "error", "sitemap hata"), ("P2", "opportunity", "CTR düşük")],
        }
    ]
    rep = gsc.build_report(results)
    assert "sc-domain:x" in rep
    assert "🔴" in rep
    assert "Hatalar" in rep  # hata bölümü
    assert "Fırsatlar" in rep  # fırsat bölümü ayrı


def test_no_legacy_send_telegram():
    """Eski _send_telegram yok; P1 bildirimleri _send_telegram_p1 ile yapılır."""
    assert not hasattr(gsc, "_send_telegram")
    assert hasattr(gsc, "_send_telegram_p1")


def test_send_telegram_p1_no_p1_returns_false():
    """P1 bulugusu yoksa Telegram gönderilmez."""
    results = [{"property": "sc-domain:x", "findings": [("P2", "opportunity", "P2 mesajı")], "clicks": 0, "impressions": 0}]
    assert gsc._send_telegram_p1(results) is False


def test_send_telegram_p1_opportunity_p1_not_sent():
    """FIRSAT asla Telegram üretmez (analyze_search P1 vermez ama kind-filtre garanti)."""
    results = [{"property": "sc-domain:x", "findings": [("P1", "opportunity", "kurgu")], "clicks": 0, "impressions": 0}]
    assert gsc._send_telegram_p1(results) is False


def test_send_telegram_p1_no_helper_returns_false(monkeypatch):
    """TG_HELPER dosyası yoksa False döner."""
    monkeypatch.setattr(gsc, "TG_HELPER", "/nonexistent/telegram-alert.sh")
    results = [{"property": "sc-domain:x", "findings": [("P1", "error", "sitemap hata")], "clicks": 0, "impressions": 0}]
    assert gsc._send_telegram_p1(results) is False


def test_write_findings_error_is_bug(monkeypatch):
    monkeypatch.setattr(gsc, "_envget", lambda k: "mk" if k == "MEMORY_API_KEY" else "")
    cap = {}
    monkeypatch.setattr(gsc, "_post_json", lambda url, body, h, t: cap.update(body) or {})
    assert gsc._write_findings("sc-domain:panola.app", [("P1", "sitemap hata")], "error") == ""
    assert cap["type"] == "bug"
    assert cap["title"] == "GSC hata: sc-domain:panola.app"


def test_write_findings_opportunity_is_learning(monkeypatch):
    """FIRSAT type=learning (bug listesini kirletmez), başlık ayrı."""
    monkeypatch.setattr(gsc, "_envget", lambda k: "mk" if k == "MEMORY_API_KEY" else "")
    cap = {}
    monkeypatch.setattr(gsc, "_post_json", lambda url, body, h, t: cap.update(body) or {})
    assert gsc._write_findings("sc-domain:x", [("P2", "CTR düşük")], "opportunity") == ""
    assert cap["type"] == "learning"
    assert cap["title"] == "GSC fırsatı: sc-domain:x"


def test_oauth_refresh_token_exchange(monkeypatch):
    """get_access_token_oauth: refresh_token → access_token (client.installed yapısı)."""
    monkeypatch.setattr(gsc, "_http", lambda url, data=None, headers=None, timeout=30: {"access_token": "AT123"})
    client = {"installed": {"client_id": "cid", "client_secret": "sec"}}
    assert gsc.get_access_token_oauth(client, "RT") == "AT123"


def test_acquire_token_prefers_oauth(monkeypatch, tmp_path):
    """OAuth client+token varsa SA yerine OAuth kullanılır."""
    cj = tmp_path / "client.json"
    cj.write_text('{"installed":{"client_id":"c","client_secret":"s"}}')
    tj = tmp_path / "token.json"
    tj.write_text('{"refresh_token":"RT"}')
    env = {"GSC_OAUTH_CLIENT": str(cj), "GSC_OAUTH_TOKEN": str(tj)}
    monkeypatch.setattr(gsc, "_envget", lambda k: env.get(k, ""))
    monkeypatch.setattr(gsc, "get_access_token_oauth", lambda c, r: "OAUTH_AT")
    monkeypatch.setattr(gsc, "get_access_token", lambda sa: "SA_AT")  # çağrılmamalı
    token, err = gsc._acquire_token()
    assert err == ""
    assert token == "OAUTH_AT"


def test_acquire_token_none_configured(monkeypatch):
    monkeypatch.setattr(gsc, "_envget", lambda k: "")
    token, err = gsc._acquire_token()
    assert token == ""
    assert "Kimlik yok" in err


def test_main_no_key_short_circuits(monkeypatch, capsys):
    monkeypatch.setattr(gsc, "_envget", lambda k: "")
    rc = gsc.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "OUTCOME: fail" in out
    assert "GSC_SA_KEY_PATH" in out
