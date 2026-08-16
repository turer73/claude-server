#!/bin/bash
# KLIPPER'da calisir. Stalwart'a Let's Encrypt / DNS-01 (Cloudflare) yapilandirir.
#
# Neden DNS-01: VPS'te 80 ve 443 Traefik'te. HTTP-01 ve TLS-ALPN-01 icin o
# portlar gerekir, dolayisiyla ikisi de kullanilamaz. Geriye DNS-01 kaliyor.
#
# Token .env'den CALISMA ANINDA okunur. UZAK TARAFA STDIN UZERINDEN gonderilir,
# komut satirina ASLA girmez.
#
# NEDEN (2026-08-16 duzeltmesi): eskiden token, vps-run.sh'e verilen script
# dosyasinin ICINE gomuluyordu ve buradaki yorum "hicbir yerde ekrana basilmaz"
# diyordu. Bu YANILTICIYDI — ekrana basilmiyordu ama sirasiyla:
#   vps-run.sh:58  curl -d "$BODY"          -> token curl'un argv'sinde
#   app/api/vps.py ssh ... shlex.quote(cmd) -> token ssh'in argv'sinde
#   uzak kabuk      komutu calistirir        -> token VPS'te de argv'de
# shlex.quote enjeksiyonu onler, IFSAYI onlemez. /proc'u okuyabilen herhangi bir
# yerel kullanici/surec, uzak kosum suresince production DNS'i degistirebilecek
# bir token yakalayabilirdi. 600 izinli gecici dosya bu yolu kapatmiyor.
#
# Cozum: sir dogrudan `ssh` STDIN'inden gecirilir (klipper'in VPS'e zaten SSH
# anahtari var; pull-vps-backup.sh ayni yolu kullaniyor). Sirri olmayan
# domain/contact degerleri normal kanaldan gidebilir.
set -uo pipefail

ENV_FILE=/opt/linux-ai-server/.env
DOMAIN=${1:-mail.panola.app}
CONTACT=${2:-postmaster@panola.app}

TOKEN=$(grep -m1 '^CLOUDFLARE_PANOLA_WRITE_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
if [ -z "$TOKEN" ]; then
  echo "HATA: CLOUDFLARE_PANOLA_WRITE_TOKEN $ENV_FILE icinde yok" >&2
  exit 1
fi

REMOTE=$(mktemp /tmp/stalwart-acme.XXXXXX.sh)
chmod 600 "$REMOTE"
trap 'rm -f "$REMOTE"' EXIT

cat > "$REMOTE" <<REMOTE_EOF
set -uo pipefail
CFG=/opt/stalwart-mail/data/etc/config.toml
STAMP=\$(date +%Y%m%d%H%M%S)

if grep -q '^\[acme\.' "\$CFG"; then
  echo "ACME blogu ZATEN var — dokunmuyorum. Once elle temizle:"
  grep -n '^\[acme\.' "\$CFG"
  exit 1
fi

cp -a "\$CFG" "\$CFG.pre-acme.\$STAMP"
echo "yedek: \$CFG.pre-acme.\$STAMP"

cat >> "\$CFG" <<'TOML'

[acme."letsencrypt"]
directory = "https://acme-v02.api.letsencrypt.org/directory"
challenge = "dns-01"
provider = "cloudflare"
secret = "__CF_TOKEN__"
domains = ["__DOMAIN__"]
contact = ["__CONTACT__"]
renew-before = "30d"
polling-interval = "15s"
propagation-timeout = "5m"
TOML

# Token'i ayri adimda yerlestiriyoruz ki heredoc icinde genisleme olmasin.
python3 - "\$CFG" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
s = s.replace("__CF_TOKEN__", open("/tmp/.cf_tok").read().strip())
s = s.replace("__DOMAIN__", open("/tmp/.acme_domain").read().strip())
s = s.replace("__CONTACT__", open("/tmp/.acme_contact").read().strip())
open(p, "w").write(s)
PY
shred -u /tmp/.cf_tok /tmp/.acme_domain /tmp/.acme_contact 2>/dev/null || rm -f /tmp/.cf_tok /tmp/.acme_domain /tmp/.acme_contact

echo "--- yazilan blok (sir maskeli) ---"
sed -E 's/^secret = .*/secret = <REDACTED>/' "\$CFG" | sed -n '/^\[acme\./,/^\$/p'

echo "--- servis yeniden baslatiliyor ---"
cd /opt/stalwart-mail && docker compose restart >/dev/null 2>&1
sleep 25

echo "--- ACME log kayitlari ---"
LOG=\$(ls -t /opt/stalwart-mail/data/logs/*.log 2>/dev/null | head -1)
if [ -n "\$LOG" ]; then
  grep -iE 'acme|certificate|order|dns-record' "\$LOG" | tail -25
  [ -z "\$(grep -icE 'acme' "\$LOG")" ] && echo "  (log'da acme satiri yok)"
else
  echo "  log dosyasi yok — docker logs'a bakiliyor:"
  docker logs stalwart-mail --tail 40 2>&1 | grep -iE 'acme|cert|error|warn' | tail -25
fi

echo "--- konteyner durumu ---"
docker inspect stalwart-mail --format '{{.State.Status}} restarts={{.RestartCount}}'
REMOTE_EOF

# 1) SIR: yalnizca stdin uzerinden. Hicbir argv'ye, hicbir script govdesine girmez.
VPS_SSH_HOST=$(grep -m1 '^VPS_HOST=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
if [ -z "$VPS_SSH_HOST" ]; then
  echo "HATA: VPS_HOST $ENV_FILE icinde yok — sir stdin ile gonderilemez" >&2
  exit 1
fi
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
# shellcheck disable=SC2086
if ! printf '%s' "$TOKEN" | ssh $SSH_OPTS "$VPS_SSH_HOST" 'umask 077; cat > /tmp/.cf_tok'; then
  echo "HATA: token uzak tarafa yazilamadi" >&2
  exit 1
fi
echo "token stdin ile gonderildi (komut satirinda gorunmedi)"

# TEMIZLIK BURADAN ITIBAREN UZAK TARAFI DA KAPSAR. Uzak script kendi sonunda
# (yukaridaki `shred -u /tmp/.cf_tok`) siliyor — ama yalnizca oraya VARABILIRSE.
# Asagidaki vps-run.sh cagrisi duserse (timeout, ag, uzak hata) canli DNS token'i
# VPS'in /tmp'sinde asili kalirdi. Trap'i tam token yazildiktan sonra kur ki
# aradaki her cikis yolu kapansin. Uzak silme basarisiz olursa sessizce gecme —
# operatorun elle temizlemesi gerektigini soyle.
# shellcheck disable=SC2064,SC2086
trap 'rm -f "$REMOTE" "${PRE:-}"; ssh $SSH_OPTS "$VPS_SSH_HOST" "shred -u /tmp/.cf_tok 2>/dev/null || rm -f /tmp/.cf_tok" </dev/null >/dev/null 2>&1 || echo "UYARI: uzak /tmp/.cf_tok silinemedi — VPSte ELLE sil" >&2' EXIT

# 2) SIR OLMAYANLAR: domain/contact normal kanaldan gidebilir.
PRE=$(mktemp /tmp/stalwart-acme-pre.XXXXXX.sh); chmod 600 "$PRE"
{
  printf 'umask 077\n'
  printf 'cat > /tmp/.acme_domain <<%s\n%s\n%s\n' "'EOFDOM'" "$DOMAIN" "EOFDOM"
  printf 'cat > /tmp/.acme_contact <<%s\n%s\n%s\n' "'EOFCON'" "$CONTACT" "EOFCON"
  cat "$REMOTE"
} > "$PRE"

/opt/linux-ai-server/scripts/vps-run.sh -t 240 -f "$PRE"
