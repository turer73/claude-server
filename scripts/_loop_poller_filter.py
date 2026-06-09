#!/usr/bin/env python3
"""Loop poller note filter — klipper-loop-poller.sh tarafindan cagrilir.

stdin: raw GET /notes JSON
argv[1]: baseline_id
argv[2]: state_path

Surer'dan yeni unread notlari bulur, basar, state'i gunceller.
Yeni not varsa stdout'a yazar ve sys.exit(0); yoksa bos cikar.
"""
from __future__ import annotations
import json
import sys

def main() -> None:
    baseline_id = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    state_path  = sys.argv[2] if len(sys.argv) > 2 else None

    raw = sys.stdin.read().strip()
    if not raw:
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    notes = (
        data if isinstance(data, list)
        else (data.get("value") or data.get("notes") or data.get("data") or [])
    )

    fresh = [
        n for n in notes
        if n.get("from_device") == "surer"
        and not n.get("read")
        and int(n.get("id", 0)) > baseline_id
    ]
    fresh.sort(key=lambda n: int(n.get("id", 0)))

    if not fresh:
        return

    for n in fresh:
        print(f"== NOTE #{n['id']} | {n.get('title', '')} ==")
        print(n.get("content", ""))
        print()

    max_id = max(int(n.get("id", 0)) for n in fresh)
    if state_path:
        try:
            with open(state_path, "w") as f:
                json.dump({"last_seen_id": max_id}, f)
        except OSError:
            pass


if __name__ == "__main__":
    main()
