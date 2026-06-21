#!/usr/bin/env python3
"""Reklam Uzmanı — reklam-başlatma danışmanı (multi-uzman vizyon 4/4).

İşletmeler HENÜZ ücretli reklam vermiyor → bu uzman "reklam vermeye BAŞLAMAYI" hedefler:
  (1) GSC→reklam köprüsü (deterministik): gerçek arama verisinden reklam-değer kelimeleri
      çıkarır (marka-savunma / striking-distance / yüksek-talep-düşük-CTR).
  (2) Reklam-metni üretimi (LLM, best-effort): top fırsatlar için Türkçe Google Ads RSA
      taslağı (başlık + açıklama) üretir. /claude başarısız olsa da (1) teslim edilir.

ERTELENDİ — Google Ads performans analizi (kullanıcı seçeneği 3): aktif kampanya YOK +
Google Ads API auth-ağır (developer-token + OAuth + manager-account onayı). Kampanya
oluşunca + auth kurulunca eklenir. Şimdi olmayan-kampanya için analiz = boşa emek.

GSC verisi seo-gsc.py'nin kanıtlanmış auth+API client'ından gelir (kod tekrarı yok).
Salt-okunur (GSC webmasters.readonly + /claude read_only). Bulgular ortak-hafızaya
(type=learning → SessionStart), Telegram yok (SEO kardeşiyle tutarlı).

Kullanım: ad-advisor.py [property...]   (default GSC_PROPERTIES; ör. 'sc-domain:panola.app')
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import urllib.parse
from datetime import UTC, datetime, timedelta

# seo-gsc.py'yi yol-ile yükle (tire içerir → normal import edilemez). Auth+_api+_envget+
# _post_json yeniden kullanılır — GSC client'ı tek-kaynak (seo-gsc), drift yok.
_GSC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seo-gsc.py")
_spec = importlib.util.spec_from_file_location("seo_gsc", _GSC_PATH)
gsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gsc)

API_BASE = os.environ.get("API_BASE", "http://localhost:8420")
DAYS = int(os.environ.get("AD_DAYS", "28"))
CLAUDE_TIMEOUT = int(os.environ.get("AD_CLAUDE_TIMEOUT", "180"))

# Reklam-fırsat eşikleri (env-tunable; GSC verisi seyrek → muhafazakâr).
MIN_IMP_STRIKING = int(os.environ.get("AD_MIN_IMP_STRIKING", "20"))
MIN_IMP_LOWCTR = int(os.environ.get("AD_MIN_IMP_LOWCTR", "50"))
LOWCTR_MAX = float(os.environ.get("AD_LOWCTR_MAX", "0.03"))


def _normalize(s: str) -> str:
    """lowercase + alfanumerik-dışı sil → marka/sorgu eşleştirme tutarlı (ayraç-bağımsız).
    '3d-labx' ve '3d labx'/'3dlabx' aynı '3dlabx'e iner (Codex P2)."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _brand_token(prop: str) -> str:
    """'sc-domain:panola.app' veya 'https://kuafor.panola.app/' → normalize ana-etiket
    (marka-savunma). sc-domain ve URL-prefix biçimlerini işler; ayraçlar atılır."""
    host = prop.split("://", 1)[-1]  # URL-prefix ise şemayı at
    host = host.split(":", 1)[-1]  # 'sc-domain:' önekini at
    host = host.strip("/").split("/")[0]  # yalnız host
    return _normalize(host.split(".")[0])  # '3d-labx' → '3dlabx'


def classify(rows: list[dict], brand: str) -> dict[str, list[dict]]:
    """GSC sorgu satırları → reklam-fırsat kovaları. Saf fonksiyon (test edilebilir).

    - brand_defense: marka-adı sorgusu ama poz>3 (rakip üst-sıra kapabilir → savunma reklamı)
    - striking: poz 5-15 + yeterli gösterim (organik yakın → reklam üst-sıra + dönüşüm)
    - low_ctr: yüksek gösterim + düşük CTR (talep var tık az → reklam yakalar)
    """
    buckets: dict[str, list[dict]] = {"brand_defense": [], "striking": [], "low_ctr": []}
    for r in rows:
        q = (r.get("keys") or ["?"])[0]
        imp = r.get("impressions", 0)
        pos = r.get("position", 0)
        ctr = r.get("ctr", 0)
        if brand and brand in _normalize(q) and pos > 3 and imp >= 5:
            buckets["brand_defense"].append({"q": q, "imp": imp, "pos": pos, "ctr": ctr})
        elif 5 <= pos <= 15 and imp >= MIN_IMP_STRIKING:
            buckets["striking"].append({"q": q, "imp": imp, "pos": pos, "ctr": ctr})
        elif imp >= MIN_IMP_LOWCTR and ctr < LOWCTR_MAX:
            buckets["low_ctr"].append({"q": q, "imp": imp, "pos": pos, "ctr": ctr})
    for k in buckets:
        buckets[k] = sorted(buckets[k], key=lambda x: -x["imp"])[:5]
    return buckets


def fetch_queries(token: str, prop: str) -> list[dict]:
    enc = urllib.parse.quote(prop, safe="")
    end = datetime.now(UTC).date()
    start = end - timedelta(days=DAYS)
    sa = gsc._api(
        token,
        f"sites/{enc}/searchAnalytics/query",
        {"startDate": str(start), "endDate": str(end), "dimensions": ["query"], "rowLimit": 50},
    )
    return sa.get("rows", [])


def build_strategy(prop: str, buckets: dict[str, list[dict]]) -> tuple[list[str], list[str]]:
    """(rapor-satırları, reklam-değer-kelimeler). Kelimeler LLM-metni için besleme."""
    labels = {
        "brand_defense": "🛡️ Marka-savunma (rakip kapabilir)",
        "striking": "🎯 Striking-distance (organik yakın → reklam üst-sıra)",
        "low_ctr": "📈 Yüksek-talep düşük-CTR (reklam tık yakalar)",
    }
    lines: list[str] = []
    keywords: list[str] = []
    for key in ("brand_defense", "striking", "low_ctr"):
        items = buckets.get(key) or []
        if not items:
            continue
        lines.append(f"  {labels[key]}:")
        for it in items:
            lines.append(f"    • '{it['q']}' — poz {it['pos']:.1f}, {it['imp']} gösterim, CTR %{it['ctr'] * 100:.1f}")
            keywords.append(it["q"])
    return lines, keywords


def _ad_copy_llm(prop: str, keywords: list[str]) -> str:
    """Top kelimeler için Türkçe Google Ads RSA taslağı (/claude Max-plan, read-only).
    Best-effort: hata/zaman-aşımı → boş döner, strateji yine de teslim edilir."""
    ikey = gsc._envget("INTERNAL_API_KEY")
    if not ikey or not keywords:
        return ""
    kw = ", ".join(f"'{k}'" for k in keywords[:8])
    prompt = (
        f"{prop} işletmesi için Google Ads reklam metni yaz. Hedef kelimeler: {kw}. "
        "Türkçe, dürüst (abartı yok). Her kelime-kümesi için 3 başlık (≤30 karakter) + "
        "2 açıklama (≤90 karakter) ver. Sadece reklam metnini döndür, açıklama/giriş yazma. "
        "Dosya okuma, sadece metin üret."
    )
    try:
        out = gsc._post_json(
            f"{API_BASE}/api/v1/claude/run",
            # Sentez/strateji = Sonnet (model belirtilmezse CLI default'a düşer). Haftalık,
            # tek çağrı → kota önemsiz; güçlü model reklam-metni kalitesi için.
            {"prompt": prompt, "read_only": True, "max_turns": 1, "model": "claude-sonnet-4-6"},
            {"X-API-Key": ikey},
            CLAUDE_TIMEOUT,
        )
        return (out.get("result") or "").strip()
    except Exception:
        return ""


def advise(token: str, prop: str) -> dict:
    rows = fetch_queries(token, prop)
    brand = _brand_token(prop)
    buckets = classify(rows, brand)
    lines, keywords = build_strategy(prop, buckets)
    copy = _ad_copy_llm(prop, keywords) if keywords else ""
    return {"property": prop, "lines": lines, "keywords": keywords, "copy": copy, "n_rows": len(rows)}


def build_report(results: list[dict]) -> str:
    out = ["📢 Reklam Uzmanı — başlatma fırsatları (GSC tabanlı)\n"]
    for r in results:
        if not r["lines"]:
            out.append(f"🟢 {r['property']} — belirgin reklam-fırsatı yok ({r['n_rows']} sorgu tarandı)")
            out.append("")
            continue
        out.append(f"🔵 {r['property']} — {len(r['keywords'])} reklam-değer kelime:")
        out.extend(r["lines"])
        if r["copy"]:
            out.append("  ✍️ Reklam-metni taslağı:")
            out.extend(f"    {ln}" for ln in r["copy"].splitlines() if ln.strip())
        out.append("")
    return "\n".join(out).strip()


def _write_discovery(report: str) -> str:
    mkey = gsc._envget("MEMORY_API_KEY")
    if not mkey:
        return "no MEMORY_API_KEY"
    try:
        gsc._post_json(
            f"{API_BASE}/api/v1/memory/discoveries",
            {
                "device_name": "klipper",
                "project": "linux-ai-server",
                "type": "learning",
                "title": "Reklam fırsatları (ad-advisor)",
                "details": f"📢 Reklam-başlatma danışmanı ({DAYS}g GSC):\n{report[:3800]}",
                "rationale": "ad-advisor.py — GSC→reklam köprüsü + /claude metin (salt-okunur, mail yok).",
            },
            {"X-Memory-Key": mkey},
            15,
        )
        return ""
    except Exception as e:
        return str(e)[:150]


def main() -> int:
    token, err = gsc._acquire_token()
    if err:
        print(f"OUTCOME: fail | GSC kimlik: {err}")
        return 0

    props = sys.argv[1:] or (gsc._envget("GSC_PROPERTIES").split(",") if gsc._envget("GSC_PROPERTIES") else gsc.DEFAULT_PROPERTIES)
    props = [p.strip() for p in props if p.strip()]

    results = []
    for p in props:
        try:
            results.append(advise(token, p))
        except Exception as e:
            results.append({"property": p, "lines": [f"  ⚠️ çekilemedi: {str(e)[:80]}"], "keywords": [], "copy": "", "n_rows": 0})

    report = build_report(results)
    print(report)

    opportunities = sum(len(r["keywords"]) for r in results)
    derr = _write_discovery(report) if opportunities else ""
    if derr:
        print(f"\nOUTCOME: partial | {len(props)} property, {opportunities} fırsat, DISCOVERY-FAIL: {derr}")
    else:
        print(f"\nOUTCOME: pass | {len(props)} property, {opportunities} reklam-fırsatı→ortak-hafıza (mail yok)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
