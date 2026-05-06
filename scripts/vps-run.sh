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
  echo "usage: $(basename "$0") '<command>'" >&2
  exit 2
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

BODY=$(CMD="$CMD" python3 -c 'import json,os; print(json.dumps({"command": os.environ["CMD"]}))')

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
