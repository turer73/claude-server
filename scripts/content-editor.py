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

API_BASE = os.environ.get("API_BASE", "http://localhost:8420")
CLAUDE_TIMEOUT = int(os.environ.get("EDITOR_CLAUDE_TIMEOUT", "240"))
ENV_FILE = "/opt/linux-ai-server/.env"

# Site kayıt defteri. adapter=articles_ts → dosya-blog PR; adapter=draft → hafıza taslağı.
SITES: dict[str, dict] = {
    "renderhane": {
        "repo": "/data/projects/renderhane",
        "adapter": "articles_ts",
        "articles_path": "src/lib/blog/articles.ts",
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
        "adapter": "draft",
        "reason": "İçerik DB/CMS-tabanlı (update-content.sql) — PR'ın dosya-hedefi yok; yayın elle.",
        "about": "3D-Labx: 3D baskı, filament, teknik rehber ve ürün içeriği.",
    },
    "bilge-arena": {
        "repo": "/data/projects/bilge-arena",
        "adapter": "draft",
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


def _post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")  # noqa: S310 (sabit localhost)
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (sabit localhost)
        return json.loads(r.read().decode())


def _git(args: list[str], cwd: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} → {r.stderr.strip()[:200]}")
    return r.stdout.strip()


# ── mevcut içerik (tekrar önleme) ──


def existing_articles(site: dict) -> list[dict]:
    """articles.ts'ten slug + TR başlıkları çıkar (regex; tam-parse gerekmez)."""
    path = os.path.join(site["repo"], site.get("articles_path", ""))
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return []
    slugs = re.findall(r'slug:\s*"([^"]+)"', src)
    tr_titles = re.findall(r'title:\s*\{\s*\n?\s*tr:\s*"([^"]+)"', src)
    out = []
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


def suggest_topics(site: dict) -> str:
    existing = existing_articles(site)
    titles = "; ".join(a["title"] for a in existing if a["title"]) or "(henüz makale yok)"
    prompt = (
        f"{site['about']}\n\n"
        f"Mevcut blog makaleleri: {titles}\n\n"
        "Bu site için SEO değeri yüksek, MEVCUTLARLA ÇAKIŞMAYAN 5 yeni blog makale konusu öner. "
        "Her satır: kısa başlık — neden (hedef arama niyeti). Sadece liste, giriş/açıklama yazma."
    )
    return _claude(prompt)


def _parse_article(text: str) -> dict:
    """LLM çıktısından JSON makaleyi çıkar (markdown fence'leri ayıkla, ilk{..son} dene)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    if not t.startswith("{"):
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i : j + 1]
    art = json.loads(t)
    req = ["slug", "tags", "title", "description", "content"]
    missing = [k for k in req if k not in art]
    if missing:
        raise ValueError(f"eksik alan: {missing}")
    for sub in ("title", "description", "content"):
        if not (isinstance(art[sub], dict) and art[sub].get("tr") and art[sub].get("en")):
            raise ValueError(f"{sub} tr+en olmalı")
    art["slug"] = re.sub(r"[^a-z0-9-]", "", art["slug"].lower().replace(" ", "-")).strip("-")
    if not art["slug"]:
        raise ValueError("slug geçersiz")
    return art


def generate_article(site: dict, topic: str) -> dict:
    existing = existing_articles(site)
    titles = "; ".join(a["title"] for a in existing if a["title"]) or "(yok)"
    prompt = (
        f"Sen {site['about']} sitesi için çift-dilli SEO blog editörüsün.\n"
        f"KONU: {topic}\n"
        f"MEVCUT MAKALELER (tekrarlama, farklı açıdan yaz): {titles}\n\n"
        "Bu konuda TEK bir özgün makale üret. SADECE şu şemada geçerli JSON döndür "
        "(markdown fence YOK, giriş/açıklama YOK):\n"
        '{"slug":"ascii-kebab-case","tags":["Etiket1","Etiket2","Etiket3"],'
        '"title":{"tr":"...","en":"..."},'
        '"description":{"tr":"120-160 karakter meta","en":"120-160 chars"},'
        '"content":{"tr":"## ...\\n\\nmarkdown 800-1200 kelime...","en":"## ...markdown..."}}\n\n'
        "KURALLAR: özgün + doğru bilgi; UYDURMA teknik iddia/istatistik YOK; H2/H3 başlıklı, "
        "okunabilir, dürüst ton (abartı/clickbait yok); TR ve EN içerik birbirinin EŞDEĞERİ "
        "(birebir çeviri şart değil, aynı değeri taşısın); slug ASCII kebab-case (Türkçe karakter yok); "
        "JSON içindeki tüm string'ler düzgün escape'li (özellikle content içi \\n)."
    )
    return _parse_article(_claude(prompt))


# ── renderhane adaptörü: articles.ts'e ekle ──


def _ts_str(s: str) -> str:
    """Kısa string → güvenli TS çift-tırnak literal (JSON escape TS-uyumlu)."""
    return json.dumps(s, ensure_ascii=False)


def _ts_template(s: str) -> str:
    """Uzun markdown → TS template-literal (backtick). ` ve ${ kaçışlanır."""
    return "`" + s.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${") + "`"


def render_ts_object(art: dict, author: str, date: str) -> str:
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


def insert_article(site: dict, ts_obj: str) -> None:
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


def open_pr(site: dict, art: dict, topic: str) -> str:
    repo = site["repo"]
    branch = f"content/blog-{art['slug'][:40]}"
    _git(["fetch", "origin", "-q"], repo)
    _git(["checkout", "-B", branch, "origin/master"], repo)
    # git kimliği (cron/headless ortamda boş olabilir)
    subprocess.run(["git", "config", "user.email", "turgut.urer@gmail.com"], cwd=repo)
    subprocess.run(["git", "config", "user.name", "klipperos"], cwd=repo)

    date = datetime.now(UTC).strftime("%Y-%m-%d")
    insert_article(site, render_ts_object(art, site["author"], date))
    _git(["add", site["articles_path"]], repo)
    msg = (
        f"content(blog): {art['title']['tr'][:60]}\n\n"
        f"İçerik Editörü ajanı (content-editor.py) tarafından üretildi.\n"
        f"Konu: {topic}\nSlug: {art['slug']} | Etiketler: {', '.join(art['tags'])}\n\n"
        "TASLAK — insan review gerekli: özgünlük, teknik doğruluk, ton.\n"
        "Auto-publish yok; merge öncesi içerik gözden geçirilmeli.\n\n"
        "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
    )
    _git(["commit", "-m", msg], repo)
    _git(["push", "-u", "origin", branch], repo)
    body = (
        f"## İçerik Editörü taslağı — SEO blog makalesi\n\n"
        f"- **Konu:** {topic}\n- **Slug:** `{art['slug']}` → `/blog/{art['slug']}` (TR+EN)\n"
        f"- **Etiketler:** {', '.join(art['tags'])}\n\n"
        f"### TR başlık\n{art['title']['tr']}\n\n### Açıklama (meta)\n{art['description']['tr']}\n\n"
        "---\n⚠️ **İNSAN REVIEW GEREKLİ** — `content-editor.py` ajanı üretti. Merge öncesi kontrol: "
        "(1) bilgi doğruluğu/uydurma-iddia yok, (2) özgünlük, (3) dürüst ton, (4) TR/EN denklik. "
        "Auto-publish yok; CI+Codex gate + insan onayı.\n\n"
        "🤖 content-editor.py (multi-uzman: editör)"
    )
    pr_url = subprocess.run(
        ["gh", "pr", "create", "--title", f"content(blog): {art['title']['tr'][:60]}", "--body", body, "--head", branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    _git(["checkout", "master", "-q"], repo)
    if pr_url.returncode != 0:
        raise RuntimeError(f"gh pr create → {pr_url.stderr.strip()[:200]}")
    return pr_url.stdout.strip()


# ── taslak adaptörü: hafızaya yaz (dosya-blog'u olmayan siteler) ──


def write_draft(site_name: str, art: dict, topic: str) -> str:
    mkey = _envget("MEMORY_API_KEY")
    if not mkey:
        return "no MEMORY_API_KEY"
    details = (
        f"📝 İçerik Editörü taslağı — {site_name} (konu: {topic})\n"
        f"PR-hedefi yok ({SITES[site_name].get('reason', '')}); yayın elle.\n\n"
        f"BAŞLIK (TR): {art['title']['tr']}\nSLUG: {art['slug']}\n"
        f"META: {art['description']['tr']}\n\n--- İÇERİK (TR) ---\n{art['content']['tr'][:3000]}"
    )
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

    # slug çakışma kontrolü (dosya-blog)
    if site["adapter"] == "articles_ts":
        if any(a["slug"] == art["slug"] for a in existing_articles(site)):
            print(f"OUTCOME: fail | slug zaten var: {art['slug']} (farklı konu/açı dene)")
            return 0
        try:
            pr = open_pr(site, art, topic)
            print(f"OUTCOME: pass | makale taslağı PR açıldı: {pr}")
        except Exception as e:
            # PR başarısızsa taslağı kaybetme → hafızaya düş
            err = write_draft(site_name, art, topic)
            print(f"OUTCOME: partial | PR açılamadı ({str(e)[:120]}); taslak hafızaya yazıldı{' (' + err + ')' if err else ''}")
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
