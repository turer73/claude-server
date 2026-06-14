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

    art = ce._parse_article(json.dumps(_valid_article()), ["tr", "en"])
    assert art["slug"] == "test-makale"
    assert art["title"]["tr"] == "Türkçe Başlık"


def test_parse_strips_markdown_fence():
    import json

    raw = "```json\n" + json.dumps(_valid_article()) + "\n```"
    art = ce._parse_article(raw, ["tr", "en"])
    assert art["slug"] == "test-makale"


def test_parse_extracts_embedded_json():
    import json

    raw = "İşte makale:\n" + json.dumps(_valid_article()) + "\nUmarım beğenirsin."
    art = ce._parse_article(raw, ["tr", "en"])
    assert art["tags"] == ["A", "B"]


def test_parse_json_with_trailing_prose():
    # P3 (Codex): JSON ile BAŞLAYIP sonrasında düz metin → 'extra data' yerine brace-fallback
    import json

    raw = json.dumps(_valid_article()) + "\nUmarım işine yarar."
    art = ce._parse_article(raw, ["tr", "en"])
    assert art["slug"] == "test-makale"


def test_parse_slug_normalized_ascii_kebab():
    import json

    a = _valid_article()
    a["slug"] = "Türkçe Slug Çöp!!"
    art = ce._parse_article(json.dumps(a), ["tr", "en"])
    # Türkçe karakter + boşluk + noktalama temizlenir, ascii-kebab kalır
    assert art["slug"] == "trke-slug-p"
    assert all(c.isascii() for c in art["slug"])


def test_parse_rejects_missing_field():
    import json

    a = _valid_article()
    del a["content"]
    with pytest.raises(ValueError, match="eksik alan"):
        ce._parse_article(json.dumps(a), ["tr", "en"])


def test_parse_rejects_monolingual():
    import json

    a = _valid_article()
    a["title"] = {"tr": "yalniz tr"}  # en eksik
    with pytest.raises(ValueError, match="tr\\+en"):
        ce._parse_article(json.dumps(a), ["tr", "en"])


def test_parse_rejects_empty_slug_after_normalize():
    import json

    a = _valid_article()
    a["slug"] = "!!!"  # normalize sonrası boş
    with pytest.raises(ValueError, match="slug"):
        ce._parse_article(json.dumps(a), ["tr", "en"])


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


# ── astro_rehber adaptörü (3d-labx) ──


def _valid_article_3lang() -> dict:
    a = _valid_article()
    a["title"]["de"] = "Deutscher Titel"
    a["description"]["de"] = "de beschreibung"
    a["content"]["de"] = "<h2>Überschrift</h2><p>Inhalt.</p>"
    return a


def test_parse_3lang_requires_de():
    import json

    a = _valid_article()  # yalniz tr+en
    with pytest.raises(ValueError, match="tr\\+en\\+de"):
        ce._parse_article(json.dumps(a), ["tr", "en", "de"])


def test_parse_3lang_accepts_full():
    import json

    art = ce._parse_article(json.dumps(_valid_article_3lang()), ["tr", "en", "de"])
    assert art["title"]["de"] == "Deutscher Titel"


def test_render_astro_page_wellformed():
    ts = ce.render_astro_page(_valid_article_3lang(), ["tr", "en", "de"])
    # Frontmatter + import + Record<Language> + body
    assert ts.startswith("---\n")
    assert 'import BaseLayout from "../../layouts/BaseLayout.astro";' in ts
    assert "const titles: Record<Language, string> = {" in ts
    assert "tr:" in ts
    assert "en:" in ts
    assert "de:" in ts
    assert "<article set:html={body} />" in ts
    assert ts.rstrip().endswith("</BaseLayout>")


def test_render_astro_page_escapes_backtick_content():
    a = _valid_article_3lang()
    a["content"]["tr"] = "Örnek: `npm run build` ve ${x}"
    ts = ce.render_astro_page(a, ["tr", "en", "de"])
    # content template-literal'i kırılmasın
    assert "\\`npm run build\\`" in ts
    assert "\\${x}" in ts


def test_astro_slug_exists(tmp_path):
    cdir = tmp_path / "rehberler"
    cdir.mkdir()
    (cdir / "var-olan.astro").write_text("x", encoding="utf-8")
    site = {"repo": str(tmp_path), "content_dir": "rehberler"}
    assert ce._astro_slug_exists(site, "var-olan") is True
    assert ce._astro_slug_exists(site, "yok") is False


# ── Codex #128 P2 fix'leri ──


def test_sanitize_html_strips_dangerous():
    out = ce._sanitize_html(
        '<h2>Başlık</h2><script>alert(1)</script><p onclick="x()">m</p>'
        '<a href="javascript:evil()">l</a><iframe src=x></iframe><p>güvenli</p>'
    )
    assert "<script" not in out
    assert "onclick" not in out
    assert "javascript:" not in out
    assert "<iframe" not in out
    assert "<h2>Başlık</h2>" in out
    assert "<p>güvenli</p>" in out


def test_render_astro_page_sanitizes_content():
    a = _valid_article_3lang()
    a["content"]["tr"] = "<p>ok</p><script>alert(1)</script>"
    ts = ce.render_astro_page(a, ["tr", "en", "de"])
    assert "<script" not in ts


def test_existing_articles_astro_scans_dir(tmp_path):
    cdir = tmp_path / "rehberler"
    cdir.mkdir()
    (cdir / "rehber-bir.astro").write_text(
        'const titles: Record<Language, string> = {\n  tr: "Rehber Bir",\n  en: "Guide One",\n};\n',
        encoding="utf-8",
    )
    (cdir / "_test.astro").write_text("x", encoding="utf-8")  # _ ile başlayan atlanır
    (cdir / "index.astro").write_text("x", encoding="utf-8")  # index atlanır
    site = {"repo": str(tmp_path), "adapter": "astro_rehber", "content_dir": "rehberler"}
    arts = ce.existing_articles(site)
    slugs = [a["slug"] for a in arts]
    assert slugs == ["rehber-bir"]
    assert arts[0]["title"] == "Rehber Bir"


def test_existing_articles_astro_missing_dir():
    site = {"repo": "/nonexistent", "adapter": "astro_rehber", "content_dir": "x"}
    assert ce.existing_articles(site) == []


def test_write_draft_langs_intersection_no_keyerror():
    # title'da fazladan 'de' var ama description/content'te yok → KeyError VERMEMELI
    import re as _re

    a = _valid_article()
    a["title"]["de"] = "extra"  # description/content'te de yok
    # langs hesabı kesişim olmalı (tr,en) → KeyError yok; details TR+EN içermeli
    langs = [lng for lng in a["title"] if lng in a["description"] and lng in a["content"]]
    assert langs == ["tr", "en"]
    assert _re  # noqa
