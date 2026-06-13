"""scripts/content-editor.py — saf fonksiyonlar (LLM/git/HTTP'siz).

Auth/_claude/open_pr test edilmez (canlı /claude + git + gh gerektirir); deterministik
çekirdek test edilir: JSON-makale parse (fence/eksik-alan/slug-normalize), TS literal
üretimi (backtick/${} escape — bozuk escape = derlenmeyen articles.ts), mevcut-makale parse.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("content_editor", ROOT / "scripts" / "content-editor.py")
ce = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ce)


def _valid_article() -> dict:
    return {
        "slug": "test-makale",
        "tags": ["A", "B"],
        "title": {"tr": "Türkçe Başlık", "en": "English Title"},
        "description": {"tr": "tr açıklama", "en": "en desc"},
        "content": {"tr": "## TR\n\niçerik", "en": "## EN\n\nbody"},
    }


# ── _parse_article ──


def test_parse_plain_json():
    import json

    art = ce._parse_article(json.dumps(_valid_article()))
    assert art["slug"] == "test-makale"
    assert art["title"]["tr"] == "Türkçe Başlık"


def test_parse_strips_markdown_fence():
    import json

    raw = "```json\n" + json.dumps(_valid_article()) + "\n```"
    art = ce._parse_article(raw)
    assert art["slug"] == "test-makale"


def test_parse_extracts_embedded_json():
    import json

    raw = "İşte makale:\n" + json.dumps(_valid_article()) + "\nUmarım beğenirsin."
    art = ce._parse_article(raw)
    assert art["tags"] == ["A", "B"]


def test_parse_slug_normalized_ascii_kebab():
    import json

    a = _valid_article()
    a["slug"] = "Türkçe Slug Çöp!!"
    art = ce._parse_article(json.dumps(a))
    # Türkçe karakter + boşluk + noktalama temizlenir, ascii-kebab kalır
    assert art["slug"] == "trke-slug-p"
    assert all(c.isascii() for c in art["slug"])


def test_parse_rejects_missing_field():
    import json

    a = _valid_article()
    del a["content"]
    with pytest.raises(ValueError, match="eksik alan"):
        ce._parse_article(json.dumps(a))


def test_parse_rejects_monolingual():
    import json

    a = _valid_article()
    a["title"] = {"tr": "yalniz tr"}  # en eksik
    with pytest.raises(ValueError, match="tr\\+en"):
        ce._parse_article(json.dumps(a))


def test_parse_rejects_empty_slug_after_normalize():
    import json

    a = _valid_article()
    a["slug"] = "!!!"  # normalize sonrası boş
    with pytest.raises(ValueError, match="slug"):
        ce._parse_article(json.dumps(a))


# ── TS literal üretimi (escape doğruluğu = derlenebilir articles.ts) ──


def test_ts_str_escapes_quotes():
    assert ce._ts_str('a"b') == '"a\\"b"'


def test_ts_template_escapes_backtick_and_interp():
    # backtick ve ${ kaçışlanmazsa template literal kırılır → TS derlenmez
    out = ce._ts_template("kod: `x` ve ${y}")
    assert "\\`x\\`" in out
    assert "\\${y}" in out
    assert out.startswith("`")
    assert out.endswith("`")


def test_ts_template_escapes_backslash_first():
    out = ce._ts_template("yol\\path")
    assert "\\\\path" in out


def test_render_ts_object_wellformed():
    ts = ce.render_ts_object(_valid_article(), "Renderhane", "2026-06-13")
    # Zorunlu alanlar + kapanış virgülü (dizi-içi ekleme)
    assert 'slug: "test-makale"' in ts
    assert 'date: "2026-06-13"' in ts
    assert 'author: "Renderhane"' in ts
    assert "tr: `## TR" in ts
    assert ts.strip().endswith("},")


def test_render_ts_object_with_backtick_content_safe():
    a = _valid_article()
    a["content"]["tr"] = "Örnek: `npm run build` komutu"
    ts = ce.render_ts_object(a, "Renderhane", "2026-06-13")
    # içerikteki backtick kaçışlı → çevreleyen template'i kırmaz
    assert "\\`npm run build\\`" in ts


# ── existing_articles (regex parse) ──


def test_existing_articles_parses_slugs(tmp_path):
    sample = """export const articles: BlogArticle[] = [
  {
    slug: "ilk-makale",
    title: {
      tr: "İlk Makale",
      en: "First",
    },
  },
  {
    slug: "ikinci-makale",
    title: {
      tr: "İkinci Makale",
      en: "Second",
    },
  },
];
"""
    p = tmp_path / "articles.ts"
    p.write_text(sample, encoding="utf-8")
    site = {"repo": str(tmp_path), "articles_path": "articles.ts"}
    arts = ce.existing_articles(site)
    slugs = [a["slug"] for a in arts]
    assert slugs == ["ilk-makale", "ikinci-makale"]
    assert arts[0]["title"] == "İlk Makale"


def test_existing_articles_missing_file_returns_empty():
    site = {"repo": "/nonexistent", "articles_path": "x.ts"}
    assert ce.existing_articles(site) == []
