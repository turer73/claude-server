#!/usr/bin/env python3
"""GSC OAuth bir-kerelik kurulum — refresh_token üretir (seo-gsc.py kullanır).

GSC arayüzü service-account'u kabul etmiyor → kullanıcı-delege OAuth. Kullanıcı tüm
property'lerin sahibi olduğundan kendi hesabıyla yetkilendirir; per-property ekleme YOK.

AKIŞ (headless-uyumlu, manuel code-paste):
  1. OAuth client JSON'u (GCP Desktop-app, indirilen) ver.
  2. Script consent URL'i basar → tarayıcıda aç → onayla.
  3. Tarayıcı http://localhost:8765/?code=... 'a yönlenir (sayfa AÇILMAZ, normal) →
     adres çubuğundaki 'code=' değerini KOPYALA.
  4. Buraya yapıştır → script code'u refresh_token'a çevirir → güvenli dosyaya yazar.

Kullanım:
  python3 gsc-oauth-setup.py /path/to/oauth-client.json [/path/to/token-out.json]
Çıktı token dosyası (default data/gsc-oauth-token.json) chmod 600 yazılır.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 (URL)
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
REDIRECT = "http://localhost:8765"
DEFAULT_OUT = "/opt/linux-ai-server/data/gsc-oauth-token.json"


def _post(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(  # noqa: S310
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read().decode() or "{}")


def main() -> int:
    if len(sys.argv) < 2:
        print("Kullanım: gsc-oauth-setup.py <oauth-client.json> [token-out.json]")
        return 2
    client_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT
    with open(client_path) as fh:
        client = json.load(fh)
    c = client.get("installed") or client.get("web") or client
    cid, csecret = c["client_id"], c["client_secret"]

    params = urllib.parse.urlencode(
        {
            "client_id": cid,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    print("\n1) Bu URL'i tarayıcıda aç + onayla (turgut.urer@gmail.com ile):\n")
    print(f"{AUTH_URI}?{params}\n")
    print("2) Onay sonrası tarayıcı 'http://localhost:8765/?code=...' adresine gider")
    print("   (sayfa açılmaz — NORMAL). Adres çubuğundaki code= değerini kopyala.\n")
    code = input("3) code'u buraya yapıştır: ").strip()
    # Bazen tam URL yapıştırılır → code paramını ayıkla
    if "code=" in code:
        code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query).get("code", [code])[0]

    tok = _post(
        TOKEN_URI,
        {
            "code": code,
            "client_id": cid,
            "client_secret": csecret,
            "redirect_uri": REDIRECT,
            "grant_type": "authorization_code",
        },
    )
    rt = tok.get("refresh_token")
    if not rt:
        print(f"\nHATA: refresh_token alınamadı → {tok}")
        print("(prompt=consent olduğundan gelmeli; code süresi geçtiyse 1-3 tekrarla.)")
        return 1

    # Codex P2: parent-dizin yoksa (fresh install) yazma patlar → önce oluştur.
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump({"refresh_token": rt}, fh)
    os.chmod(out_path, 0o600)
    print(f"\n✅ refresh_token kaydedildi: {out_path} (chmod 600)")
    print("Şimdi .env'e ekle: GSC_OAUTH_CLIENT=<client.json> + GSC_OAUTH_TOKEN=" + out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
