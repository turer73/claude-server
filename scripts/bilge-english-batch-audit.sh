#!/bin/bash
# bilge-english-batch-audit.sh — validate next batch of items_raw.jsonl
#
# Single-responsibility: read JSONL, compare to state, validate new lines,
# emit JSON report, atomically update state.
#
# Env:
#   JSONL_PATH (default /home/klipperos/work/bilge-arena-en/data/items_raw.jsonl)
#   STATE_PATH (default /opt/linux-ai-server/data/bilge-english-state.json)
# Exit codes:
#   0 = new batch passed
#   1 = new batch failed (schema or count)
#   2 = no new lines
#   3 = IO/usage error

set -euo pipefail

export JSONL_PATH="${JSONL_PATH:-/home/klipperos/work/bilge-arena-en/data/items_raw.jsonl}"
export STATE_PATH="${STATE_PATH:-/opt/linux-ai-server/data/bilge-english-state.json}"

if [[ ! -f "$JSONL_PATH" ]]; then
    echo '{"verdict":"error","reason":"jsonl_not_found","path":"'"$JSONL_PATH"'"}'
    exit 3
fi

exec python3 <<'PY'
import os, sys, json, datetime
from collections import Counter

JSONL = os.environ["JSONL_PATH"]
STATE = os.environ["STATE_PATH"]

DEFAULT_STATE = {
    "last_processed_line": 0,
    "current_batch": 0,
    "current_phase": "1.2.2",
    "expected_total_batches": 20,
    "expected_lines_per_batch": 10,
    "milestones": [5, 10, 15, 20],
    "history": [],
}

REQUIRED = {"type","skill","cefr_target","difficulty_b","discrimination_a",
            "stem","options","correct_index","explanation_tr","source"}
TYPE_SKILL = {"grammar_mcq":"grammar","cloze":"vocabulary",
              "listening":"listening","reading":"reading"}

def emit(payload, code):
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)

# Load or init state
if os.path.exists(STATE):
    try:
        state = json.load(open(STATE))
    except Exception as e:
        emit({"verdict":"error","reason":"state_corrupt","detail":str(e)}, 3)
else:
    state = dict(DEFAULT_STATE)

# Read JSONL (raw lines + line numbers, skip blanks)
items = []
with open(JSONL, encoding="utf-8") as fh:
    for n, raw in enumerate(fh, 1):
        s = raw.strip()
        if s:
            items.append((n, s))
total = len(items)
last = state["last_processed_line"]

if total < last:
    emit({"verdict":"error","reason":"file_shrunk","total":total,"last":last}, 3)

if total == last:
    emit({"verdict":"no_new","total_lines":total,"last_processed":last,
          "current_batch":state["current_batch"]}, 2)

new_items = items[last:]
new_count = len(new_items)
batch = state["current_batch"] + 1
expected = state["expected_lines_per_batch"]

errors = []
parsed = []
for line_no, raw in new_items:
    try:
        o = json.loads(raw)
    except Exception as e:
        errors.append(f"L{line_no}: parse error: {e}")
        continue
    parsed.append((line_no, o))
    miss = REQUIRED - o.keys()
    if miss:
        errors.append(f"L{line_no}: missing {sorted(miss)}")
    ts = o.get("type")
    if ts in TYPE_SKILL and o.get("skill") != TYPE_SKILL[ts]:
        errors.append(f"L{line_no}: type={ts} skill={o.get('skill')} expect={TYPE_SKILL[ts]}")
    b = o.get("difficulty_b")
    if not isinstance(b,(int,float)) or not (-3.0 <= b <= 3.0):
        errors.append(f"L{line_no}: difficulty_b={b}")
    a = o.get("discrimination_a")
    if not isinstance(a,(int,float)) or not (0.5 <= a <= 2.5):
        errors.append(f"L{line_no}: discrimination_a={a}")
    opts = o.get("options")
    if not isinstance(opts, list) or len(opts) < 2:
        errors.append(f"L{line_no}: options invalid")
    ci = o.get("correct_index")
    if not isinstance(ci,int) or ci < 0 or (isinstance(opts,list) and ci >= len(opts)):
        errors.append(f"L{line_no}: correct_index={ci} oob")
    if ts == "listening" and not o.get("transcript"):
        errors.append(f"L{line_no}: listening missing transcript")
    if ts == "reading" and not o.get("passage"):
        errors.append(f"L{line_no}: reading missing passage")
    if ts != "listening" and "transcript" in o:
        errors.append(f"L{line_no}: stray transcript on type={ts}")
    if ts != "reading" and "passage" in o:
        errors.append(f"L{line_no}: stray passage on type={ts}")
    ex = o.get("explanation_tr","")
    if len(ex) < 20:
        errors.append(f"L{line_no}: explanation_tr too short ({len(ex)})")

if new_count != expected:
    errors.append(f"batch_size new={new_count} expected={expected}")

# Distribution metrics (informational)
type_count = Counter(o["type"] for _, o in parsed)
ci_dist = Counter(o["correct_index"] for _, o in parsed)
b_vals = [o["difficulty_b"] for _, o in parsed]
b_range = [min(b_vals), max(b_vals)] if b_vals else None
b_unique = len(set(b_vals))
cefrs = sorted({o["cefr_target"] for _, o in parsed})

verdict = "pass" if not errors else "fail"
sample_due = batch in state["milestones"]

report = {
    "verdict": verdict,
    "batch": batch,
    "lines_added": new_count,
    "lines_total": total,
    "lines_range": [last+1, total],
    "phase": state["current_phase"],
    "cefr": cefrs,
    "type_count": dict(type_count),
    "ci_dist": {str(k):v for k,v in ci_dist.items()},
    "b_range": b_range,
    "b_unique": b_unique,
    "sample_audit_due": sample_due,
    "phase_complete": batch >= state["expected_total_batches"],
    "next_batch": batch + 1 if batch < state["expected_total_batches"] else None,
    "errors": errors,
    "validated_at": datetime.datetime.utcnow().isoformat() + "Z",
}

if verdict == "pass":
    state["last_processed_line"] = total
    state["current_batch"] = batch
    state["history"].append({k: report[k] for k in
        ("batch","lines_range","cefr","type_count","ci_dist","b_range","b_unique","sample_audit_due","validated_at")})
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    json.dump(state, open(tmp,"w"), indent=2, ensure_ascii=False)
    os.replace(tmp, STATE)

emit(report, 0 if verdict == "pass" else 1)
PY
