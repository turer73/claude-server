"""scripts/ad-advisor.py — reklam-fırsat sınıflandırma mantığı (GSC'siz, saf fonksiyonlar).

Deterministik çekirdek (classify/_brand_token/build_strategy/build_report/_extract_json/
_validate_rsa_limits) saf-fonksiyon olarak test edilir. _ad_copy_llm/_critic_review'ın ağ-
çağrısı monkeypatch ile mock'lanır (canlı GSC/Claude gerektirmez, adsense-readiness.py
test deseniyle tutarlı)."""

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


def test_brand_token_hyphenated_normalized():
    # Codex P2: ayraçlı domain etiketi → ayraçsız normalize ('3d-labx' → '3dlabx')
    assert ad._brand_token("sc-domain:3d-labx.com") == "3dlabx"


def test_classify_brand_defense_hyphenated_match():
    # '3d labx' sorgusu, marka '3dlabx' → normalize sayesinde savunma kovasına düşer (Codex P2)
    rows = [{"keys": ["3d labx"], "impressions": 30, "ctr": 0.02, "position": 7.0}]
    b = ad.classify(rows, "3dlabx")
    assert len(b["brand_defense"]) == 1


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
    results = [{"property": "sc-domain:x.com", "lines": [], "keywords": [], "ads": [], "n_rows": 10}]
    rep = ad.build_report(results)
    assert "belirgin reklam-fırsatı yok" in rep


def test_extract_json_plain():
    assert ad._extract_json('[{"a": 1}]') == [{"a": 1}]


def test_extract_json_code_fence():
    # LLM'ler talimata rağmen sık sık ```json ... ``` sarmalı döner
    assert ad._extract_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]


def test_validate_rsa_limits_clean():
    ads = [{"keyword": "panola", "headlines": ["Kısa Başlık"], "descriptions": ["Kısa açıklama."]}]
    assert ad._validate_rsa_limits(ads) == []


def test_validate_rsa_limits_headline_too_long():
    long_h = "Bu Başlık Otuz Karakterden Kesinlikle Daha Uzun"
    ads = [{"keyword": "panola", "headlines": [long_h], "descriptions": []}]
    v = ad._validate_rsa_limits(ads)
    assert len(v) == 1
    assert "başlık" in v[0]
    assert str(len(long_h)) in v[0]


def test_validate_rsa_limits_description_too_long():
    long_d = "Bu açıklama doksan karakterden kesinlikle daha uzun olacak şekilde özenle uzatılmış bir cümledir, evet."
    ads = [{"keyword": "panola", "headlines": [], "descriptions": [long_d]}]
    v = ad._validate_rsa_limits(ads)
    assert len(v) == 1
    assert "açıklama" in v[0]


def test_build_report_renders_ads_violations_and_critic():
    results = [
        {
            "property": "sc-domain:panola.app",
            "lines": ["  🛡️ Marka-savunma:", "    • 'panola' — poz 4.0, 37 gösterim, CTR %2.7"],
            "keywords": ["panola"],
            "ads": [{"keyword": "panola", "headlines": ["Panola Resmi"], "descriptions": ["Kısa ve öz açıklama metni."]}],
            "violations": [],
            "critic": {"verdict": "APPROVED", "notes": ""},
            "n_rows": 5,
        }
    ]
    rep = ad.build_report(results)
    assert "Panola Resmi" in rep
    assert "ONAYLANDI" in rep


def test_build_report_flags_violations_and_critic_rejection():
    results = [
        {
            "property": "sc-domain:panola.app",
            "lines": ["  🛡️ Marka-savunma:", "    • 'panola' — poz 4.0, 37 gösterim, CTR %2.7"],
            "keywords": ["panola"],
            "ads": [{"keyword": "panola", "headlines": ["Bu Başlık Otuz Karakterden Kesinlikle Daha Uzun"], "descriptions": []}],
            "violations": ["'panola': başlık 47 karakter (>30) — '...'"],
            "critic": {"verdict": "FLAGGED", "notes": "kanıtsız üstünlük iddiası"},
            "n_rows": 5,
        }
    ]
    rep = ad.build_report(results)
    assert "MEKANİK karakter-limit ihlali" in rep
    assert "BAYRAKLANDI" in rep
    assert "kanıtsız üstünlük iddiası" in rep


def test_critic_review_skipped_without_ads():
    assert ad._critic_review("sc-domain:x.com", []) == {"verdict": "SKIPPED", "notes": ""}


def test_ad_copy_llm_returns_parsed_ads(monkeypatch):
    monkeypatch.setattr(ad.gsc, "_envget", lambda k: "fake-key" if k == "INTERNAL_API_KEY" else "")
    monkeypatch.setattr(
        ad.gsc,
        "_post_json",
        lambda *a, **k: {"result": '```json\n[{"keyword": "panola", "headlines": ["H1"], "descriptions": ["D1"]}]\n```'},
    )
    ads = ad._ad_copy_llm("sc-domain:panola.app", ["panola"])
    assert ads == [{"keyword": "panola", "headlines": ["H1"], "descriptions": ["D1"]}]


def test_ad_copy_llm_empty_on_invalid_json(monkeypatch):
    monkeypatch.setattr(ad.gsc, "_envget", lambda k: "fake-key" if k == "INTERNAL_API_KEY" else "")
    monkeypatch.setattr(ad.gsc, "_post_json", lambda *a, **k: {"result": "bu JSON değil, düz metin"})
    assert ad._ad_copy_llm("sc-domain:panola.app", ["panola"]) == []


def test_critic_review_parses_verdict(monkeypatch):
    monkeypatch.setattr(ad.gsc, "_envget", lambda k: "fake-key" if k == "INTERNAL_API_KEY" else "")
    monkeypatch.setattr(ad.gsc, "_post_json", lambda *a, **k: {"result": '{"verdict": "FLAGGED", "notes": "abartı var"}'})
    v = ad._critic_review("sc-domain:panola.app", [{"keyword": "panola", "headlines": ["H1"], "descriptions": ["D1"]}])
    assert v == {"verdict": "FLAGGED", "notes": "abartı var"}


def test_critic_review_unverified_on_network_failure(monkeypatch):
    monkeypatch.setattr(ad.gsc, "_envget", lambda k: "fake-key" if k == "INTERNAL_API_KEY" else "")

    def boom(*a, **k):
        raise TimeoutError("timeout")

    monkeypatch.setattr(ad.gsc, "_post_json", boom)
    v = ad._critic_review("sc-domain:panola.app", [{"keyword": "panola", "headlines": [], "descriptions": []}])
    assert v["verdict"] == "UNVERIFIED"


def test_write_discovery_sets_skip_dedup_and_week_tag(monkeypatch):
    # Bug-regresyon testi: skip_dedup=True olmadan ardışık haftalık raporlar semantic-dedup'a
    # yutuluyordu (discoveries#1144 06-22'den beri güncellenmedi, 3 hafta veri kaybı).
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured.update(payload)
        return {}

    monkeypatch.setattr(ad.gsc, "_envget", lambda k: "fake-key" if k == "MEMORY_API_KEY" else "")
    monkeypatch.setattr(ad.gsc, "_post_json", fake_post)
    err = ad._write_discovery("rapor metni")
    assert err == ""
    assert captured["skip_dedup"] is True
    assert captured["title"].startswith("Reklam fırsatları (ad-advisor) — ")
    assert "-W" in captured["title"]
