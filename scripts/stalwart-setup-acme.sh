#!/bin/bash
# KLIPPER'da calisir. Stalwart'a Let's Encrypt / DNS-01 (Cloudflare) yapilandirir.
#
# Neden DNS-01: VPS'te 80 ve 443 Traefik'te. HTTP-01 ve TLS-ALPN-01 icin o
# portlar gerekir, dolayisiyla ikisi de kullanilamaz. Geriye DNS-01 kaliyor.
#
# Token .env'den CALISMA ANINDA okunur ve 600 izinli gecici bir dosyaya
# yazilan uzak script'in icine gomulur; hicbir yerde ekrana basilmaz.
# Gecici dosya cikista silinir.
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

# Sirlari uzak tarafa ayri, kisa omurlu dosyalar olarak gonder.
PRE=$(mktemp /tmp/stalwart-acme-pre.XXXXXX.sh); chmod 600 "$PRE"
trap 'rm -f "$REMOTE" "$PRE"' EXIT
{
  printf 'umask 077\n'
  printf 'cat > /tmp/.cf_tok <<%s\n%s\n%s\n' "'EOFTOK'" "$TOKEN" "EOFTOK"
  printf 'cat > /tmp/.acme_domain <<%s\n%s\n%s\n' "'EOFDOM'" "$DOMAIN" "EOFDOM"
  printf 'cat > /tmp/.acme_contact <<%s\n%s\n%s\n' "'EOFCON'" "$CONTACT" "EOFCON"
  cat "$REMOTE"
} > "$PRE"

/opt/linux-ai-server/scripts/vps-run.sh -t 240 -f "$PRE"
