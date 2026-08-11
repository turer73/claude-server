#!/bin/bash
# Her proje domaini icin info@ kutusu acar (+ panola.app icin postmaster@).
#
# Parolalar guclu uretilir, $6$ hash'li saklanir ve YALNIZCA
# /root/stalwart-credentials.txt (600) dosyasina yazilir — hicbiri stdout'a
# basilmaz. Var olan hesaba DOKUNMAZ: parolasini sifirlamak, calisan bir
# istemciyi sessizce koparmak demektir.
set -uo pipefail

CRED=/root/stalwart-credentials.txt
BASE=http://127.0.0.1:8443
P=$(awk '$1=="webadmin-admin"{print $2}' "$CRED" | head -1)
[ -z "$P" ] && { echo "HATA: webadmin-admin parolasi yok"; exit 1; }
AUTH=$(printf 'admin:%s' "$P" | base64 -w0)

BOXES="info@panola.app info@3d-labx.com info@kokenakademi.com info@bilgearena.com info@renderhane.com postmaster@panola.app"

umask 077
echo "" >> "$CRED"
echo "# --- info kutulari, $(date -Is) ---" >> "$CRED"

CREATED=0; SKIPPED=0; FAILED=0
for box in $BOXES; do
  # DIKKAT: Stalwart olmayan kayit icin de HTTP 200 dondurur ve hatayi
  # GOVDEYE koyar ({"error":"notFound"}). Durum koduna bakan kontrol burada
  # her hesabi "var" sayar ve hicbir kutu acilmaz.
  BODY=$(curl -s -m 15 -H "Authorization: Basic $AUTH" "$BASE/api/principal/$box")
  if printf '%s' "$BODY" | grep -q '"data"'; then
    echo "  zaten var, dokunulmadi: $box"
    SKIPPED=$((SKIPPED+1)); continue
  elif ! printf '%s' "$BODY" | grep -q '"error":"notFound"'; then
    echo "  BELIRSIZ yanit, atlaniyor: $box -> $(printf '%s' "$BODY" | head -c 120)"
    FAILED=$((FAILED+1)); continue
  fi

  PW=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20)
  HASH=$(openssl passwd -6 "$PW")
  CODE=$(curl -s -m 20 -o /tmp/mbresp -w '%{http_code}' -X POST \
    -H "Authorization: Basic $AUTH" -H 'Content-Type: application/json' \
    --data "{\"type\":\"individual\",\"name\":\"$box\",\"emails\":[\"$box\"],\"secrets\":[\"$HASH\"],\"roles\":[\"user\"]}" \
    "$BASE/api/principal")

  if [ "$CODE" = "200" ]; then
    echo "$box  $PW" >> "$CRED"
    echo "  olusturuldu: $box"
    CREATED=$((CREATED+1))
  else
    echo "  BASARISIZ $box (HTTP $CODE): $(head -c 160 /tmp/mbresp)"
    FAILED=$((FAILED+1))
  fi
done
rm -f /tmp/mbresp
chmod 600 "$CRED"

echo
echo "=== Dogrulama: gercek IMAP girisi ==="
# Olusturuldu demek yetmez; hesabin fiilen acilabildigini kanitla.
cat > /tmp/imapchk.py <<'PY'
import imaplib, ssl, sys
ctx = ssl._create_unverified_context()
try:
    c = imaplib.IMAP4_SSL("127.0.0.1", 993, ssl_context=ctx, timeout=15)
    c.login(sys.argv[1], sys.argv[2]); c.select("INBOX"); c.logout()
    print("     OK", sys.argv[1]); sys.exit(0)
except Exception as e:
    print("     BASARISIZ", sys.argv[1], "->", e); sys.exit(1)
PY
VERIFIED=0
while read -r acct pw; do
  case "$acct" in info@*|postmaster@*) ;; *) continue ;; esac
  python3 /tmp/imapchk.py "$acct" "$pw" && VERIFIED=$((VERIFIED+1))
done < <(awk '/^# --- info kutulari/{f=1;next} f && NF==2 {print $1, $2}' "$CRED")
rm -f /tmp/imapchk.py

echo
echo "=== Tum hesaplar ==="
curl -s -m 15 -H "Authorization: Basic $AUTH" "$BASE/api/principal?types=individual" \
 | python3 -c "
import json,sys
d=json.load(sys.stdin)['data']
print('   ', sorted(x['name'] for x in d['items']))
print('    toplam:', d['total'])
"
echo
echo "olusturulan=$CREATED atlanan=$SKIPPED basarisiz=$FAILED dogrulanan=$VERIFIED"
echo "Parolalar: $CRED"
