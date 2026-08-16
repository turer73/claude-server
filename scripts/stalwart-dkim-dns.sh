#!/bin/bash
# Stalwart'in FIILEN imzalamada kullandigi ozel anahtardan DKIM DNS degerini
# turetir ve diskteki .pub dosyasiyla karsilastirir.
#
# Neden .pub dosyasini dogrudan kullanmiyoruz: imzalama anahtari ayar
# deposunda (RocksDB), .pub ise kurulum sirasinda diske yazilmis ayri bir
# dosya. Ayrisirlarsa yayinlanan kayit sessizce dogrulanmaz — DKIM "kurulu"
# gorunur ama her mail fail eder. Kaynak, imzayi atan anahtar olmali.
#
# Ciktisi yalnizca ACIK anahtardir; ozel anahtar hicbir zaman basilmaz.
set -uo pipefail

CRED=/root/stalwart-credentials.txt
P=$(awk '$1=="webadmin-admin"{print $2}' "$CRED" | head -1)
[ -z "$P" ] && { echo "HATA: webadmin-admin parolasi yok"; exit 1; }
AUTH=$(printf 'admin:%s' "$P" | base64 -w0)

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
chmod 700 "$TMP"

curl -s -m 20 -H "Authorization: Basic $AUTH" \
  "http://127.0.0.1:8443/api/settings/list" > "$TMP/settings.json"

python3 - "$TMP" <<'PY'
import json, os, subprocess, sys

tmp = sys.argv[1]
items = json.load(open(os.path.join(tmp, "settings.json")))["data"]["items"]

# Imza kimligi ("rsa-panola.app") nokta iceriyor, alan adi da ("headers.0")
# icerebiliyor — bu yuzden split(".") ile ayirmak yanlis sonuc verir.
# Bilinen alan adlarini SONEK olarak esleyip geri kalani kimlik sayiyoruz.
FIELDS = ("algorithm", "canonicalization", "domain", "private-key",
          "report", "selector", "expiration", "set-body-length")

sigs = {}
for key, val in items.items():
    if not key.startswith("signature."):
        continue
    rest = key[len("signature."):]
    for field in FIELDS:
        if rest.endswith("." + field):
            sigs.setdefault(rest[: -(len(field) + 1)], {})[field] = val
            break

if not sigs:
    sys.exit("HATA: ayar deposunda hic signature.* kaydi yok")

for sid, cfg in sorted(sigs.items()):
    domain = cfg.get("domain", "?")
    selector = cfg.get("selector", "?")
    priv = cfg.get("private-key", "")
    print(f"=== {domain}  (selector: {selector}, id: {sid}) ===")
    if not priv:
        print("  HATA: private-key bos"); continue

    keyfile = os.path.join(tmp, f"{sid}.key")
    with open(os.open(keyfile, os.O_WRONLY | os.O_CREAT, 0o600), "w") as fh:
        fh.write(priv if priv.endswith("\n") else priv + "\n")

    proc = subprocess.run(["openssl", "rsa", "-in", keyfile, "-pubout"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print("  HATA: ozel anahtardan acik anahtar turetilemedi:",
              proc.stderr.strip()[:200])
        continue

    derived = "".join(l for l in proc.stdout.splitlines() if "-----" not in l)

    pubfile = f"/opt/stalwart-mail/dkim/{domain}.pub"
    if os.path.exists(pubfile):
        ondisk = "".join(l for l in open(pubfile).read().splitlines()
                         if "-----" not in l)
        if ondisk == derived:
            print(f"  diskteki {domain}.pub ile AYNI")
        else:
            print(f"  UYARI: diskteki {domain}.pub FARKLI — asagidaki deger")
            print("         (imzalama anahtarindan turetilen) dogru olandir.")
    else:
        print(f"  not: {pubfile} yok, karsilastirma yapilamadi")

    print(f"  DNS adi   : {selector}._domainkey.{domain}")
    print("  DNS tipi  : TXT")
    print(f"  DNS degeri: v=DKIM1; k=rsa; p={derived}")
    print()
PY
