#!/bin/bash
# Bir domainin MX'ini Stalwart'a cevirir. Google Workspace teslimatini durdurur.
#
#   mx-cutover.sh panola.app mail.panola.app
#
# Sira onemli: ONCE yeni MX eklenir, SONRA eskiler silinir. Tersi yapilirsa
# arada MX'siz bir pencere olusur ve o anda gelen mail kalici olarak reddedilir.
# Yeni kayit dusuk oncelikle (10) eklendigi icin, eskiler durdugu surece
# (Google prio 1-5) trafik bolunmez; devir eskiler silindiginde gerceklesir.
#
# Geri alma icin mevcut MX kayitlari once dosyaya yazilir.
set -uo pipefail

ZONE=${1:?kullanim: mx-cutover.sh <zone> <mail-host>}
MAILHOST=${2:?kullanim: mx-cutover.sh <zone> <mail-host>}
CF=/opt/linux-ai-server/scripts/cf-dns.py
BACKUP=/opt/linux-ai-server/data/mx-backup-$ZONE-$(date +%Y%m%d%H%M%S).txt

echo "=== 1. Mevcut MX kayitlari yedekleniyor ==="
"$CF" list "$ZONE" | awk '$2=="MX"' | tee "$BACKUP"
COUNT=$(wc -l < "$BACKUP")
echo "  $COUNT kayit -> $BACKUP"
[ "$COUNT" -eq 0 ] && { echo "  UYARI: hic MX yok, devam ediliyor"; }

echo
echo "=== 2. Yeni MX ekleniyor (prio 10 — eskiler dururken trafik bolunmez) ==="
"$CF" upsert "$ZONE" MX "$ZONE" "$MAILHOST" --priority 10 || exit 1

echo
echo "=== 3. Eski MX kayitlari siliniyor ==="
while read -r id type name content prio; do
  [ "$type" = "MX" ] || continue
  case "$content" in
    "$MAILHOST") echo "  korunuyor: $content"; continue ;;
  esac
  echo "  siliniyor: $content"
  "$CF" delete "$ZONE" "$id" >/dev/null || echo "    SILINEMEDI: $id"
done < "$BACKUP"

echo
echo "=== 4. Dogrulama ==="
sleep 10
echo "  canli MX:"
dig +short MX "$ZONE" | sed 's/^/    /'
LIVE=$(dig +short MX "$ZONE" | grep -c "$MAILHOST")
OLD=$(dig +short MX "$ZONE" | grep -ci "aspmx\|google")
if [ "$LIVE" -ge 1 ] && [ "$OLD" -eq 0 ]; then
  echo "  SONUC: devir TAMAM — $ZONE artik $MAILHOST'a teslim ediyor"
else
  echo "  SONUC: BEKLENMEYEN durum (yeni=$LIVE eski=$OLD) — asagidaki geri alma hazir"
fi

echo
echo "=== GERI ALMA (gerekirse) ==="
echo "  Yedek: $BACKUP"
awk '$2=="MX"{print "  " ENVIRON["CF"] " upsert " ENVIRON["ZONE"] " MX " ENVIRON["ZONE"] " " $4 " --priority " $5}' \
  CF="$CF" ZONE="$ZONE" "$BACKUP"
echo "  sonra yeni kaydi sil:  $CF list $ZONE | awk '\$2==\"MX\" && \$4==\"$MAILHOST\"'"
