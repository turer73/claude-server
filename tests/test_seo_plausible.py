"""scripts/seo-plausible.py — Plausible analiz mantığı (gerçek-Plausible'sız, mock).

Saf fonksiyonlar (analyze_pages/analyze_sources/build_report) + key-yok kısa-devre.
Auth/HTTP test edilmez (canlı Plausible + API-key gerektirir); analiz-kuralları test edilir.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("seo_plausible", ROOT / "scripts" / "seo-plausible.py")
pl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pl)


def _page(page, vis, bounce, dur):
    return {"dimensions": [page], "metrics": [vis, bounce, dur]}


def _src(source, vis):
    return {"dimensions": [source], "metrics": [vis]}


# ── analyze_pages ────────────────────────────────────────────────────────────


def test_pages_high_bounce_flagged_p2():
    f = pl.analyze_pages([_page("/landing", 120, 88, 5)])
    assert any(s == "P2" and "bounce" in m for s, m in f)


def test_pages_low_engagement_flagged_p3():
    f = pl.analyze_pages([_page("/thin", 80, 40, 8)])
    assert any(s == "P3" and "etkileşim" in m for s, m in f)


def test_pages_healthy_not_flagged():
    assert pl.analyze_pages([_page("/", 875, 17, 216)]) == []


def test_pages_low_traffic_below_threshold_ignored():
    # 49 ziyaretçi < 50 eşik → bounce %100 olsa bile gürültü yapma
    assert pl.analyze_pages([_page("/x", 49, 100, 0)]) == []


def test_pages_malformed_row_skipped():
    assert pl.analyze_pages([{"dimensions": [], "metrics": []}, {"metrics": [1]}]) == []


# ── analyze_sources ──────────────────────────────────────────────────────────


def test_sources_low_organic_share_flagged():
    rows = [_src("Direct / None", 90), _src("Google", 10)]
    f = pl.analyze_sources(rows, total_visitors=100)
    assert any(s == "P3" and "organik" in m for s, m in f)


def test_sources_healthy_organic_not_flagged():
    rows = [_src("Google", 70), _src("Bing", 10), _src("Direct / None", 20)]
    assert pl.analyze_sources(rows, total_visitors=100) == []


def test_sources_below_traffic_threshold_ignored():
    rows = [_src("Direct / None", 40)]
    assert pl.analyze_sources(rows, total_visitors=40) == []


def test_sources_organic_label_case_insensitive():
    # 'google' küçük harf de organik sayılmalı
    rows = [_src("google", 80), _src("Direct / None", 20)]
    assert pl.analyze_sources(rows, total_visitors=100) == []


# ── build_report ─────────────────────────────────────────────────────────────


def test_report_clean_site_green():
    r = [{"site": "x.com", "visitors": 100, "pageviews": 300, "bounce": 20, "duration": 200, "findings": []}]
    out = pl.build_report(r)
    assert "🟢" in out
    assert "belirgin trafik/davranış sorunu yok" in out


def test_report_p1_site_red():
    r = [{"site": "y.com", "visitors": 0, "pageviews": 0, "bounce": 0, "duration": 0, "findings": [("P1", "çekilemedi")]}]
    assert "🔴" in pl.build_report(r)
