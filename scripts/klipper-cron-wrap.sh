#!/bin/bash
# klipper-cron-wrap.sh — Cron komutlarini saran wrapper.
# rc!=0 -> klipper-event.sh + n8n self-healing webhook tetikleyici
# Payload: workflow template field'larina tam uyumlu (alert.severity, value vb)

set +e

NAME="${1:-unknown-cron}"
shift

if [ $# -eq 0 ]; then
    /opt/linux-ai-server/scripts/klipper-event.sh "cron-${NAME}" "MISSING-COMMAND"
    exit 2
fi

CMD_STR="$*"
"$@"
RC=$?

if [ "$RC" -eq 0 ]; then
    /opt/linux-ai-server/scripts/klipper-event.sh "cron-${NAME}" "OK"
else
    /opt/linux-ai-server/scripts/klipper-event.sh "cron-${NAME}" "FAIL rc=${RC}"

    # n8n webhook payload — workflow template path'leri tam uyumlu:
    # $json.alert.{source,severity,message,value,threshold} (body wrapping yok, alert root'ta)
    SAFE_CMD=$(printf "%s" "$CMD_STR" | tr -d '\\"`' | tr '\n\r\t' '   ' | head -c 200)
    BODY="{\"alert\":{\"source\":\"klipper-cron-${NAME}\",\"severity\":\"critical\",\"message\":\"cron ${NAME} FAIL rc=${RC} (${SAFE_CMD})\",\"value\":${RC},\"threshold\":0},\"meta\":{\"type\":\"cron_failure\",\"project\":\"klipper-cron\",\"device\":\"klipper\",\"command\":\"${SAFE_CMD}\",\"exit_code\":\"${RC}\",\"auto_fix_eligible\":true,\"hook_source\":\"klipper-cron-wrap\"}}"
    curl -s -X POST --max-time 3 \
        -H "Content-Type: application/json" \
        -d "${BODY}" \
        "http://194.163.134.239:5678/webhook/klipper-alert" > /dev/null 2>&1 || true
fi

exit $RC
