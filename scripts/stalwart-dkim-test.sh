#!/bin/bash
# DKIM imzasi FIILEN atiliyor mu — yerel teslimatla bakar (disari mail YOK).
#
# Uyari: Stalwart imzayi giden kuyrukta atar. Yerel->yerel teslimat o yoldan
# gecmeyebilir; bu testin "imza yok" demesi kesin kanit DEGILDIR. "Imza var"
# demesi ise kesindir. Sonuc negatifse dis test gerekir.
set -uo pipefail

CRED=/root/stalwart-credentials.txt
FROM=info@panola.app
TO=info@3d-labx.com
FPW=$(awk -v a="$FROM" '$1==a{print $2}' "$CRED" | head -1)
TPW=$(awk -v a="$TO"   '$1==a{print $2}' "$CRED" | head -1)
[ -z "$FPW" ] || [ -z "$TPW" ] && { echo "HATA: parolalar $CRED icinde bulunamadi"; exit 1; }

SUBJ="dkim-test-$(date +%s)"
echo "konu: $SUBJ"

FROM="$FROM" TO="$TO" FPW="$FPW" TPW="$TPW" SUBJ="$SUBJ" python3 <<'PY'
import email, imaplib, os, smtplib, ssl, sys, time
from email.message import EmailMessage

frm, to = os.environ["FROM"], os.environ["TO"]
subj = os.environ["SUBJ"]
ctx = ssl._create_unverified_context()

msg = EmailMessage()
msg["From"], msg["To"], msg["Subject"] = frm, to, subj
msg.set_content("DKIM imza dogrulama testi. Icerik onemsiz.")

print("--- gonderiliyor (587 STARTTLS, kimlik dogrulamali) ---")
try:
    s = smtplib.SMTP("127.0.0.1", 587, timeout=20)
    s.starttls(context=ctx)
    s.login(frm, os.environ["FPW"])
    s.send_message(msg)
    s.quit()
    print("    gonderildi")
except Exception as e:
    sys.exit(f"    GONDERIM BASARISIZ: {e}")

print("--- teslim bekleniyor ---")
raw = None
for attempt in range(12):
    time.sleep(2)
    try:
        c = imaplib.IMAP4_SSL("127.0.0.1", 993, ssl_context=ctx, timeout=15)
        c.login(to, os.environ["TPW"])
        c.select("INBOX")
        typ, data = c.search(None, 'HEADER', 'Subject', subj)
        if typ == "OK" and data[0].split():
            num = data[0].split()[-1]
            typ, fetched = c.fetch(num, "(RFC822)")
            raw = fetched[0][1]
            c.logout()
            break
        c.logout()
    except Exception as e:
        print(f"    deneme {attempt+1}: {e}")
if raw is None:
    sys.exit("    TESLIM EDILMEDI (12 deneme) — kuyruk/log'a bak")

print(f"    teslim edildi ({len(raw)} bayt)")
m = email.message_from_bytes(raw)

print()
print("--- kimlik dogrulama basliklari ---")
found = False
for h in ("DKIM-Signature", "Authentication-Results", "ARC-Seal", "Received-SPF"):
    for v in m.get_all(h, []):
        found = True
        print(f"  {h}: {' '.join(str(v).split())[:200]}")
if not found:
    print("  HICBIRI YOK")

print()
sig = m.get_all("DKIM-Signature", [])
if sig:
    print("SONUC: DKIM imzasi ATILIYOR")
    for v in sig:
        parts = dict(p.strip().split("=", 1) for p in str(v).replace("\n", "").split(";")
                     if "=" in p)
        print(f"  d={parts.get('d')}  s={parts.get('s')}  a={parts.get('a')}")
else:
    print("SONUC: yerel teslimatta imza YOK.")
    print("  Bu kesin degil — imza giden kuyrukta atiliyor olabilir.")
    print("  Kesin kanit icin dis bir adrese test gonderimi gerekir.")
PY
