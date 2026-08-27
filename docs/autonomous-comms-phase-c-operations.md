# Autonomous communications Phase C operations

Phase C is fail-safe by default. `AUTONOMOUS_COMMS_PHASE_C=0` keeps the legacy poller path. Setting it to `1` enables the new pipeline, but replies remain shadow-only until all promotion gates pass.

## Safety invariants

- Sender identity, `msg_type`, `thread_id`, `reply_to`, and `hop_count` are server-derived.
- `legacy` is pinned to dispatch-equivalent hold; dispatch is always held for human handling.
- Kill-switch read errors halt Phase C. Closed/failed/poisoned/expired threads and hop exhaustion reject.
- SQLite claims serialize each thread. `(thread_id, source_note_id)` idempotency and the atomic note-insert/state-transition prevent duplicate replies.
- Reply, token, new-thread, and in-flight capacity are reserved transactionally before LLM work. No DB transaction remains open during an LLM call.
- Semantic repetition and acknowledgement ping-pong are blocked in addition to hop TTL.
- Audit stores decisions and identifiers, never prompts, note content, credentials, or secrets.

## Promotion sequence

1. Deploy with `AUTONOMOUS_COMMS_PHASE_C=1` and `AUTONOMOUS_COMMS_ACTIVE=0`.
2. List redacted shadow candidates through `GET /api/v1/memory/comms/shadow-candidates`, inspect their reply text, and record reviews with `POST /api/v1/memory/comms/promotion/reviews` using the admin credential. `GET /api/v1/memory/comms/promotion` reports pending count and gate status.
3. Minimum default criteria are 100 reviews, routing precision at least 0.95, accepted-response precision at least 0.90, zero critical violations, generation failure rate at most 0.05, and loop-block rate at most 0.10. Metrics must be newer than 24 hours.
4. Record human approval with `PUT /api/v1/memory/comms/promotion/approval`. Approval must be newer than seven days.
5. Set `AUTONOMOUS_COMMS_ACTIVE=1` and restart the poller/service. Active sending occurs only while the env flip, approval, freshness, and every metric threshold remain valid.

The admin/master human credential is required for approval and reviews. Autonomous and device credentials cannot self-approve. The public `NoteCreate` model has no thread, identity, or message-type authority.

## Observe and roll back

Inspect mode and blocking reasons:

```bash
curl -sS -H "X-Memory-Key: $MEMORY_API_KEY_ADMIN" \
  http://127.0.0.1:8420/api/v1/memory/comms/promotion
```

Audit and work queues:

```sql
SELECT decision, reason, COUNT(*)
FROM autonomous_comms_decision_audit
WHERE created_at >= unixepoch('now', '-1 day')
GROUP BY decision, reason;

SELECT type, state, COUNT(*)
FROM work_items
WHERE type LIKE 'autonomous_comms:%'
GROUP BY type, state;
```

Immediate rollback is either:

- set the existing `autonomous_comms_halt.id=1` row to `active=1`; or
- set `AUTONOMOUS_COMMS_ACTIVE=0` to return to shadow; or
- set `AUTONOMOUS_COMMS_PHASE_C=0` to restore the legacy poller path.

Revoking approval through the approval endpoint with `{"approved": false}` also returns the pipeline to shadow without deleting evidence.
