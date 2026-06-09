#!/usr/bin/env python3
"""AdSense OAuth bir-kerelik kurulum — refresh_token üretir + AdSense account ID'yi
otomatik bulur (adsense-monitor.py kullanır).

AdSense Management API service-account'u kabul etmez → kullanıcı-delege OAuth (GSC ile
aynı desen, [[gsc-oauth-setup.py]]). AYNI OAuth client JSON'u kullanılabilir (GCP'de
AdSense Management API ENABLE edilmiş olmalı). GSC token'ına DOKUNMAZ — ayrı token dosyası.

ÖN-KOŞUL (bir kez, tarayıcı/GCP):
  - GCP Console'da OAuth client'ın projesinde "AdSense Management API"yi ENABLE et:
    https://console.cloud.google.com/apis/library/adsense.googleapis.com

AKIŞ (headless-uyumlu, manuel code-paste — GSC ile birebir):
  1. OAuth client JSON'u ver (GSC ile aynısını kullanabilirsin: $GSC_OAUTH_CLIENT).
  2. Script consent URL'i basar → tarayıcıda aç → onayla (turgut.urer@gmail.com).
  3. Tarayıcı http://localhost:8765/?code=... 'a yönlenir (sayfa AÇILMAZ, normal) →
     adres çubuğundaki 'code=' değerini KOPYALA.
  4. Buraya yapıştır → script refresh_token'a çevirir + account ID'yi çeker.

Kullanım:
  python3 adsense-oauth-setup.py /path/to/oauth-client.json [/path/to/token-out.json]
Çıktı token dosyası (default data/adsense-oauth-token.json) chmod 600 yazılır.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 (URL)
SCOPE = "https://www.googleapis.com/auth/adsense.readonly"
ADSENSE_ACCOUNTS = "https://adsense.googleapis.com/v2/accounts"
REDIRECT = "http://localhost:8765"
DEFAULT_OUT = "/opt/linux-ai-server/data/adsense-oauth-token.json"


def _post(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(  # noqa: S310
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read().decode() or "{}")


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read().decode() or "{}")


def main() -> int:
    if len(sys.argv) < 2:
        print("Kullanım: adsense-oauth-setup.py <oauth-client.json> [token-out.json]")
        print("(oauth-client.json: GSC ile aynısını kullanabilirsin — $GSC_OAUTH_CLIENT)")
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
    print("\nÖN-KOŞUL: GCP'de AdSense Management API ENABLE olmalı:")
    print("  https://console.cloud.google.com/apis/library/adsense.googleapis.com\n")
    print("1) Bu URL'i tarayıcıda aç + onayla (turgut.urer@gmail.com ile):\n")
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
    at = tok.get("access_token")
    if not rt:
        print(f"\nHATA: refresh_token alınamadı → {tok}")
        print("(prompt=consent olduğundan gelmeli; code süresi geçtiyse 1-3 tekrarla.)")
        return 1

    # parent-dizin yoksa (fresh install) yazma patlar → önce oluştur.
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump({"refresh_token": rt}, fh)
    os.chmod(out_path, 0o600)
    print(f"\n✅ refresh_token kaydedildi: {out_path} (chmod 600)")

    # AdSense account ID'yi otomatik çek (elle aramaya gerek yok).
    account = ""
    try:
        accs = _get(ADSENSE_ACCOUNTS, at) if at else {}
        names = [a.get("name", "") for a in accs.get("accounts", [])]
        if names:
            account = names[0]  # 'accounts/pub-XXXXXXXXXXXXXXXX'
            print(f"✅ AdSense account ID bulundu: {account}")
            if len(names) > 1:
                print(f"   (birden fazla hesap: {names} — ilki seçildi, gerekirse değiştir)")
        else:
            print("⚠️  accounts.list boş döndü — AdSense API enable mi / hesap aktif mi kontrol et.")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  account ID çekilemedi ({e}) — token yine de geçerli; ID'yi elle ekle.")

    print("\nŞimdi .env'e ekle:")
    print(f"  ADSENSE_OAUTH_CLIENT={client_path}")
    print(f"  ADSENSE_OAUTH_TOKEN={out_path}")
    if account:
        print(f"  ADSENSE_ACCOUNT={account}")
    else:
        print("  ADSENSE_ACCOUNT=accounts/pub-XXXXXXXXXXXXXXXX  # AdSense konsolundan al")
    return 0


if __name__ == "__main__":
    sys.exit(main())
