#!/usr/bin/env python3
"""İçerik Editörü — SEO blog makalesi üretici (multi-uzman vizyon: editör).

Diğer uzmanlar (seo-audit/ad-advisor/data-analyst) ANALİZ eder; bu uzman ÜRETİR:
verilen konu için özgün, çift-dilli (TR+EN) SEO blog makalesi yazar ve insan-onayına
PR olarak açar. Auto-publish YOK — PR merge-gate (CI+Codex) + insan review zorunlu.

Akış:
  1. /claude (Max-plan, read_only) ile makale METNİ üretilir — yalnız metin, mutasyon yok.
     Mevcut başlıklar prompt'a beslenir → tekrar/çakışma önlenir (slug benzersizliği).
  2. Script (LLM değil) çıktıyı parse eder, hedef-adaptöre yazar:
       - renderhane: articles.ts dizisine BlogArticle literal'i ekler → branch+commit+PR.
       - 3d-labx / bilge-arena: içerik-katmanı yok (DB/CMS veya blog-yok) → şimdilik
         TASLAK olarak ortak-hafızaya (discovery) yazılır; yayın eli/destination tanımlanınca
         PR-adaptörü eklenir. (Dürüstlük: PR-çıktısı yalnız dosya-blog'da gerçek.)

LLM içerik halüsinasyon riski → prompt "uydurma teknik iddia yok, dürüst ton" zorlar +
nihai kapı insan PR-review'ı. Salt-okunur /claude; git/PR'ı SCRIPT yapar (Claude değil).

Kullanım:
  content-editor.py <site> "<konu>"        # ör: content-editor.py renderhane "AI ile ürün fotoğrafı çekimi"
  content-editor.py renderhane --suggest    # LLM mevcut makalelere bakıp 5 konu önerir (üretmez)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from typing import Any

API_BASE = os.environ.get("API_BASE", "http://localhost:8420")
CLAUDE_TIMEOUT = int(os.environ.get("EDITOR_CLAUDE_TIMEOUT", "240"))
ENV_FILE = "/opt/linux-ai-server/.env"

# Site kayıt defteri. adapter=articles_ts → dosya-blog PR; adapter=draft → hafıza taslağı.
SITES: dict[str, dict[str, str]] = {
    "renderhane": {
        "repo": "/data/projects/renderhane",
        "adapter": "articles_ts",
        "articles_path": "src/lib/blog/articles.ts",
        "default_branch": "master",
        "langs": "tr,en",
        "content_format": "markdown",  # blog [slug] sayfası markdown render eder
        "author": "Renderhane",
        "about": (
            "Renderhane: AI destekli e-ticaret görsel üretim stüdyosu. Tek ürün fotoğrafından "
            "3D model, ürün fotoğrafı, sahne kurgusu, A+ içerik, tanıtım videosu üretir. "
            "Hedef kitle: Trendyol/Hepsiburada/Amazon/Etsy satıcıları, küçük markalar. "
            "Konular: e-ticaret görseli, ürün fotoğrafçılığı, 3D model, yapay zeka tasarım, "
            "pazaryeri optimizasyonu, dönüşüm artırma."
        ),
    },
    "3d-labx": {
        "repo": "/data/projects/3d-labx",
        "adapter": "astro_rehber",
        # tech-portal-frontend Astro sitesi; rehberler/ dosya-tabanlı .astro içerik sayfaları.
        "content_dir": "tech-portal-frontend/src/pages/rehberler",
        "route_prefix": "/rehberler",
        "default_branch": "main",
        "langs": "tr,en,de",  # mevcut rehberler 3-dilli (Language type = tr|en|de)
        "content_format": "html",  # .astro set:html ile basar
        "about": (
            "3D-labX: 3D baskı meraklıları ve profesyoneller için Türkçe rehber/teknik içerik sitesi. "
            "Konular: 3D yazıcı ayarları, slicer (Cura/PrusaSlicer/Orca/Bambu) kalibrasyonu, filament "
            "(PLA/PETG/ABS/TPU) seçimi, sık karşılaşılan baskı sorunları ve çözümleri, bakım, donanım."
        ),
    },
    "bilge-arena": {
        "repo": "/data/projects/bilge-arena",
        "adapter": "draft",
        "langs": "tr,en",
        "reason": "Blog katmanı yok (quiz platformu) — içerik-destination tanımlı değil.",
        "about": "Bilge Arena: TYT/LGS/AYT eğitim quiz platformu; eğitim içeriği/rehber.",
    },
}


# ── küçük yardımcılar (self-contained; GSC bağımlılığı yok) ──


def _envget(key: str) -> str:
    """.env'den ilk eşleşen anahtarı oku (çift-tanım varsa ilki — bkz architecture_env_dual)."""
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return os.environ.get(key, "")


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")  # noqa: S310 (sabit localhost)
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (sabit localhost)
        out: dict[str, Any] = json.loads(r.read().decode())
        return out


def _git(args: list[str], cwd: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} → {r.stderr.strip()[:200]}")
    return r.stdout.strip()


# ── mevcut içerik (tekrar önleme) ──


def existing_articles(site: dict[str, str]) -> list[dict[str, str]]:
    """articles.ts'ten slug + TR başlıkları çıkar (regex; tam-parse gerekmez)."""
    path = os.path.join(site["repo"], site.get("articles_path", ""))
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return []
    slugs = re.findall(r'slug:\s*"([^"]+)"', src)
    tr_titles = re.findall(r'title:\s*\{\s*\n?\s*tr:\s*"([^"]+)"', src)
    out: list[dict[str, str]] = []
    for i, s in enumerate(slugs):
        out.append({"slug": s, "title": tr_titles[i] if i < len(tr_titles) else ""})
    return out


# ── LLM üretim ──


def _claude(prompt: str) -> str:
    ikey = _envget("INTERNAL_API_KEY")
    if not ikey:
        raise RuntimeError("INTERNAL_API_KEY yok")
    out = _post_json(
        f"{API_BASE}/api/v1/claude/run",
        {"prompt": prompt, "read_only": True, "max_turns": 1},
        {"X-API-Key": ikey},
        CLAUDE_TIMEOUT,
    )
    return (out.get("result") or "").strip()


def suggest_topics(site: dict[str, str]) -> str:
    existing = existing_articles(site)
    titles = "; ".join(a["title"] for a in existing if a["title"]) or "(henüz makale yok)"
    prompt = (
        f"{site['about']}\n\n"
        f"Mevcut blog makaleleri: {titles}\n\n"
        "Bu site için SEO değeri yüksek, MEVCUTLARLA ÇAKIŞMAYAN 5 yeni blog makale konusu öner. "
        "Her satır: kısa başlık — neden (hedef arama niyeti). Sadece liste, giriş/açıklama yazma."
    )
    return _claude(prompt)


def _parse_article(text: str, langs: list[str]) -> dict[str, Any]:
    """LLM çıktısından JSON makaleyi çıkar (markdown fence'leri ayıkla, ilk{..son} dene).
    langs: zorunlu dil anahtarları (renderhane tr,en; 3d-labx tr,en,de)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    # Önce doğrudan dene; başarısızsa ilk{..son} span'ini çıkar. P3 (Codex): '{...}\ntrailing'
    # gibi JSON-sonrası düz-metinde startswith-{ true olup json.loads 'extra data' verir →
    # her sarmalı-metin (önde VEYA arkada prose) durumunda brace-narrowing fallback'i çalışır.
    try:
        art: dict[str, Any] = json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            raise
        art = json.loads(t[i : j + 1])
    req = ["slug", "tags", "title", "description", "content"]
    missing = [k for k in req if k not in art]
    if missing:
        raise ValueError(f"eksik alan: {missing}")
    for sub in ("title", "description", "content"):
        if not isinstance(art[sub], dict) or any(not art[sub].get(lng) for lng in langs):
            raise ValueError(f"{sub} {'+'.join(langs)} olmalı")
    art["slug"] = re.sub(r"[^a-z0-9-]", "", art["slug"].lower().replace(" ", "-")).strip("-")
    if not art["slug"]:
        raise ValueError("slug geçersiz")
    return art


def generate_article(site: dict[str, str], topic: str) -> dict[str, Any]:
    langs = site["langs"].split(",")
    existing = existing_articles(site)
    titles = "; ".join(a["title"] for a in existing if a["title"]) or "(yok)"
    # Şema dil-sayısına göre kurulur (tr,en / tr,en,de). Örnek değerler langs'tan üretilir.
    lang_names = {"tr": "Türkçe", "en": "İngilizce", "de": "Almanca"}

    fmt = site.get("content_format", "markdown")
    fmt_sample = "<h2>...</h2><p>...</p> (HTML)" if fmt == "html" else "## ...\\n\\n(markdown)"
    fmt_rule = (
        "içerik HTML (<h2>/<h3>/<p>/<ul>/<li>; set:html ile basılır, markdown DEĞİL)"
        if fmt == "html"
        else "içerik markdown (## / ### başlıklar, ** vurgu; markdown-render edilir)"
    )

    def _obj(sample: str) -> str:
        return "{" + ",".join(f'"{lng}":"{sample}"' for lng in langs) + "}"

    schema = (
        '{"slug":"ascii-kebab-case","tags":["Etiket1","Etiket2","Etiket3"],'
        f'"title":{_obj("...")},"description":{_obj("120-160 karakter meta")},'
        f'"content":{_obj(fmt_sample + " 800-1200 kelime")}}}'
    )
    diller = ", ".join(lang_names.get(lng, lng) for lng in langs)
    prompt = (
        f"Sen {site['about']} sitesi için çok-dilli SEO editörüsün.\n"
        f"KONU: {topic}\nDİLLER: {diller} ({','.join(langs)})\n"
        f"MEVCUT İÇERİK (tekrarlama, farklı açıdan yaz): {titles}\n\n"
        "Bu konuda TEK bir özgün makale üret. SADECE şu şemada geçerli JSON döndür "
        "(markdown fence YOK, giriş/açıklama YOK):\n" + schema + "\n\n"
        f"KURALLAR: özgün + doğru bilgi; UYDURMA teknik iddia/istatistik YOK; {fmt_rule}; "
        f"okunabilir, dürüst ton (abartı/clickbait yok); TÜM diller ({','.join(langs)}) birbirinin "
        "EŞDEĞERİ (aynı değeri taşısın); slug ASCII kebab-case (Türkçe/aksan yok); JSON string'leri "
        "düzgün escape'li."
    )
    return _parse_article(_claude(prompt), langs)


# ── renderhane adaptörü: articles.ts'e ekle ──


def _ts_str(s: str) -> str:
    """Kısa string → güvenli TS çift-tırnak literal (JSON escape TS-uyumlu)."""
    return json.dumps(s, ensure_ascii=False)


def _ts_template(s: str) -> str:
    """Uzun markdown → TS template-literal (backtick). ` ve ${ kaçışlanır."""
    return "`" + s.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${") + "`"


def render_ts_object(art: dict[str, Any], author: str, date: str) -> str:
    tags = ", ".join(_ts_str(t) for t in art["tags"])
    return (
        "  {\n"
        f"    slug: {_ts_str(art['slug'])},\n"
        f"    date: {_ts_str(date)},\n"
        f"    author: {_ts_str(author)},\n"
        f"    tags: [{tags}],\n"
        "    title: {\n"
        f"      tr: {_ts_str(art['title']['tr'])},\n"
        f"      en: {_ts_str(art['title']['en'])},\n"
        "    },\n"
        "    description: {\n"
        f"      tr: {_ts_str(art['description']['tr'])},\n"
        f"      en: {_ts_str(art['description']['en'])},\n"
        "    },\n"
        "    content: {\n"
        f"      tr: {_ts_template(art['content']['tr'])},\n"
        f"      en: {_ts_template(art['content']['en'])},\n"
        "    },\n"
        "  },\n"
    )


def insert_article(site: dict[str, str], ts_obj: str) -> None:
    path = os.path.join(site["repo"], site["articles_path"])
    with open(path, encoding="utf-8") as f:
        src = f.read()
    anchor = "export const articles: BlogArticle[] = [\n"
    idx = src.find(anchor)
    if idx < 0:
        raise RuntimeError("articles dizisi bulunamadı")
    pos = idx + len(anchor)
    new_src = src[:pos] + ts_obj + src[pos:]
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_src)


# ── 3d-labx adaptörü: tech-portal-frontend rehberler/<slug>.astro dosyası ──


def render_astro_page(art: dict[str, Any], langs: list[str]) -> str:
    """BaseLayout+Header kullanan, çok-dilli (Record<Language,string>) içerik .astro sayfası.
    Frontmatter yapısı mevcut rehberler'in pattern'iyle aynı (astro check ile doğrulandı);
    title/description JSON-string, content template-literal (HTML, backtick/${} escape)."""

    def _rec(field: str) -> str:
        body = "".join(f"    {lng}: {_ts_str(art[field][lng])},\n" for lng in langs)
        return "{\n" + body + "  }"

    content_body = "".join(f"    {lng}: {_ts_template(art['content'][lng])},\n" for lng in langs)
    content_rec = "{\n" + content_body + "  }"
    return (
        "---\n"
        'import BaseLayout from "../../layouts/BaseLayout.astro";\n'
        'import Header from "../../components/Header.astro";\n'
        'import { getLanguage, type Language } from "../../lib/api";\n\n'
        "export const prerender = false;\n\n"
        "// İçerik Editörü ajanı (content-editor.py) tarafından üretildi — insan review gerekli.\n"
        "const lang = getLanguage(Astro.request) as Language;\n\n"
        f"const titles: Record<Language, string> = {_rec('title')};\n"
        f"const descriptions: Record<Language, string> = {_rec('description')};\n"
        f"const content: Record<Language, string> = {content_rec};\n\n"
        "const title = titles[lang];\nconst description = descriptions[lang];\nconst body = content[lang];\n"
        "---\n\n"
        "<BaseLayout title={title} description={description}>\n"
        "  <Header />\n"
        '  <main class="article-page">\n'
        '    <div class="container" style="max-width:860px;margin:0 auto;padding:2.5rem 1rem;line-height:1.7;">\n'
        "      <article set:html={body} />\n"
        "    </div>\n"
        "  </main>\n"
        "</BaseLayout>\n"
    )


def write_astro(site: dict[str, str], art: dict[str, Any]) -> str:
    """Üretilen .astro'yu content_dir/<slug>.astro'ya yaz. Dönüş: git-add için repo-rel yol."""
    rel = os.path.join(site["content_dir"], art["slug"] + ".astro")
    path = os.path.join(site["repo"], rel)
    if os.path.exists(path):
        raise RuntimeError(f"dosya zaten var: {rel}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_astro_page(art, site["langs"].split(",")))
    return rel


def _astro_slug_exists(site: dict[str, str], slug: str) -> bool:
    return os.path.exists(os.path.join(site["repo"], site["content_dir"], slug + ".astro"))


def open_pr(site: dict[str, str], art: dict[str, Any], topic: str) -> str:
    repo = site["repo"]
    adapter = site["adapter"]
    default_branch = site.get("default_branch", "master")
    route_prefix = site.get("route_prefix", "/blog")
    branch = f"content/{art['slug'][:42]}"
    # P2 (Codex): commit/push başarısız olursa checkout'u ESKİ branch'e geri al — aksi halde
    # /data/projects/<repo> content-branch'inde kalır, ORADA koşan audit/test'leri zehirler
    # (weekly-audit.sh, run-all-tests.sh aynı dizinde çalışır). finally tüm yollarda restore.
    try:
        orig_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    except RuntimeError:
        orig_branch = default_branch
    # P2 (Codex): /data/projects/<repo> İNSAN+başka-otomasyonla PAYLAŞILIYOR. Kirliyse DOKUNMA —
    # aksi halde finally'deki 'checkout -f' alakasız yerel değişikliği siler ya da staged edit
    # üretilen commit'e sızar. Kirli → raise (main except → write_draft fallback, içerik kaybolmaz).
    if _git(["status", "--porcelain"], repo).strip():
        raise RuntimeError(f"{repo} kirli (commit'siz değişiklik var) — güvenlik için dokunulmadı")
    try:
        _git(["fetch", "origin", "-q"], repo)
        _git(["checkout", "-B", branch, f"origin/{default_branch}"], repo)
        # P2 (Codex): slug-çakışmasını GERÇEK base'e (origin/<default>) karşı YENİDEN doğrula.
        if adapter == "articles_ts":
            collision = any(a["slug"] == art["slug"] for a in existing_articles(site))
        else:  # astro_rehber
            collision = _astro_slug_exists(site, art["slug"])
        if collision:
            raise RuntimeError(f"slug origin/{default_branch}'da zaten var: {art['slug']}")
        # git kimliği: Vercel/CF-deploy repo kuralı → author turer73 (klipperos DEĞİL) +
        # Co-Authored-By YOK (renderhane Vercel hobby-deploy bloklar; 3d-labx convention=0 co-author).
        subprocess.run(["git", "config", "user.email", "turgut.urer@gmail.com"], cwd=repo, check=False)
        subprocess.run(["git", "config", "user.name", "turer73"], cwd=repo, check=False)

        date = datetime.now(UTC).strftime("%Y-%m-%d")
        if adapter == "articles_ts":
            insert_article(site, render_ts_object(art, site["author"], date))
            add_path = site["articles_path"]
        else:  # astro_rehber
            add_path = write_astro(site, art)
        _git(["add", add_path], repo)
        msg = (
            f"content: {art['title']['tr'][:60]}\n\n"
            "İçerik Editörü ajanı (content-editor.py) tarafından üretildi.\n"
            f"Konu: {topic}\nSlug: {art['slug']} | Etiketler: {', '.join(art['tags'])}\n\n"
            "TASLAK — insan review gerekli: özgünlük, teknik doğruluk, ton.\n"
            "Auto-publish yok; merge öncesi içerik gözden geçirilmeli."
        )
        _git(["commit", "-m", msg], repo)
        _git(["push", "-u", "origin", branch], repo)
        diller = "/".join(s.upper() for s in site["langs"].split(","))
        body = (
            "## İçerik Editörü taslağı — SEO içerik\n\n"
            f"- **Konu:** {topic}\n- **Slug:** `{art['slug']}` → `{route_prefix}/{art['slug']}` ({diller})\n"
            f"- **Etiketler:** {', '.join(art['tags'])}\n\n"
            f"### TR başlık\n{art['title']['tr']}\n\n### Açıklama (meta)\n{art['description']['tr']}\n\n"
            "---\n⚠️ **İNSAN REVIEW GEREKLİ** — `content-editor.py` ajanı üretti. Merge öncesi kontrol: "
            "(1) bilgi doğruluğu/uydurma-iddia yok, (2) özgünlük, (3) dürüst ton, (4) dil denkliği. "
            "Auto-publish yok; insan onayı.\n\n"
            "🤖 content-editor.py (multi-uzman: editör)"
        )
        pr_url = subprocess.run(
            ["gh", "pr", "create", "--title", f"content: {art['title']['tr'][:60]}", "--body", body, "--head", branch],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if pr_url.returncode != 0:
            raise RuntimeError(f"gh pr create → {pr_url.stderr.strip()[:200]}")
        return pr_url.stdout.strip()
    finally:
        # Her yolda orijinal branch'e dön; -f yarım-uygulanan (insert oldu ama commit/push
        # olmadı) değişiklikleri de ATAR → repo TEMİZ kalır, aynı dizinde koşan audit/test
        # zehirlenmez (Codex P2: kirli-tree restore). checkout başarısız olsa da yut.
        subprocess.run(["git", "checkout", "-f", orig_branch, "-q"], cwd=repo, check=False)


# ── taslak adaptörü: hafızaya yaz (dosya-blog'u olmayan siteler) ──


def write_draft(site_name: str, art: dict[str, Any], topic: str) -> str:
    mkey = _envget("MEMORY_API_KEY")
    if not mkey:
        return "no MEMORY_API_KEY"
    # P2 (Codex): bu discovery taslağın TEK kalıcı kopyası (draft-only site / PR-fail fallback)
    # → TR+EN içeriğin TAMAMINI sakla (eski hali yalnız TR'yi 3000c kırpıyordu → EN + TR-kuyruğu
    # kaybolup taslak yeniden-üretmeden yayınlanamıyordu). discoveries.details TEXT — sınırsız.
    # Dile-genel: art'ta bulunan TÜM dilleri sakla (3d-labx fallback'inde DE kaybolmasın).
    langs = list(art.get("title", {}).keys())
    head = (
        f"📝 İçerik Editörü taslağı — {site_name} (konu: {topic})\n"
        f"PR-hedefi yok/başarısız ({SITES[site_name].get('reason', '')}); yayın elle.\n\n"
        f"SLUG: {art['slug']}\nETİKETLER: {', '.join(art['tags'])}\n"
    )
    body = "".join(
        f"\nBAŞLIK ({lng.upper()}): {art['title'][lng]}\nMETA ({lng.upper()}): {art['description'][lng]}\n"
        f"--- İÇERİK ({lng.upper()}) ---\n{art['content'][lng]}\n"
        for lng in langs
    )
    details = head + body
    # P2 (Codex): memory API timeout/HTTP hatası propagate olup OUTCOME marker'sız crash
    # ETMESİN → hata string'i döndür (main OUTCOME: fail/partial basar, diğer script'lerle tutarlı).
    try:
        _post_json(
            f"{API_BASE}/api/v1/memory/discoveries",
            {
                "device_name": "klipper",
                "project": site_name,
                "type": "learning",
                "title": f"İçerik taslağı: {art['title']['tr'][:50]}",
                "details": details,
                "rationale": "content-editor.py — taslak makale (dosya-blog yok, yayın elle).",
            },
            {"X-Memory-Key": mkey},
            15,
        )
    except Exception as e:
        return str(e)[:150]
    return ""


# ── ana ──


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2:
        print('OUTCOME: fail | kullanım: content-editor.py <site> "<konu>" | <site> --suggest')
        print(f"siteler: {', '.join(SITES)}")
        return 0
    site_name, topic = args[0], args[1]
    site = SITES.get(site_name)
    if not site:
        print(f"OUTCOME: fail | bilinmeyen site '{site_name}' (geçerli: {', '.join(SITES)})")
        return 0

    if topic == "--suggest":
        try:
            print(suggest_topics(site))
            print('OUTCOME: pass | konu önerileri üretildi (üretim için: content-editor.py <site> "<konu>")')
        except Exception as e:
            print(f"OUTCOME: fail | öneri üretilemedi: {str(e)[:150]}")
        return 0

    try:
        art = generate_article(site, topic)
    except Exception as e:
        print(f"OUTCOME: fail | makale üretilemedi: {str(e)[:180]}")
        return 0

    # Dosya-blog: slug-çakışma kontrolü open_pr İÇİNDE (fetch→origin/master sonrası, tek-kaynak;
    # Codex P2: main'deki ön-kontrol stale local checkout'a bakardı → kaldırıldı). Çakışma/hata →
    # except → write_draft (üretilen içerik kaybolmaz).
    if site["adapter"] in ("articles_ts", "astro_rehber"):
        try:
            pr = open_pr(site, art, topic)
            print(f"OUTCOME: pass | makale taslağı PR açıldı: {pr}")
        except Exception as e:
            # P2 (Codex): write_draft de FAIL'se içerik HİÇBİR yerde yok → 'partial/yazıldı' yanıltıcı.
            # err'e göre ayır: fail (içerik kayıp) vs partial (PR yok ama taslak kurtarıldı).
            err = write_draft(site_name, art, topic)
            if err:
                print(f"OUTCOME: fail | PR açılamadı ({str(e)[:80]}) VE taslak kaydedilemedi ({err}) — içerik KAYIP")
            else:
                print(f"OUTCOME: partial | PR açılamadı ({str(e)[:100]}); taslak hafızaya kurtarıldı")
        return 0

    # draft adaptörü
    err = write_draft(site_name, art, topic)
    if err:
        print(f"OUTCOME: fail | taslak yazılamadı: {err}")
    else:
        print(f"OUTCOME: pass | taslak hafızaya yazıldı ({site_name} — dosya-blog yok, yayın elle)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
