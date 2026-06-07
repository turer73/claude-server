#!/usr/bin/env python3
"""SEO Ajanı — Google Search Console bağlantısı (gerçek arama verisi → hata + öneri).

seo-audit.py teknik-denetim YAPAR (HTML sinyalleri); bu script GERÇEK GSC verisini çeker:
Search Analytics (sorgu/sayfa/CTR/pozisyon), Sitemaps (gönderim/hata), URL Inspection
(index/coverage). Bulguları önceliklendirir + somut düzeltme önerisi verir.

AUTH: service account (sunucu-sunucu, interaktif-OAuth yok → cron-dostu). python-jose (declared
dep) RS256 ile SA-JWT imzala → Google token endpoint → access_token → GSC REST API (urllib).
google-* kütüphanesi GEREKMEZ. SA-key .env'den GSC_SA_KEY_PATH (secret, commit'siz).

KURULUM (kullanıcı, Google-tarafı): (1) GCP'de Search Console API etkinleştir, (2) service
account + JSON key, (3) her GSC property'sinde Settings→Users'a SA e-postasını ekle.

Kullanım: seo-gsc.py [property...]   (default GSC_PROPERTIES; ör. 'sc-domain:panola.app')
Salt-okunur (webmasters.readonly). HATALAR Telegram YERİNE ortak-hafızaya (type=bug →
SessionStart'ta görünür → açılan oturumda düzeltilir) yazılır — mail/Telegram yok.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request

from jose import jwt  # python-jose (pyproject'te DECLARED dep — fresh-install/CI'da mevcut; PyJWT eklemeye gerek yok)

ENV_FILE = os.environ.get("NOTIFY_ENV_FILE", "/opt/linux-ai-server/.env")
API_BASE = os.environ.get("API_BASE", "http://localhost:8420")
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 (URL, parola değil)
GSC_BASE = "https://searchconsole.googleapis.com/webmasters/v3"
# Codex P2: URL Inspection webmasters/v3'te DEĞİL, v1 altında ayrı endpoint.
URLINSPECT_URI = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
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
    """Service-account JSON → imzalı JWT → access_token. python-jose RS256."""
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


def get_access_token_oauth(client: dict, refresh_token: str) -> str:
    """OAuth refresh_token → access_token (kullanıcı-delege; GSC UI service-account'u kabul
    etmediği için bu yol kullanılır — kullanıcı tüm property'lerin sahibi). gsc-oauth-setup.py
    ile bir kez alınan refresh_token'dan her çağrıda taze access_token üretir."""
    c = client.get("installed") or client.get("web") or client
    body = urllib.parse.urlencode(
        {
            "client_id": c["client_id"],
            "client_secret": c["client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
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
            ins = _http(
                URLINSPECT_URI,
                data=json.dumps({"inspectionUrl": u, "siteUrl": prop}).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
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


def _write_bug(prop: str, findings: list[tuple[str, str]]) -> str:
    """Hata içeren property → type=bug discovery (SessionStart-görünür → düzeltilir).
    Telegram/mail YOK (kullanıcı kararı). Dedup: 'GSC: <property>' başlığı."""
    mkey = _envget("MEMORY_API_KEY")
    if not mkey:
        return "no MEMORY_API_KEY"
    body = "🔍 Search Console hataları (seo-gsc):\n" + "\n".join(f"[{s}] {m}" for s, m in findings)
    try:
        _post_json(
            f"{API_BASE}/api/v1/memory/discoveries",
            {
                "device_name": "klipper",
                "project": "linux-ai-server",
                "type": "bug",
                "title": f"GSC: {prop}",
                "details": body[:3800],
                "rationale": "seo-gsc.py — Telegram yok; düzeltme açılan oturumda (ortak-hafıza).",
            },
            {"X-Memory-Key": mkey},
            15,
        )
        return ""
    except Exception as e:
        return str(e)[:150]


def _acquire_token() -> tuple[str, str]:
    """(token, err). OAuth (kullanıcı-delege) ÖNCELİKLİ — GSC UI service-account'u kabul
    etmediği için. OAuth yoksa SA-key'e düşer."""
    oc = _envget("GSC_OAUTH_CLIENT")
    ot = _envget("GSC_OAUTH_TOKEN")
    if oc and ot and os.path.exists(oc) and os.path.exists(ot):
        try:
            with open(oc) as fh:
                client = json.load(fh)
            with open(ot) as fh:
                refresh = json.load(fh)["refresh_token"]
            return get_access_token_oauth(client, refresh), ""
        except Exception as e:
            return "", f"OAuth auth hatası: {str(e)[:120]}"
    sa_path = _envget("GSC_SA_KEY_PATH")
    if sa_path and os.path.exists(sa_path):
        try:
            with open(sa_path) as fh:
                return get_access_token(json.load(fh)), ""
        except Exception as e:
            return "", f"SA auth hatası: {str(e)[:120]}"
    return "", "Kimlik yok: GSC_OAUTH_CLIENT+GSC_OAUTH_TOKEN veya GSC_SA_KEY_PATH gerekli"


def main() -> int:
    token, err = _acquire_token()
    if err:
        print(f"OUTCOME: fail | {err}")
        return 0

    props = sys.argv[1:] or (_envget("GSC_PROPERTIES").split(",") if _envget("GSC_PROPERTIES") else DEFAULT_PROPERTIES)
    props = [p.strip() for p in props if p.strip()]
    results = [audit_property(token, p) for p in props]
    report = build_report(results)
    print(report)

    # Hatalar (P1+P2) → ortak hafıza (type=bug → SessionStart). MAIL/Telegram YOK.
    raised, errs = 0, []
    for r in results:
        actionable = [(s, m) for s, m in r["findings"] if s in ("P1", "P2")]
        if actionable:
            e = _write_bug(r["property"], actionable)
            errs.append(e) if e else None
            raised += 0 if e else 1
    if errs:
        print(f"\nOUTCOME: partial | {len(props)} property, {raised} bug→ortak-hafıza, MEMORY-FAIL: {errs[0]}")
    else:
        print(f"\nOUTCOME: pass | {len(props)} property, {raised} bug→ortak-hafıza (SessionStart, mail yok)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
