#!/bin/bash
# Stalwart mail — P0 guvenlik kapatmasi. VPS'te root olarak calisir.
#
#   /opt/linux-ai-server/scripts/vps-run.sh -f scripts/stalwart-p0-harden.sh
#
# Kapatilan acikliklar (denetim 2026-08-09):
#   1. 4 hesabin da parolasi "Test1234!" ve 587/465/993/143 tum internete acik
#   2. Parolalar hash'siz saklanmis — API duz metin donuyor
#   3. fallback-admin parolasi `docker logs` ciktisinda duz metin
#   4. webadmin 8443 duz HTTP olarak internete acik
#   5. Log yolu bos — 2 gundur hicbir sey yazilmiyor, teshis imkansiz
#
# Uretilen parolalar SADECE /root/stalwart-credentials.txt (600) dosyasina
# yazilir; hicbiri stdout'a basilmaz.
#
# Idempotent DEGIL: her kosuda yeni parola uretir ve eskiyi gecersiz kilar —
# yani ikinci kez calistirmak, kurulmus istemcileri sessizce koparir. Bu
# yuzden kimlik dosyasi doluysa kendini durdurur; bilerek tekrarlamak icin
# --force gerekir.

set -uo pipefail

CFG=/opt/stalwart-mail/data/etc/config.toml
COMPOSE=/opt/stalwart-mail/docker-compose.yml
CRED=/root/stalwart-credentials.txt
STAMP=$(date +%Y%m%d%H%M%S)
ACCOUNTS="admin@panola.app noreply@panola.app admin@3d-labx.com noreply@3d-labx.com"

[ "$(id -u)" -eq 0 ] || { echo "HATA: root gerekli"; exit 1; }
[ -f "$CFG" ] || { echo "HATA: config bulunamadi: $CFG"; exit 1; }

if [ -s "$CRED" ] && [ "${1:-}" != "--force" ]; then
  echo "Kimlik dosyasi zaten dolu: $CRED"
  echo "Bu script daha once calistirilmis. Tekrar kosarsa TUM parolalari"
  echo "yeniden uretir ve kurulu mail istemcilerini koparir."
  echo "Gercekten istiyorsan: $0 --force"
  exit 1
fi

umask 077
echo "### 0. Yedek"
cp -a "$CFG" "$CFG.pre-p0.$STAMP"
cp -a "$COMPOSE" "$COMPOSE.pre-p0.$STAMP"
# Yarim kalmis bir kosudan artik kayit varsa kenara alinacak — ayni hesap icin
# iki satir birakmak, hangisinin gecerli oldugunu belirsizlestirir.
#
# AMA ONCE OKU, SONRA TASI. Basarili bir onceki kosumdan sonra konteyner yeniden
# olusturuldugu icin fallback-admin parolasi `docker logs`'ta ARTIK YOK; tek
# kopyasi bu dosyadaki "webadmin-admin" satiridir. Eskiden dosya burada
# tasiniyor, kimlik dogrulama (asagida) yalnizca log'a bakip bos bulunca
# "parolayi $CRED icinden al" diyerek cikiyordu — oysa $CRED'i az once kendisi
# tasimisti. Yani --force yolu, isaret ettigi dosyayi yok ederek olurdu.
SAVED_ADM=""
if [ -s "$CRED" ]; then
  SAVED_ADM=$(grep -m1 '^webadmin-admin[[:space:]]' "$CRED" | awk '{print $2}')
fi
echo "  $CFG.pre-p0.$STAMP"

# IMAP dogrulayici. Duz 143'te LOGIN kapali oldugu icin STARTTLS/993 sart;
# ayrica "reddedildi" (cikis 1) ile "ulasilamadi" (cikis 2) ayrilmali, yoksa
# tasima hatasi sahte "parola yanlis" olarak okunur.
CHECK=/tmp/stalwart-imap-check.$STAMP.py
cat > "$CHECK" <<'PY'
import imaplib, ssl, sys
def check(account, password, host):
    ctx = ssl._create_unverified_context()
    errs = []
    for mode in ("starttls", "implicit"):
        try:
            if mode == "starttls":
                c = imaplib.IMAP4(host, 143, timeout=15); c.starttls(ctx)
            else:
                c = imaplib.IMAP4_SSL(host, 993, ssl_context=ctx, timeout=15)
        except Exception as e:
            errs.append("%s: %s" % (mode, e)); continue
        try:
            c.login(account, password)
        except imaplib.IMAP4.error as e:
            msg = str(e)
            try: c.logout()
            except Exception: pass
            if "disabled" in msg.lower() or "clear-text" in msg.lower():
                errs.append("%s: %s" % (mode, msg)); continue
            return 1, "reddedildi (%s): %s" % (mode, msg)
        except Exception as e:
            errs.append("%s: %s" % (mode, e)); continue
        try: c.logout()
        except Exception: pass
        return 0, "giris basarili (%s)" % mode
    return 2, "tasima hatasi -> " + " | ".join(errs)
code, detail = check(sys.argv[1], sys.argv[2], "127.0.0.1")
print("     " + detail)
sys.exit(code)
PY

echo "### 1. Mevcut admin ile baglan"
OLDP=$(docker logs stalwart-mail 2>&1 | grep -oP "password '\K[^']+" | head -1)
if [ -z "$OLDP" ] && [ -n "$SAVED_ADM" ]; then
  # Konteyner yeniden olusturulmus (basarili onceki kosum). Kayitli kimlikle devam et.
  echo "  log'da parola yok — $CRED icindeki webadmin-admin kaydi kullaniliyor"
  OLDP="$SAVED_ADM"
fi
if [ -z "$OLDP" ]; then
  echo "HATA: fallback-admin parolasi ne log'da ne $CRED icinde bulunabildi."
  echo "      AUTH'u elle kurman gerekiyor."
  exit 1
fi
AUTH=$(printf 'admin:%s' "$OLDP" | base64 -w0)
api() { curl -s -m 15 -H "Authorization: Basic $AUTH" "$@"; }

if ! api "http://127.0.0.1:8443/api/principal?types=domain" | grep -q '"domain"'; then
  echo "HATA: yonetim API'sine baglanilamadi"; exit 1
fi
echo "  baglanti tamam"

# Kimlik dogrulama BASARILI oldu — eski kayit dosyasini ancak simdi kenara al.
# Once tasinsaydi, yukaridaki kurtarma yolu (SAVED_ADM) kendi kaynagini silmis
# olurdu ve --force calistirmak kimlik dosyasini kalicı olarak kaybettirirdi.
if [ -s "$CRED" ]; then
  mv "$CRED" "$CRED.$STAMP.eski"
  echo "  eski kimlik dosyasi: $CRED.$STAMP.eski"
fi

# --- 2. Ilk hesabi dondur ve $6$ hash'inin GERCEKTEN kabul edildigini kanitla.
# Stalwart birden fazla hash bicimi destekler; yanlis bicim sessizce yazilir ve
# hesap kilitlenir. Once bir hesapta IMAP girisiyle dogrula, sonra digerlerine gec.
echo "### 2. Hash bicimi dogrulaniyor (tek hesap uzerinde)"
FIRST=$(echo $ACCOUNTS | awk '{print $1}')
P=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20)
H=$(openssl passwd -6 "$P")
# GOVDEYI BURADA DA OKU. Asagidaki dongude oldugu gibi, Stalwart principal
# hatasini HTTP 200 + JSON govdesiyle bildirebiliyor. Govde atilirsa reddedilen
# bir rotasyon "PATCH tamam" sayilir; sonraki IMAP dogrulamasi elbette duser ama
# TESHISI YANLIS koyar ("hash bicimi kabul EDILMEDI"), oysa hash'e hic sira
# gelmemistir. Iki hatayi ayirmak, sonraki adimi (yeniden calistir / AUTH'u
# duzelt) dogru secmeyi mumkun kilar.
RESP=$(curl -s -w '\n%{http_code}' -m 15 -X PATCH \
  -H "Authorization: Basic $AUTH" -H 'Content-Type: application/json' \
  --data "[{\"action\":\"set\",\"field\":\"secrets\",\"value\":[\"$H\"]}]" \
  "http://127.0.0.1:8443/api/principal/$FIRST")
CODE=$(printf '%s' "$RESP" | tail -n1)
BODY=$(printf '%s' "$RESP" | sed '$d')
[ "$CODE" = "200" ] || { echo "HATA: PATCH $FIRST -> HTTP $CODE"; exit 1; }
if printf '%s' "$BODY" | grep -qiE '"error"|notFound|"type"[[:space:]]*:[[:space:]]*"[a-zA-Z]*[Ee]rror'; then
  echo "HATA: PATCH $FIRST HTTP 200 dondu ama govdede principal hatasi var:"
  echo "      $(printf '%s' "$BODY" | head -c 200)"
  echo "      Bu bir HASH BICIMI sorunu DEGIL — hesap adini ve AUTH'u kontrol et."
  exit 1
fi

python3 "$CHECK" "$FIRST" "$P"
case $? in
  0) echo "  \$6\$ (SHA-512 crypt) kabul ediliyor" ;;
  1) echo "HATA: \$6\$ hash bicimi kabul EDILMEDI — sunucu bu parolayi reddetti."
     echo "      $FIRST simdi bilinmeyen bir parolada. Yeniden calistir:"
     echo "      fallback-admin hala gecerli oldugu icin script hepsini bastan atar."
     exit 1 ;;
  2) echo "HATA: IMAP'e ulasilamadi — dogrulama YAPILAMADI."
     echo "      Parola dogru olabilir de olmayabilir de; bilmeden digerlerine"
     echo "      dokunmuyorum. 143/993 dinliyor mu diye bak, sonra tekrar calistir."
     exit 1 ;;
esac

{
  echo "# Stalwart mail kimlik bilgileri"
  echo "# uretim: $(date -Is) — scripts/stalwart-p0-harden.sh"
  echo "# IMAP/SMTP: mail.panola.app  (993 IMAPS / 465 SMTPS / 587 STARTTLS)"
  echo
  echo "$FIRST  $P"
} >> "$CRED"

echo "### 3. Kalan hesaplar donduruluyor"
FAIL=0
for acct in $ACCOUNTS; do
  [ "$acct" = "$FIRST" ] && continue
  P=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20)
  H=$(openssl passwd -6 "$P")
  # GOVDEYI DE OKU: Stalwart uygulama-seviyesi principal hatasini HTTP 200 ile
  # JSON govdesinde bildirebiliyor (stalwart-create-mailboxes.sh bunu "notFound"
  # icin zaten ele aliyor). Govde atilirsa reddedilen bir rotasyon BASARILI
  # sayilir ve hesap "Test1234!" ile kalir; son kontrol yalnizca
  # noreply@panola.app'i test ettigi icin digerlerinin sessiz basarisizligi
  # "SONUC: TAMAM" uretebilirdi.
  RESP=$(curl -s -w '\n%{http_code}' -m 15 -X PATCH \
    -H "Authorization: Basic $AUTH" -H 'Content-Type: application/json' \
    --data "[{\"action\":\"set\",\"field\":\"secrets\",\"value\":[\"$H\"]}]" \
    "http://127.0.0.1:8443/api/principal/$acct")
  CODE=$(printf '%s' "$RESP" | tail -n1)
  BODY=$(printf '%s' "$RESP" | sed '$d')
  if [ "$CODE" = "200" ] && ! printf '%s' "$BODY" | grep -qiE '"error"|notFound|"type"[[:space:]]*:[[:space:]]*"[a-zA-Z]*[Ee]rror'; then
    echo "$acct  $P" >> "$CRED"
    echo "  OK  $acct"
  elif [ "$CODE" = "200" ]; then
    echo "  BASARISIZ $acct (HTTP 200 ama govdede hata: $(printf '%s' "$BODY" | head -c 160))"; FAIL=1
  else
    echo "  BASARISIZ $acct (HTTP $CODE)"; FAIL=1
  fi
done

echo "### 4. fallback-admin parolasi donduruluyor"
NEWADM=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20)
ADMHASH=$(openssl passwd -6 "$NEWADM")
if ADMHASH="$ADMHASH" python3 - "$CFG" <<'PY'
import os, re, sys
cfg = sys.argv[1]
s = open(cfg).read()
new, n = re.subn(
    r'(\[authentication\.fallback-admin\][^\[]*?secret\s*=\s*)"[^"]*"',
    lambda m: m.group(1) + '"' + os.environ["ADMHASH"] + '"',
    s, count=1)
if n != 1:
    sys.exit("  fallback-admin secret satiri bulunamadi")
open(cfg, "w").write(new)
PY
then
  { echo; echo "webadmin-admin  $NEWADM"; } >> "$CRED"
  echo "  OK hash'lendi"
else
  echo "  BASARISIZ — fallback-admin duz metin kaldi"; FAIL=1
fi

echo "### 5. Log dizini aciliyor"
# config'deki /opt/stalwart-mail/logs konteyner-ici yol; host karsiligi data/logs
mkdir -p /opt/stalwart-mail/data/logs
echo "  /opt/stalwart-mail/data/logs"

echo "### 6. webadmin 8443 internetten kaldiriliyor"
TS_IP=$(tailscale ip -4 2>/dev/null | head -1)
[ -z "$TS_IP" ] && TS_IP=127.0.0.1
if TS="$TS_IP" python3 - "$COMPOSE" <<'PY'
import os, sys
p = sys.argv[1]
s = open(p).read()
if '"8443:8443"' not in s:
    sys.exit("  8443 satiri beklenen bicimde degil — elle bak")
s = s.replace('"8443:8443"', '"127.0.0.1:8443:8443"\n      - "%s:8443:8443"' % os.environ["TS"])
open(p, "w").write(s)
PY
then
  echo "  OK 8443 -> 127.0.0.1 + $TS_IP"
else
  echo "  ATLANDI"; FAIL=1
fi

echo "### 7. Konteyner yeniden olusturuluyor"
# Recreate ayni zamanda duz-metin admin parolasini tasiyan log'u da imha eder.
cd /opt/stalwart-mail && docker compose up -d --force-recreate 2>&1 | tail -3
sleep 8

echo "### 8. Dogrulama"
chmod 600 "$CRED"
echo "-- konteyner: $(docker inspect stalwart-mail --format '{{.State.Status}} restarts={{.RestartCount}}')"
echo "-- yayinlanan portlar:"; docker port stalwart-mail | sed 's/^/     /'
LEAK=$(docker logs stalwart-mail 2>&1 | grep -c "administrator account")
echo "-- log'da duz-metin parola satiri: $LEAK  (0 olmali)"
echo "-- eski parola hala gecerli mi:"
python3 "$CHECK" noreply@panola.app 'Test1234!'
case $? in
  0) echo "     UYARI: ESKI PAROLA HALA CALISIYOR"; FAIL=1 ;;
  1) echo "     OK reddedildi" ;;
  2) echo "     BELIRSIZ: IMAP'e ulasilamadi, eski parola test EDILEMEDI"; FAIL=1 ;;
esac
rm -f "$CHECK"
echo "-- kimlik dosyasi: $(ls -l "$CRED" | awk '{print $1, $3, $9}')"
echo
echo "Parolalar: $CRED  (sadece root okur)"
if [ $FAIL -eq 0 ]; then
  echo "SONUC: TAMAM"
  exit 0
fi
# CIKIS KODU SART: eskiden yalnizca "KISMI" basilip 0 donuluyordu. vps-run.sh ve
# her otomasyon sertlestirmeyi TAMAMLANMIS sayiyordu — oysa varsayilan kimlik
# bilgileri ya da halka acik yonetim portu hala acik olabilir. Sessiz "basarili"
# raporu, bu script'in onlemek icin var oldugu riski gizler.
echo "SONUC: KISMI — yukaridaki BASARISIZ satirlarina bak"
exit 1
