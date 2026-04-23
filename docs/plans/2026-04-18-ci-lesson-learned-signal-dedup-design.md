# CI Lesson-Learned & Signal De-duplication — Design

**Date:** 2026-04-18
**Status:** Approved (brainstorming complete)
**Branch:** `feat/ci-lesson-learned-signal-dedup`
**Inspired by:** [EvoMap/evolver](https://github.com/EvoMap/evolver) — conceptually, not code. Adapted patterns only.

---

## 1. Context & Motivation

Our Self-Healing CI/CD system (v3, 2026-04-12) runs 11 projects × 2178 tests with AI auto-fix (Claude Code CLI, max 3 retry). Today the AI has no memory between attempts — it treats every failure as new. Two gaps:

- **No learning loop:** same bug gets fixed the same way repeatedly, no knowledge accumulates.
- **No pattern-based stuck-detection:** 3-retry is a raw counter, not a signal-based guard. If the same `project::test::error` pattern fails run after run, the AI keeps trying the same first-pass strategy.

Two features close both gaps:

- **EvolutionEvent / `lesson_learned`** — every AI fix attempt records what was tried and the outcome, so future attempts have ground truth to pull from.
- **Signal de-duplication** — compute a normalized signature per failure, detect recurrence across recent runs, switch to a context-enriched strategy when a signal is "stuck".

Both are adapted from Evolver's core ideas. We do NOT pull in Evolver code (GPL-3.0 + source-available transition + host-runtime coupling).

## 2. Goals & Non-Goals

**Goals**
- AI auto-fix has access to relevant past lessons when the same pattern recurs.
- Operators see cross-device summary of what the CI learned (via existing `/api/v1/memory/*`).
- Zero disruption to current CI pipeline — all new behavior is additive, env-gated, non-blocking.

**Non-Goals (this iteration)**
- Model escalation (Sonnet → Opus). Stays single-model.
- Circuit-breaker / test-skip logic. We only enrich context, never block tests.
- Cross-project lesson sharing. Lessons are scoped per-project for now.
- Automatic normalizer pattern learning. Patterns are hand-curated, iterated by a human.

## 3. Architecture

### Existing (unchanged)
- `ci_runner.py` — orchestrator, AI auto-fix caller, 3-retry logic
- `ci_tests.db` — `ci_runs`, `ci_project_results`, `ci_failures`, `ci_test_results`
- `claude_memory.db` + `/api/v1/memory/*` — cross-device memory
- `ci_notify.py` — Telegram direct

### New
- **Table:** `ci_tests.db::ci_lesson_learned`
- **Module:** `app/core/ci_signal_dedup.py` (5 pure functions, DB via DI)
- **Integration:** 2 call sites in `ci_runner.py::auto_fix_test()`

### Dual-layer storage (decision C from brainstorming)
- Layer A — `ci_lesson_learned` in `ci_tests.db`: structured, AI-consumable, hash-indexed, every attempt logged.
- Layer B — `memories` table via API: `type=lesson_learned`, human-readable summary, **only on successful fix**, cross-device visibility.

Single writer (`ci_runner.py`). Dual readers (AI prompt = Layer A via direct SQL; dashboard/CLI = Layer B via memory API).

### Data flow (failing test)

```
CI run starts
  → test fails
    → compute signature = project::test_name::normalized_error_hash
    → get_recent_occurrences(signature, window=3)
        ├─ 0-1 past occurrences → strategy = "fix-direct" (current behavior)
        └─ ≥ 2 past occurrences → strategy = "context-enriched"
              → fetch_lesson_context(project, signature, limit=5)
              → inject "past lessons" block into AI prompt
    → invoke Claude Code CLI
    → capture outcome
    → record_lesson(...)  (Layer A, always)
    → if outcome == "passed": POST /api/v1/memory/memories (Layer B, summary only)
```

## 4. Schema

### `ci_tests.db::ci_lesson_learned`

```sql
CREATE TABLE ci_lesson_learned (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          INTEGER NOT NULL,              -- FK ci_runs
  project         TEXT    NOT NULL,
  test_name       TEXT    NOT NULL,
  error_hash      TEXT    NOT NULL,              -- normalized, sha1[:12]
  signature       TEXT    NOT NULL,              -- project::test_name::error_hash
  raw_error       TEXT,                          -- human-facing original
  attempt_num     INTEGER NOT NULL,              -- 1, 2, 3
  strategy        TEXT    NOT NULL,              -- 'fix-direct' | 'context-enriched'
  context_lessons TEXT,                          -- JSON array of past lesson IDs
  fix_diff        TEXT,                          -- AI applied diff, capped at ~4KB
  outcome         TEXT    NOT NULL,              -- 'passed' | 'failed' | 'error'
  duration_ms     INTEGER,
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (run_id) REFERENCES ci_runs(id)
);
CREATE INDEX idx_lesson_signature ON ci_lesson_learned(signature, created_at DESC);
CREATE INDEX idx_lesson_project   ON ci_lesson_learned(project, created_at DESC);
```

### Memories API payload (Layer B, on successful fix only)

```json
{
  "type": "lesson_learned",
  "name": "CI fix: bilge-arena/test_has_all_projects",
  "description": "2. attempt, context-enriched — 11→12 projects expectation updated",
  "content": "Run #42: kuafor-worker-d1 added as new project; PROJECT_REGISTRY assertion bumped. Signature: bilge-arena::test_has_all_projects::a3f2c1b8d4e9. Past 5 lessons injected into context."
}
```

## 5. Modules

### `app/core/ci_signal_dedup.py` — API

All functions pure; DB connection passed in for testability.

```python
def normalize_error(raw: str) -> str:
    """Apply NOISE_PATTERNS regex list to strip timestamps, UUIDs, paths, etc."""

def compute_signature(project: str, test_name: str, raw_error: str) -> tuple[str, str]:
    """Return (error_hash, full_signature). error_hash = sha1(normalize_error(raw))[:12]."""

def get_recent_occurrences(db, signature: str, window: int = 3) -> int:
    """Count failed attempts with this signature across the last `window` runs."""

def fetch_lesson_context(db, project: str, signature: str, limit: int = 5) -> list[dict]:
    """Return past lessons for this signature, newest first. Used for AI prompt enrichment."""

def record_lesson(db, **fields) -> int:
    """Insert into ci_lesson_learned; return new row id."""
```

### Normalizer patterns (default starter set)

Curated list in `app/core/ci_signal_dedup.py`. First 8 patterns cover the common noise; operators iterate as new sources appear.

```python
NOISE_PATTERNS = [
    (r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[\.\d]*Z?', '<TS>'),
    (r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}',          '<TS>'),
    (r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<UUID>'),
    (r'0x[0-9a-f]+',                                    '<HEX>'),
    (r'/tmp/[^\s)\'"]+',                                '<TMPPATH>'),
    (r'(/home/|/Users/|C:\\Users\\)[^\s)\'"]+',         '<USERPATH>'),
    (r':\d{4,5}\b',                                     ':<PORT>'),
    (r'\b\d{10,}\b',                                    '<BIGINT>'),
]
```

Later additions (CF D1 execution IDs, Supabase request IDs, pytest worker IDs) added as we observe lesson-DB drift.

## 6. Integration Points in `ci_runner.py`

Two call sites inside `auto_fix_test()`:

**Before Claude Code CLI call:**
```python
from app.core.ci_signal_dedup import (
    compute_signature, get_recent_occurrences, fetch_lesson_context,
)
error_hash, signature = compute_signature(project, test_name, raw_error)
recent = get_recent_occurrences(db, signature, window=3)
strategy = "context-enriched" if recent >= 2 else "fix-direct"
extra_context = (
    fetch_lesson_context(db, project, signature, 5)
    if strategy == "context-enriched" else None
)
# extra_context is appended to the AI prompt as a "Past lessons" block
```

**After AI attempt:**
```python
record_lesson(
    db, run_id=run_id, project=project, test_name=test_name,
    error_hash=error_hash, signature=signature, raw_error=raw_error,
    attempt_num=attempt, strategy=strategy,
    context_lessons=json.dumps([l["id"] for l in (extra_context or [])]),
    fix_diff=diff_truncated_4kb, outcome=outcome, duration_ms=ms,
)
if outcome == "passed":
    post_to_memories_api(
        type="lesson_learned",
        name=f"CI fix: {project}/{test_name}",
        description=f"Attempt {attempt}, {strategy} — fixed",
        content=build_narrative(...),
    )
```

## 7. Error Handling

**Non-blocking, best-effort.** Lesson system must never break CI pipeline.

- Dedup check failure → log warning, fall back to `strategy="fix-direct"`.
- Lesson insert failure → log warning, CI run continues.
- Memory API failure → silently skip (consistent with existing memory-session rule "API erisilemiyorsa sessizce atla").
- `fetch_lesson_context` empty → prompt still works without enrichment.

## 8. Testing

### Unit — `tests/test_ci_signal_dedup.py` (~15 tests)
- `normalize_error`: one test per noise pattern (timestamp, UUID, port, hex, path, bigint).
- `compute_signature`: identical errors with different timestamps produce same hash.
- `get_recent_occurrences`: 3-run fixture with mixed signatures.
- `fetch_lesson_context`: limit, DESC ordering.
- `record_lesson`: required-field validation, index usage.

### Integration — `tests/test_ci_runner_lesson_flow.py` (~5 scenarios)
- Fake failing test → dedup check → strategy selection → fix → lesson insert.
- 2 consecutive failures → returns `strategy="context-enriched"`.
- Lesson DB populated → context list non-empty.
- Memory API unreachable → CI run still succeeds.
- DB write failure → warning logged, CI run still succeeds.

### Regression
- No existing test in `tests/test_ci_runner.py` is modified.
- `test_has_all_projects` and other core CI tests must still pass.

## 9. Rollout

### Phase 0 — Schema + Module (half day)
- SQL migration: create `ci_lesson_learned` + 2 indexes.
- Write `app/core/ci_signal_dedup.py` + unit tests.
- `ci_runner.py` untouched. Production silent.

### Phase 1 — Passive observation (1-2 days)
- Add write-only integration to `ci_runner.py`: every AI fix attempt logs a lesson.
- Dedup check NOT yet performed. Strategy hard-coded to `"fix-direct"`.
- Goal: see real signatures in the wild, identify missing normalizer patterns.
- Success criterion: after 1-2 days, ≥30 lessons in DB; manual review shows <20% "same bug, different signature" noise.

### Phase 2 — Active dedup (after Phase 1)
- Enable read integration: dedup check + context enrichment.
- Env var: `CI_SIGNAL_DEDUP_ENABLED=1` (default 1; set to 0 to revert to Phase 1 behavior).
- First successful context-enriched fix triggers a special Telegram message.

### Rollback
`CI_SIGNAL_DEDUP_ENABLED=0` — single env flip, no code change.

## 10. Success Metrics (measure at +2 weeks)

- Same-signature repeat-fix success rate: before vs. after. Target: +15-30%.
- Count of tests stuck at 3-retry with identical signature: target ↓.
- Total `lesson_learned` entries in `memories` table: visible in dashboard.
- False-positive dedup (same signature, different underlying bug): target <10%, sampled manually.

## 11. References

- [EvoMap/evolver repo](https://github.com/EvoMap/evolver) (conceptual inspiration only; no code copied, GPL-3.0 avoided)
- Evolver concepts adapted: EvolutionEvent (→ `ci_lesson_learned`), Signal de-duplication (→ `ci_signal_dedup.py`)
- Evolver concepts NOT adapted: Gene/Capsule asset format, GEP protocol, A2A hub, host-runtime stdout directives, Strategy presets (deferred)
- Existing memory: [project_self_healing_cicd](../../../../Users/sevdi/.claude/projects/F--projelerim/memory/project_self_healing_cicd.md)

---

## Open items (tracked in implementation plan)
- Normalizer patterns beyond the starter 8 — iterated after Phase 1 observations.
- Future: strategy preset env var (balanced/harden/repair-only) — deferred, not in this iteration.
- Future: `Protected Source Files` (AI auto-fix allow-list) — deferred.
