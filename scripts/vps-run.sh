#!/bin/bash
# Execute a command on the Contabo VPS via /api/v1/vps/exec.
# Auth + transport handled here so callers can stay terse:
#
#   vps-run.sh 'docker ps --format "{{.Names}}"'
#   vps-run.sh 'tail -n 50 /var/log/syslog'
#
# Returns stdout, forwards stderr, exits with the remote command's
# exit code. No whitelist gating on the inner command — `ssh` is the
# wrapper, anything you can type into a remote shell goes through.

set -euo pipefail

if [ $# -eq 0 ]; then
  echo "usage: $(basename "$0") [-t SECONDS] '<command>'" >&2
  exit 2
fi

# Optional -t/--timeout flag: bump past the API's default 30 s for apt
# installs, npm bootstraps, image pulls. Default kept at 60 s so a
# typo doesn't quietly hang the API worker for ten minutes.
TIMEOUT=60
if [ "$1" = "-t" ] || [ "$1" = "--timeout" ]; then
  TIMEOUT="$2"
  shift 2
fi

CMD="$*"
API="${VPS_API:-http://127.0.0.1:8420/api/v1}"
ENV_FILE="${ENV_FILE:-/opt/linux-ai-server/.env}"

KEY=$(grep -E '^API_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | head -c 200)
if [ -z "$KEY" ]; then
  echo "error: API_KEY missing from $ENV_FILE" >&2
  exit 3
fi

TOK=$(curl -fsS -X POST "$API/auth/token" \
  -H "Content-Type: application/json" \
  -d "{\"api_key\":\"$KEY\"}" \
  | python3 -c 'import sys,json; print(json.loads(sys.stdin.read())["access_token"])')

BODY=$(CMD="$CMD" TO="$TIMEOUT" python3 -c 'import json,os; print(json.dumps({"command": os.environ["CMD"], "timeout": int(os.environ["TO"])}))')

RESP=$(curl -fsS -X POST "$API/vps/exec" \
  -H "Authorization: Bearer $TOK" \
  -H "Content-Type: application/json" \
  -d "$BODY")

python3 - "$RESP" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
sys.stdout.write(d.get("stdout", ""))
err = d.get("stderr", "")
if err:
    sys.stderr.write(err)
sys.exit(int(d.get("exit_code", 1)))
PY
