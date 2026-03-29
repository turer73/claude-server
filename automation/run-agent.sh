#!/bin/bash
# Run a named agent via API
AGENT_NAME=$1
API=http://localhost:8420
KEY=REDACTED_API_KEY
TOKEN=$(curl -s -X POST $API/api/v1/auth/token -H "Content-Type: application/json" -d "{\"api_key\": \"$KEY\"}" | python3 -c "import sys,json; print(json.load(sys.stdin)[\"access_token\"])" 2>/dev/null)

if [ -z "$TOKEN" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] FAIL: Cannot get token" >> /var/log/linux-ai-server/agent-runner.log
    exit 1
fi

# Get agent details
DETAIL=$(curl -s $API/api/v1/agents/$AGENT_NAME -H "Authorization: Bearer $TOKEN")
TOOLS=$(echo $DETAIL | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get(\"tools\",[])))")

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Running agent: $AGENT_NAME (tools: $TOOLS)" >> /var/log/linux-ai-server/agent-runner.log

# Execute each tool in the agent steps
STEPS=$(python3 << PYEOF
import yaml, json
with open(f"/var/AI-stump/agents/$AGENT_NAME.yml") as f:
    agent = yaml.safe_load(f)
for step in agent.get("steps", []):
    print(json.dumps(step))
PYEOF
)

echo "$STEPS" | while IFS= read -r step; do
    TOOL=$(echo $step | python3 -c "import sys,json; print(json.load(sys.stdin).get(\"tool\",\"\"))")
    PARAMS=$(echo $step | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get(\"params\",{})))" 2>/dev/null)
    DESC=$(echo $step | python3 -c "import sys,json; print(json.load(sys.stdin).get(\"description\",\"\"))")
    
    case $TOOL in
        shell_exec)
            CMD=$(echo $PARAMS | python3 -c "import sys,json; print(json.load(sys.stdin).get(\"command\",\"\"))")
            RESULT=$(curl -s -X POST $API/api/v1/shell/exec               -H "Authorization: Bearer $TOKEN"               -H "Content-Type: application/json"               -d "{\"command\": \"$CMD\"}")
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)]   step[$TOOL]: $DESC -> $(echo $RESULT | head -c 200)" >> /var/log/linux-ai-server/agent-runner.log
            ;;
        monitor_metrics)
            RESULT=$(curl -s $API/api/v1/monitor/metrics -H "Authorization: Bearer $TOKEN")
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)]   step[$TOOL]: $DESC -> $(echo $RESULT | head -c 200)" >> /var/log/linux-ai-server/agent-runner.log
            ;;
        process_list)
            LIMIT=$(echo $PARAMS | python3 -c "import sys,json; print(json.load(sys.stdin).get(\"limit\",10))" 2>/dev/null)
            SORT=$(echo $PARAMS | python3 -c "import sys,json; print(json.load(sys.stdin).get(\"sort_by\",\"cpu\"))" 2>/dev/null)
            RESULT=$(curl -s "$API/api/v1/system/processes?limit=$LIMIT&sort_by=$SORT" -H "Authorization: Bearer $TOKEN")
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)]   step[$TOOL]: $DESC -> $(echo $RESULT | head -c 200)" >> /var/log/linux-ai-server/agent-runner.log
            ;;
    esac
done

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Agent $AGENT_NAME completed" >> /var/log/linux-ai-server/agent-runner.log
