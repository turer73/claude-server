#!/usr/bin/env python3
"""SEO Ajanı — Google Search Console bağlantısı (gerçek arama verisi → hata + öneri).

seo-audit.py teknik-denetim YAPAR (HTML sinyalleri); bu script GERÇEK GSC verisini çeker:
Search Analytics (sorgu/sayfa/CTR/pozisyon), Sitemaps (gönderim/hata), URL Inspection
(index/coverage). Bulguları önceliklendirir + somut düzeltme önerisi verir.

AUTH: service account (sunucu-sunucu, interaktif-OAuth yok → cron-dostu). PyJWT RS256 ile
SA-JWT imzala → Google token endpoint → access_token → GSC REST API. google-* kütüphanesi
GEREKMEZ (PyJWT + requests). SA-key .env'den GSC_SA_KEY_PATH (secret, commit'siz).

KURULUM (kullanıcı, Google-tarafı): (1) GCP'de Search Console API etkinleştir, (2) service
account + JSON key, (3) her GSC property'sinde Settings→Users'a SA e-postasını ekle.

Kullanım: seo-gsc.py [property...]   (default GSC_PROPERTIES; ör. 'sc-domain:panola.app')
Salt-okunur (webmasters.readonly) — GSC'de değişiklik yapmaz.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import jwt  # PyJWT

ENV_FILE = os.environ.get("NOTIFY_ENV_FILE", "/opt/linux-ai-server/.env")
API_BASE = os.environ.get("API_BASE", "http://localhost:8420")
TG_HELPER = os.environ.get("SEO_TG_HELPER", "/opt/linux-ai-server/automation/telegram-alert.sh")
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 (URL, parola değil)
GSC_BASE = "https://searchconsole.googleapis.com/webmasters/v3"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
DAYS = int(os.environ.get("GSC_DAYS", "28"))

DEFAULT_PROPERTIES = [
    "sc-domain:panola.app",  # kuafor/petvet.panola.app subdomain'leri bu domain-property'de
    "sc-domain:bilgearena.com",
    "sc-domain:kokenakademi.com",
    "sc-domain:3d-labx.com",
    "sc-domain:renderhane.com",
]


def _envget(key: str) -> str:
    v = os.environ.get(key)
    if v:
        return v
    try:
        with open(ENV_FILE) as fh:
            for line in fh:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def _http(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers or {})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode() or "{}")


def get_access_token(sa: dict) -> str:
    """Service-account JSON → imzalı JWT → access_token. PyJWT RS256."""
    now = int(time.time())
    claim = {
        "iss": sa["client_email"],
        "scope": SCOPE,
        "aud": TOKEN_URI,
        "iat": now,
        "exp": now + 3600,
    }
    assertion = jwt.encode(claim, sa["private_key"], algorithm="RS256")
    body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}).encode()
    resp = _http(TOKEN_URI, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    return resp["access_token"]


def _api(token: str, path: str, body: dict | None = None) -> dict:
    url = f"{GSC_BASE}/{path}"
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        return _http(url, data=json.dumps(body).encode(), headers=headers)
    return _http(url, headers=headers)


# ── Saf analiz fonksiyonları (mock'la test edilir) ──────────────────────────


def analyze_search(rows: list[dict]) -> list[tuple[str, str]]:
    """searchAnalytics satırları (keys=[query], clicks/impressions/ctr/position) → bulgular.
    Striking-distance (poz 5-20) + yüksek-gösterim-düşük-CTR = başlık/meta fırsatı."""
    f: list[tuple[str, str]] = []
    for r in sorted(rows, key=lambda x: -x.get("impressions", 0))[:10]:
        q = (r.get("keys") or ["?"])[0]
        imp = r.get("impressions", 0)
        ctr = r.get("ctr", 0) * 100
        pos = r.get("position", 0)
        if imp >= 50 and 5 <= pos <= 20:
            f.append(("P2", f"'{q}': poz {pos:.1f} (striking-distance), {imp} gösterim → içerik/başlık güçlendir, ilk-5'e taşı"))
        elif imp >= 100 and ctr < 2:
            f.append(("P2", f"'{q}': {imp} gösterim ama CTR %{ctr:.1f} → başlık/meta-description çekici yap"))
    return f


def analyze_sitemaps(sitemaps: list[dict]) -> list[tuple[str, str]]:
    f: list[tuple[str, str]] = []
    if not sitemaps:
        f.append(("P2", "GSC'ye sitemap GÖNDERİLMEMİŞ → Sitemaps'ten ekle"))
        return f
    for s in sitemaps:
        path = s.get("path", "?")
        errs = int(s.get("errors", 0))
        warns = int(s.get("warnings", 0))
        if errs:
            f.append(("P1", f"sitemap {path}: {errs} HATA → düzelt"))
        elif warns:
            f.append(("P3", f"sitemap {path}: {warns} uyarı"))
    return f


def analyze_inspection(result: dict, url: str) -> list[tuple[str, str]]:
    """urlInspection sonucu → index/coverage hatası."""
    f: list[tuple[str, str]] = []
    idx = (result.get("inspectionResult") or {}).get("indexStatusResult") or {}
    verdict = idx.get("verdict", "")
    cov = idx.get("coverageState", "")
    if verdict and verdict != "PASS":
        f.append(("P1", f"{url}: index VERDICT={verdict} ({cov}) → coverage hatası, incele"))
    elif cov and "indexed" not in cov.lower() and "submitted and indexed" not in cov.lower():
        f.append(("P2", f"{url}: {cov}"))
    return f


def audit_property(token: str, prop: str, inspect_urls: list[str] | None = None) -> dict:
    enc = urllib.parse.quote(prop, safe="")
    findings: list[tuple[str, str]] = []
    # Search Analytics (son DAYS gün, sorgu bazında)
    try:
        from datetime import UTC, datetime, timedelta

        end = datetime.now(UTC).date()
        start = end - timedelta(days=DAYS)
        sa = _api(
            token,
            f"sites/{enc}/searchAnalytics/query",
            {
                "startDate": str(start),
                "endDate": str(end),
                "dimensions": ["query"],
                "rowLimit": 25,
            },
        )
        findings += analyze_search(sa.get("rows", []))
        total_clicks = sum(r.get("clicks", 0) for r in sa.get("rows", []))
        total_imp = sum(r.get("impressions", 0) for r in sa.get("rows", []))
    except Exception as e:
        findings.append(("P1", f"Search Analytics çekilemedi: {str(e)[:100]}"))
        total_clicks = total_imp = 0
    # Sitemaps
    try:
        sm = _api(token, f"sites/{enc}/sitemaps")
        findings += analyze_sitemaps(sm.get("sitemap", []))
    except Exception as e:
        findings.append(("P2", f"Sitemaps çekilemedi: {str(e)[:100]}"))
    # URL Inspection (anahtar sayfalar, opsiyonel — rate-limit'li)
    for u in (inspect_urls or [])[:5]:
        try:
            ins = _api(token, "urlInspection/index:inspect", {"inspectionUrl": u, "siteUrl": prop})
            findings += analyze_inspection(ins, u)
        except Exception:
            pass
    return {"property": prop, "clicks": total_clicks, "impressions": total_imp, "findings": findings}


def build_report(results: list[dict]) -> str:
    lines = ["🔍 Google Search Console — Denetim\n"]
    for r in results:
        p1 = sum(1 for s, _ in r["findings"] if s == "P1")
        emoji = "🔴" if p1 else ("🟡" if r["findings"] else "🟢")
        lines.append(f"{emoji} {r['property']} — {r['clicks']} tık / {r['impressions']} gösterim ({DAYS}g)")
        for sev, msg in r["findings"][:12]:
            lines.append(f"   [{sev}] {msg}")
        if not r["findings"]:
            lines.append("   ✓ belirgin GSC hatası/fırsatı yok")
        lines.append("")
    return "\n".join(lines).strip()


def _post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode() or "{}")


def _send_telegram(report: str) -> bool:
    if not os.path.exists(TG_HELPER):
        return False
    safe = report.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    try:
        r = subprocess.run(
            [TG_HELPER, "--kind", "generic", "--text", "🔍 <b>GSC Denetimi</b>\n<pre>" + safe[:3500] + "</pre>"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return r.returncode == 0
    except Exception:
        return False


def main() -> int:
    key_path = _envget("GSC_SA_KEY_PATH")
    if not key_path or not os.path.exists(key_path):
        print("OUTCOME: fail | GSC_SA_KEY_PATH yok/bulunamadı (service-account kurulumu gerekli)")
        return 0
    try:
        with open(key_path) as fh:
            sa = json.load(fh)
        token = get_access_token(sa)
    except Exception as e:
        print(f"OUTCOME: fail | GSC auth hatası: {str(e)[:120]}")
        return 0

    props = sys.argv[1:] or (_envget("GSC_PROPERTIES").split(",") if _envget("GSC_PROPERTIES") else DEFAULT_PROPERTIES)
    props = [p.strip() for p in props if p.strip()]
    results = [audit_property(token, p) for p in props]
    report = build_report(results)
    print(report)

    mkey = _envget("MEMORY_API_KEY")
    disc_err = ""
    if mkey:
        try:
            _post_json(
                f"{API_BASE}/api/v1/memory/discoveries",
                {
                    "device_name": "klipper",
                    "project": "linux-ai-server",
                    "type": "learning",
                    "title": "Search Console denetimi (seo-gsc)",
                    "details": f"🔍 {len(props)} property GSC:\n{report[:3800]}",
                    "rationale": "seo-gsc.py (service-account, salt-okunur GSC API).",
                },
                {"X-Memory-Key": mkey},
                15,
            )
        except Exception as e:
            disc_err = str(e)[:120]
    tg = _send_telegram(report)
    print(
        f"\nOUTCOME: {'partial' if disc_err else 'pass'} | {len(props)} property, telegram={tg}"
        + (f", DISCOVERY-FAIL: {disc_err}" if disc_err else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
