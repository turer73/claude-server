"""scripts/ad-advisor.py — reklam-fırsat sınıflandırma mantığı (GSC'siz, saf fonksiyonlar).

Auth/HTTP/_ad_copy_llm test edilmez (canlı GSC + /claude gerektirir); deterministik
çekirdek (classify/_brand_token/build_strategy/build_report) test edilir.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ad_advisor", ROOT / "scripts" / "ad-advisor.py")
ad = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ad)


def test_brand_token_from_sc_domain():
    assert ad._brand_token("sc-domain:panola.app") == "panola"
    assert ad._brand_token("sc-domain:bilgearena.com") == "bilgearena"
    assert ad._brand_token("https://kuafor.panola.app/") == "kuafor"  # URL-prefix biçimi


def test_classify_brand_defense():
    # marka sorgusu, poz>3 → savunma
    rows = [{"keys": ["panola"], "impressions": 53, "ctr": 0.019, "position": 6.9}]
    b = ad.classify(rows, "panola")
    assert len(b["brand_defense"]) == 1
    assert b["brand_defense"][0]["q"] == "panola"


def test_classify_brand_at_top_not_flagged():
    # marka zaten poz≤3 → savunma gereksiz (hiçbir kovaya düşmez)
    rows = [{"keys": ["panola"], "impressions": 53, "ctr": 0.5, "position": 1.5}]
    b = ad.classify(rows, "panola")
    assert b["brand_defense"] == []


def test_classify_striking_distance():
    rows = [{"keys": ["kuaför randevu"], "impressions": 120, "ctr": 0.02, "position": 8.0}]
    b = ad.classify(rows, "kuafor")
    assert len(b["striking"]) == 1


def test_classify_high_demand_low_ctr():
    rows = [{"keys": ["arena yks"], "impressions": 121, "ctr": 0.017, "position": 2.0}]
    b = ad.classify(rows, "bilgearena")
    assert len(b["low_ctr"]) == 1


def test_classify_low_impression_ignored():
    rows = [{"keys": ["nadir sorgu"], "impressions": 3, "ctr": 0.0, "position": 9.0}]
    b = ad.classify(rows, "x")
    assert b["striking"] == []
    assert b["low_ctr"] == []
    assert b["brand_defense"] == []


def test_build_strategy_keywords_extracted():
    buckets = {
        "brand_defense": [{"q": "panola", "imp": 53, "pos": 6.9, "ctr": 0.019}],
        "striking": [{"q": "kuaför randevu", "imp": 120, "pos": 8.0, "ctr": 0.02}],
        "low_ctr": [],
    }
    lines, keywords = ad.build_strategy("sc-domain:panola.app", buckets)
    assert "panola" in keywords
    assert "kuaför randevu" in keywords
    assert any("Marka-savunma" in ln for ln in lines)


def test_build_report_no_opportunity():
    results = [{"property": "sc-domain:x.com", "lines": [], "keywords": [], "copy": "", "n_rows": 10}]
    rep = ad.build_report(results)
    assert "belirgin reklam-fırsatı yok" in rep
