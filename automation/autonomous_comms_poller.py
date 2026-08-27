from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.autonomous_comms.budget import BudgetLimits
from app.core.autonomous_comms.dialogue import DialogueProducer
from app.core.autonomous_comms.pipeline import RuntimeConfig, process_note
from app.core.config import read_env_var
from app.db.data_layer import get_conn

_TRANSIENT_PREFIXES = ("pipeline_failed:", "generation_failed:", "thread_claim_busy", "budget_denied")


def _enabled(name: str) -> bool:
    return (read_env_var(name) or "").strip().casefold() in {"1", "true", "on", "yes"}


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(read_env_var(name) or str(default))
    except ValueError:
        return default
    return value if value > 0 else default


def runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        operator_enabled=_enabled("AUTONOMOUS_COMMS_ACTIVE"),
        max_hops=_positive_int("AUTONOMOUS_COMMS_MAX_HOPS", 3),
        estimated_reply_tokens=_positive_int("AUTONOMOUS_COMMS_REPLY_TOKENS", 384),
        budget_limits=BudgetLimits(
            daily_replies=_positive_int("AUTONOMOUS_COMMS_DAILY_REPLIES", 50),
            daily_tokens=_positive_int("AUTONOMOUS_COMMS_DAILY_TOKENS", 50_000),
            daily_new_threads=_positive_int("AUTONOMOUS_COMMS_DAILY_NEW_THREADS", 5),
            concurrent_in_flight=_positive_int("AUTONOMOUS_COMMS_IN_FLIGHT", 2),
        ),
    )


def process_batch(
    conn: sqlite3.Connection,
    *,
    notes: list[dict],
    device: str,
    last_seen: int,
    producer: DialogueProducer,
    config: RuntimeConfig,
) -> tuple[int, list[dict[str, object]]]:
    results: list[dict[str, object]] = []
    retry_required = False
    valid_ids: list[int] = []
    for item in notes:
        note_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(note_id, int) or note_id <= 0:
            retry_required = True
            continue
        valid_ids.append(note_id)
        result = process_note(
            conn,
            trusted_sender=device,
            source_note_id=note_id,
            config=config,
            producer=producer,
        )
        results.append(
            {
                "note_id": note_id,
                "verdict": result.verdict.value,
                "reason": result.reason,
                "outgoing_note_id": result.outgoing_note_id,
                "correlation_id": result.correlation_id,
            }
        )
        if result.reason.startswith(_TRANSIENT_PREFIXES):
            retry_required = True
    next_seen = last_seen if retry_required else max(valid_ids, default=last_seen)
    return next_seen, results


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: autonomous_comms_poller.py <db_path> <device> [last_seen]")
    db_path, device = sys.argv[1], sys.argv[2]
    last_seen = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    try:
        notes = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        sys.stderr.write(f"phase-c invalid input: {type(exc).__name__}\n")
        print(last_seen)
        return
    if not isinstance(notes, list):
        sys.stderr.write("phase-c input must be a note list\n")
        print(last_seen)
        return
    conn = get_conn(db_path)
    try:
        next_seen, results = process_batch(
            conn,
            notes=notes,
            device=device,
            last_seen=last_seen,
            producer=DialogueProducer(),
            config=runtime_config(),
        )
    except Exception as exc:
        sys.stderr.write(f"phase-c batch failed safely: {type(exc).__name__}\n")
        print(last_seen)
        return
    finally:
        conn.close()
    summary = [{"note_id": item["note_id"], "verdict": item["verdict"], "reason": item["reason"]} for item in results]
    sys.stderr.write(json.dumps({"phase_c": summary}, ensure_ascii=False) + "\n")
    print(next_seen)


if __name__ == "__main__":
    main()
