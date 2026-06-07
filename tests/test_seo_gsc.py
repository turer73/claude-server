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
    assert any("striking-distance" in m for _, m in f)


def test_search_high_impression_low_ctr_flagged():
    rows = [{"keys": ["salon yazılımı"], "impressions": 500, "ctr": 0.005, "position": 3.0}]
    f = gsc.analyze_search(rows)
    assert any("CTR" in m for _, m in f)


def test_search_healthy_query_not_flagged():
    rows = [{"keys": ["panola"], "impressions": 300, "ctr": 0.35, "position": 1.2}]
    assert gsc.analyze_search(rows) == []


def test_sitemap_missing_and_errors():
    assert any("GÖNDERİLMEMİŞ" in m for _, m in gsc.analyze_sitemaps([]))
    errf = gsc.analyze_sitemaps([{"path": "https://x/sitemap.xml", "errors": 3}])
    assert any(s == "P1" and "HATA" in m for s, m in errf)


def test_sitemap_clean_no_finding():
    assert gsc.analyze_sitemaps([{"path": "https://x/sitemap.xml", "errors": 0, "warnings": 0}]) == []


def test_inspection_non_pass_verdict_is_p1():
    res = {"inspectionResult": {"indexStatusResult": {"verdict": "FAIL", "coverageState": "Excluded"}}}
    f = gsc.analyze_inspection(res, "https://x/page")
    assert any(s == "P1" for s, _ in f)


def test_inspection_indexed_ok():
    res = {"inspectionResult": {"indexStatusResult": {"verdict": "PASS", "coverageState": "Submitted and indexed"}}}
    assert gsc.analyze_inspection(res, "https://x/page") == []


def test_build_report_orders_and_marks():
    results = [{"property": "sc-domain:x", "clicks": 10, "impressions": 1000, "findings": [("P1", "sitemap hata")]}]
    rep = gsc.build_report(results)
    assert "sc-domain:x" in rep
    assert "🔴" in rep


def test_write_bug_type_bug_dedup_and_no_telegram():
    """Hatalar Telegram yerine type=bug discovery (SessionStart); _send_telegram kaldırıldı."""
    assert not hasattr(gsc, "_send_telegram")


def test_write_bug_posts_bug(monkeypatch):
    monkeypatch.setattr(gsc, "_envget", lambda k: "mk" if k == "MEMORY_API_KEY" else "")
    cap = {}
    monkeypatch.setattr(gsc, "_post_json", lambda url, body, h, t: cap.update(body) or {})
    assert gsc._write_bug("sc-domain:panola.app", [("P1", "sitemap hata")]) == ""
    assert cap["type"] == "bug"
    assert cap["title"] == "GSC: sc-domain:panola.app"


def test_main_no_key_short_circuits(monkeypatch, capsys):
    monkeypatch.setattr(gsc, "_envget", lambda k: "")
    rc = gsc.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "OUTCOME: fail" in out
    assert "GSC_SA_KEY_PATH" in out
