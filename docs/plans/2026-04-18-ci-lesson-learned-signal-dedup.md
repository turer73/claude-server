# CI Lesson-Learned & Signal De-duplication Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an EvolutionEvent-style lesson store and normalized-signature dedup guard to the Self-Healing CI/CD pipeline so AI auto-fix learns across runs and stops repeating the same first-pass strategy on stuck signals.

**Architecture:** New SQLite table `ci_lesson_learned` in existing `ci_tests.db`; new pure-function module `app/core/ci_signal_dedup.py`; two call sites added to `app/core/ci_fixer.py::attempt_fix()` (dedup check before AI call, lesson record after); summary POST to existing `/api/v1/memory/memories` on successful fix. Env-gated, non-blocking, additive.

**Tech Stack:** Python 3.12, SQLite (stdlib `sqlite3`), pytest + pytest-asyncio, httpx (for memory API), existing Claude Code CLI integration untouched.

**Source design doc:** [docs/plans/2026-04-18-ci-lesson-learned-signal-dedup-design.md](./2026-04-18-ci-lesson-learned-signal-dedup-design.md)

---

## Conventions for every task

- TDD: write the failing test first, run, see it fail, then implement, run, see it pass, commit.
- One logical change per commit. Use conventional commit prefix: `feat`, `test`, `refactor`, `chore`.
- Keep `app/core/ci_signal_dedup.py` functions **pure** — no module-level DB connection, no `datetime.utcnow()` without injection. Accept `db` and (where needed) `now` as parameters. This makes unit tests deterministic and not-flaky.
- After every task's "commit" step, run the full fast test suite once: `pytest tests/test_ci_signal_dedup.py -q` (plus integration tests in the later phases). The full CI build (`run-all-tests.sh`) is for end-of-phase checkpoints.
- Branch: `feat/ci-lesson-learned-signal-dedup` (already created).

---

## Phase 0 — Schema + Module (TDD)

### Task 1: Add `ci_lesson_learned` table migration

**Files:**
- Modify: `scripts/migrate_db.py` (append new migration step)
- Test: `tests/test_ci_schemas.py` (append assertions)

**Step 1: Write the failing test**

Append to `tests/test_ci_schemas.py`:

```python
def test_ci_lesson_learned_schema_exists(ci_test_db):
    cur = ci_test_db.execute("PRAGMA table_info(ci_lesson_learned)")
    cols = {row[1]: row[2] for row in cur.fetchall()}
    assert cols == {
        "id": "INTEGER",
        "run_id": "INTEGER",
        "project": "TEXT",
        "test_name": "TEXT",
        "error_hash": "TEXT",
        "signature": "TEXT",
        "raw_error": "TEXT",
        "attempt_num": "INTEGER",
        "strategy": "TEXT",
        "context_lessons": "TEXT",
        "fix_diff": "TEXT",
        "outcome": "TEXT",
        "duration_ms": "INTEGER",
        "created_at": "DATETIME",
    }

def test_ci_lesson_learned_indexes_exist(ci_test_db):
    names = {r[0] for r in ci_test_db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ci_lesson_learned'"
    ).fetchall()}
    assert "idx_lesson_signature" in names
    assert "idx_lesson_project" in names
```

The `ci_test_db` fixture should live in `tests/conftest.py` — add it there if not already present:

```python
import sqlite3, pytest
from scripts.migrate_db import run_migrations

@pytest.fixture
def ci_test_db(tmp_path):
    db = sqlite3.connect(tmp_path / "ci_tests.db")
    run_migrations(db, target="ci_tests")
    yield db
    db.close()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_schemas.py::test_ci_lesson_learned_schema_exists -v`
Expected: FAIL with "no such table: ci_lesson_learned".

**Step 3: Write minimal implementation**

In `scripts/migrate_db.py`, add to the `ci_tests` migrations list:

```python
CI_TESTS_MIGRATIONS.append("""
CREATE TABLE IF NOT EXISTS ci_lesson_learned (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          INTEGER NOT NULL,
  project         TEXT    NOT NULL,
  test_name       TEXT    NOT NULL,
  error_hash      TEXT    NOT NULL,
  signature       TEXT    NOT NULL,
  raw_error       TEXT,
  attempt_num     INTEGER NOT NULL,
  strategy        TEXT    NOT NULL,
  context_lessons TEXT,
  fix_diff        TEXT,
  outcome         TEXT    NOT NULL,
  duration_ms     INTEGER,
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (run_id) REFERENCES ci_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_lesson_signature ON ci_lesson_learned(signature, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_lesson_project   ON ci_lesson_learned(project, created_at DESC);
""")
```

If `CI_TESTS_MIGRATIONS` doesn't exist in its current shape, adapt to match the existing migration runner interface (open `scripts/migrate_db.py` and follow whatever pattern is there — do NOT rewrite the module).

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_schemas.py -v`
Expected: both new tests PASS, all existing schema tests still PASS.

**Step 5: Commit**

```bash
git add scripts/migrate_db.py tests/test_ci_schemas.py tests/conftest.py
git commit -m "feat(ci): add ci_lesson_learned table migration + schema tests"
```

---

### Task 2: `normalize_error()` with default noise patterns

**Files:**
- Create: `app/core/ci_signal_dedup.py`
- Create: `tests/test_ci_signal_dedup.py`

**Step 1: Write the failing test**

```python
# tests/test_ci_signal_dedup.py
from app.core.ci_signal_dedup import normalize_error

def test_normalize_strips_iso_timestamp():
    raw = "Connection failed at 2026-04-18T01:23:45.123Z on port 5432"
    assert normalize_error(raw) == "Connection failed at <TS> on port :<PORT>"

def test_normalize_strips_uuid():
    raw = "job id deadbeef-1234-5678-9abc-def012345678 aborted"
    assert normalize_error(raw) == "job id <UUID> aborted"

def test_normalize_strips_hex_address():
    raw = "segfault at 0xdeadbeef"
    assert normalize_error(raw) == "segfault at <HEX>"

def test_normalize_strips_tmp_path():
    raw = "cannot write /tmp/pytest-abc/test.txt"
    assert normalize_error(raw) == "cannot write <TMPPATH>"

def test_normalize_strips_user_home_path():
    raw = "open /home/klipperos/foo failed"
    assert normalize_error(raw) == "open <USERPATH> failed"

def test_normalize_strips_bigint():
    raw = "epoch 1745000000000 exceeded"
    assert normalize_error(raw) == "epoch <BIGINT> exceeded"

def test_normalize_idempotent():
    raw = "timestamp 2026-04-18T01:23:45Z"
    once = normalize_error(raw)
    twice = normalize_error(once)
    assert once == twice
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: FAIL with "ModuleNotFoundError: app.core.ci_signal_dedup".

**Step 3: Write minimal implementation**

```python
# app/core/ci_signal_dedup.py
"""Signal-based de-duplication and lesson-learned storage for CI auto-fix.

Pure functions. DB connections are injected for testability.
See: docs/plans/2026-04-18-ci-lesson-learned-signal-dedup-design.md
"""
from __future__ import annotations
import hashlib
import re
from typing import Iterable

NOISE_PATTERNS: list[tuple[str, str]] = [
    (r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[\.\d]*Z?', '<TS>'),
    (r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}',          '<TS>'),
    (r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<UUID>'),
    (r'0x[0-9a-f]+',                                    '<HEX>'),
    (r'/tmp/[^\s)\'"]+',                                '<TMPPATH>'),
    (r'(?:/home/|/Users/|C:\\Users\\)[^\s)\'"]+',       '<USERPATH>'),
    (r':\d{4,5}\b',                                     ':<PORT>'),
    (r'\b\d{10,}\b',                                    '<BIGINT>'),
]

_COMPILED = [(re.compile(pat), sub) for pat, sub in NOISE_PATTERNS]


def normalize_error(raw: str) -> str:
    """Replace noisy substrings with stable placeholders."""
    out = raw
    for rx, repl in _COMPILED:
        out = rx.sub(repl, out)
    return out
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: all 7 tests PASS.

**Step 5: Commit**

```bash
git add app/core/ci_signal_dedup.py tests/test_ci_signal_dedup.py
git commit -m "feat(ci): normalize_error() with 8 default noise patterns"
```

---

### Task 3: `compute_signature()`

**Files:**
- Modify: `app/core/ci_signal_dedup.py`
- Modify: `tests/test_ci_signal_dedup.py`

**Step 1: Write the failing test**

Append:

```python
from app.core.ci_signal_dedup import compute_signature

def test_signature_is_project_testname_hash_triple():
    h, sig = compute_signature("bilge-arena", "test_login", "AssertionError: 5 != 3")
    assert len(h) == 12
    assert sig == f"bilge-arena::test_login::{h}"

def test_signature_stable_across_timestamps():
    _, sig1 = compute_signature("p", "t", "failed at 2026-04-18T01:23:45Z")
    _, sig2 = compute_signature("p", "t", "failed at 2026-04-18T09:99:99Z")
    assert sig1 == sig2

def test_signature_differs_for_different_errors():
    _, sig1 = compute_signature("p", "t", "AssertionError: 5 != 3")
    _, sig2 = compute_signature("p", "t", "KeyError: missing")
    assert sig1 != sig2
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: FAIL with "ImportError: cannot import name 'compute_signature'".

**Step 3: Write minimal implementation**

Append to `app/core/ci_signal_dedup.py`:

```python
def compute_signature(project: str, test_name: str, raw_error: str) -> tuple[str, str]:
    normalized = normalize_error(raw_error)
    error_hash = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return error_hash, f"{project}::{test_name}::{error_hash}"
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: all tests PASS (previous 7 + new 3).

**Step 5: Commit**

```bash
git add app/core/ci_signal_dedup.py tests/test_ci_signal_dedup.py
git commit -m "feat(ci): compute_signature() returns (hash, project::test::hash)"
```

---

### Task 4: `record_lesson()`

**Files:**
- Modify: `app/core/ci_signal_dedup.py`
- Modify: `tests/test_ci_signal_dedup.py`

**Step 1: Write the failing test**

```python
from app.core.ci_signal_dedup import record_lesson

def test_record_lesson_inserts_and_returns_id(ci_test_db):
    # Seed a ci_runs row so FK holds
    ci_test_db.execute("INSERT INTO ci_runs(id, started_at) VALUES (1, CURRENT_TIMESTAMP)")
    ci_test_db.commit()
    row_id = record_lesson(
        ci_test_db,
        run_id=1, project="p", test_name="t",
        error_hash="abc123abc123", signature="p::t::abc123abc123",
        raw_error="AssertionError", attempt_num=1, strategy="fix-direct",
        context_lessons=None, fix_diff="diff --git ...", outcome="passed",
        duration_ms=420,
    )
    assert row_id > 0
    row = ci_test_db.execute(
        "SELECT project, outcome, strategy FROM ci_lesson_learned WHERE id=?", (row_id,)
    ).fetchone()
    assert row == ("p", "passed", "fix-direct")

def test_record_lesson_truncates_fix_diff(ci_test_db):
    ci_test_db.execute("INSERT INTO ci_runs(id, started_at) VALUES (1, CURRENT_TIMESTAMP)")
    ci_test_db.commit()
    big = "x" * 10000
    row_id = record_lesson(
        ci_test_db, run_id=1, project="p", test_name="t",
        error_hash="h", signature="p::t::h", raw_error="e", attempt_num=1,
        strategy="fix-direct", context_lessons=None, fix_diff=big,
        outcome="failed", duration_ms=0,
    )
    stored = ci_test_db.execute(
        "SELECT fix_diff FROM ci_lesson_learned WHERE id=?", (row_id,)
    ).fetchone()[0]
    assert len(stored) <= 4096
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: FAIL with "ImportError: cannot import name 'record_lesson'".

**Step 3: Write minimal implementation**

Append to `app/core/ci_signal_dedup.py`:

```python
FIX_DIFF_CAP = 4096

def record_lesson(
    db, *, run_id: int, project: str, test_name: str,
    error_hash: str, signature: str, raw_error: str | None,
    attempt_num: int, strategy: str,
    context_lessons: str | None, fix_diff: str | None,
    outcome: str, duration_ms: int | None,
) -> int:
    """Insert one lesson row, returning its id. fix_diff is truncated to FIX_DIFF_CAP."""
    if fix_diff is not None and len(fix_diff) > FIX_DIFF_CAP:
        fix_diff = fix_diff[:FIX_DIFF_CAP]
    cur = db.execute(
        """
        INSERT INTO ci_lesson_learned
            (run_id, project, test_name, error_hash, signature, raw_error,
             attempt_num, strategy, context_lessons, fix_diff, outcome, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, project, test_name, error_hash, signature, raw_error,
         attempt_num, strategy, context_lessons, fix_diff, outcome, duration_ms),
    )
    db.commit()
    return cur.lastrowid
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: all PASS.

**Step 5: Commit**

```bash
git add app/core/ci_signal_dedup.py tests/test_ci_signal_dedup.py
git commit -m "feat(ci): record_lesson() inserts into ci_lesson_learned, caps fix_diff"
```

---

### Task 5: `get_recent_occurrences()`

**Files:**
- Modify: `app/core/ci_signal_dedup.py`
- Modify: `tests/test_ci_signal_dedup.py`

**Step 1: Write the failing test**

```python
from app.core.ci_signal_dedup import get_recent_occurrences

def _seed_runs(db, n: int) -> list[int]:
    ids = []
    for _ in range(n):
        cur = db.execute("INSERT INTO ci_runs(started_at) VALUES (CURRENT_TIMESTAMP)")
        ids.append(cur.lastrowid)
    db.commit()
    return ids

def _seed_lesson(db, run_id: int, signature: str, outcome: str):
    db.execute(
        """INSERT INTO ci_lesson_learned
           (run_id, project, test_name, error_hash, signature, raw_error,
            attempt_num, strategy, outcome)
           VALUES (?, 'p', 't', 'h', ?, 'e', 1, 'fix-direct', ?)""",
        (run_id, signature, outcome),
    )
    db.commit()

def test_get_recent_occurrences_counts_failed_only(ci_test_db):
    r1, r2, r3 = _seed_runs(ci_test_db, 3)
    sig = "p::t::abc"
    _seed_lesson(ci_test_db, r1, sig, "failed")
    _seed_lesson(ci_test_db, r2, sig, "passed")   # should not count
    _seed_lesson(ci_test_db, r3, sig, "failed")
    assert get_recent_occurrences(ci_test_db, sig, window=3) == 2

def test_get_recent_occurrences_respects_window(ci_test_db):
    r1, r2, r3, r4 = _seed_runs(ci_test_db, 4)
    sig = "p::t::abc"
    for r in (r1, r2, r3, r4):
        _seed_lesson(ci_test_db, r, sig, "failed")
    # Only last 3 runs (r2, r3, r4) should be counted
    assert get_recent_occurrences(ci_test_db, sig, window=3) == 3

def test_get_recent_occurrences_zero_when_empty(ci_test_db):
    assert get_recent_occurrences(ci_test_db, "nope", window=3) == 0
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: FAIL with ImportError.

**Step 3: Write minimal implementation**

Append:

```python
def get_recent_occurrences(db, signature: str, window: int = 3) -> int:
    """Count 'failed' attempts with this signature in the most recent `window` runs."""
    row = db.execute(
        """
        WITH recent AS (
            SELECT id FROM ci_runs ORDER BY id DESC LIMIT ?
        )
        SELECT COUNT(*) FROM ci_lesson_learned
        WHERE signature = ?
          AND outcome = 'failed'
          AND run_id IN (SELECT id FROM recent)
        """,
        (window, signature),
    ).fetchone()
    return int(row[0] or 0)
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: all PASS.

**Step 5: Commit**

```bash
git add app/core/ci_signal_dedup.py tests/test_ci_signal_dedup.py
git commit -m "feat(ci): get_recent_occurrences() counts failed attempts in run window"
```

---

### Task 6: `fetch_lesson_context()`

**Files:**
- Modify: `app/core/ci_signal_dedup.py`
- Modify: `tests/test_ci_signal_dedup.py`

**Step 1: Write the failing test**

```python
from app.core.ci_signal_dedup import fetch_lesson_context

def test_fetch_lesson_context_returns_newest_first(ci_test_db):
    r1, r2, r3 = _seed_runs(ci_test_db, 3)
    sig = "p::t::abc"
    # Seed in ascending order; newest should come back first
    _seed_lesson(ci_test_db, r1, sig, "failed")
    _seed_lesson(ci_test_db, r2, sig, "failed")
    _seed_lesson(ci_test_db, r3, sig, "passed")
    rows = fetch_lesson_context(ci_test_db, "p", sig, limit=5)
    assert len(rows) == 3
    assert rows[0]["run_id"] > rows[1]["run_id"] > rows[2]["run_id"]

def test_fetch_lesson_context_respects_limit(ci_test_db):
    run_ids = _seed_runs(ci_test_db, 7)
    sig = "p::t::abc"
    for r in run_ids:
        _seed_lesson(ci_test_db, r, sig, "failed")
    rows = fetch_lesson_context(ci_test_db, "p", sig, limit=3)
    assert len(rows) == 3

def test_fetch_lesson_context_scoped_to_project(ci_test_db):
    r1 = _seed_runs(ci_test_db, 1)[0]
    _seed_lesson(ci_test_db, r1, "p::t::abc", "failed")
    rows = fetch_lesson_context(ci_test_db, "other-project", "p::t::abc", limit=5)
    assert rows == []
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: FAIL with ImportError.

**Step 3: Write minimal implementation**

Append:

```python
def fetch_lesson_context(
    db, project: str, signature: str, limit: int = 5,
) -> list[dict]:
    """Return past lessons matching signature for this project, newest first."""
    cur = db.execute(
        """
        SELECT id, run_id, attempt_num, strategy, outcome, fix_diff, raw_error,
               created_at
        FROM ci_lesson_learned
        WHERE project = ? AND signature = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (project, signature, limit),
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: all PASS.

**Step 5: Commit**

```bash
git add app/core/ci_signal_dedup.py tests/test_ci_signal_dedup.py
git commit -m "feat(ci): fetch_lesson_context() returns scoped, ordered past lessons"
```

---

## 🔖 Phase 0 checkpoint

Run the full fast suite: `pytest tests/ -q -x`. Everything must be green.
Run the full CI build once: `bash scripts/run-all-tests.sh` (takes longer). Must be green.
If green, continue to Phase 1. Otherwise, stop and diagnose.

---

## Phase 1 — Passive Integration (write-only)

### Task 7: `ci_fixer.attempt_fix()` records every attempt as a lesson

**Files:**
- Modify: `app/core/ci_fixer.py` (inside `attempt_fix`, around line 188+)
- Modify: `tests/test_ci_fixer.py`

**Step 1: Write the failing test**

Append to `tests/test_ci_fixer.py`:

```python
import asyncio, sqlite3
from unittest.mock import AsyncMock, patch
from app.core.ci_fixer import attempt_fix

def test_attempt_fix_records_lesson(ci_test_db, tmp_path, monkeypatch):
    # Seed a run
    ci_test_db.execute("INSERT INTO ci_runs(id, started_at) VALUES (99, CURRENT_TIMESTAMP)")
    ci_test_db.commit()

    monkeypatch.setenv("CI_TESTS_DB", str(tmp_path / "ci_tests.db"))  # adapt to real config
    # If the module reads the DB path at call-time, pass it in; otherwise patch.

    with patch("app.core.ci_fixer._call_claude_code",
               new=AsyncMock(return_value={"ok": True, "diff": "stub diff"})):
        asyncio.run(attempt_fix(
            run_id=99, project="p", test_name="t_foo",
            raw_error="AssertionError: 1 != 2", attempt_num=1, cwd=str(tmp_path),
        ))

    row = ci_test_db.execute(
        "SELECT project, test_name, strategy, outcome FROM ci_lesson_learned WHERE run_id=99"
    ).fetchone()
    assert row == ("p", "t_foo", "fix-direct", "passed")
```

> **Note:** the exact signature of `attempt_fix` may differ (currently `async def attempt_fix(...)` at `app/core/ci_fixer.py:188`). Read the real signature before writing the test; keep positional/keyword style consistent with the existing callers. The intent of the test is what matters: a lesson row must be inserted per call.

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_fixer.py::test_attempt_fix_records_lesson -v`
Expected: FAIL (no row in ci_lesson_learned).

**Step 3: Write minimal implementation**

Inside `attempt_fix`, after the Claude Code CLI call resolves:

```python
from app.core import ci_signal_dedup as dedup

# ... existing code that calls _call_claude_code and gets result ...
outcome = "passed" if result.get("ok") else "failed"
error_hash, signature = dedup.compute_signature(project, test_name, raw_error)

try:
    dedup.record_lesson(
        get_ci_tests_db(),   # existing accessor or new tiny helper
        run_id=run_id, project=project, test_name=test_name,
        error_hash=error_hash, signature=signature, raw_error=raw_error,
        attempt_num=attempt_num,
        strategy="fix-direct",           # Phase 1: hard-coded; Phase 2 replaces this
        context_lessons=None,
        fix_diff=result.get("diff"),
        outcome=outcome,
        duration_ms=result.get("duration_ms"),
    )
except Exception as e:
    logger.warning("lesson record failed: %s", e)
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_fixer.py::test_attempt_fix_records_lesson -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add app/core/ci_fixer.py tests/test_ci_fixer.py
git commit -m "feat(ci): attempt_fix() records every AI fix attempt as a lesson"
```

---

### Task 8: Memory API summary on successful fix

**Files:**
- Modify: `app/core/ci_fixer.py`
- Modify: `tests/test_ci_fixer.py`

**Step 1: Write the failing test**

```python
def test_attempt_fix_posts_memory_summary_on_success(ci_test_db, tmp_path, monkeypatch):
    ci_test_db.execute("INSERT INTO ci_runs(id, started_at) VALUES (100, CURRENT_TIMESTAMP)")
    ci_test_db.commit()

    posted = []
    async def fake_post(**kw):
        posted.append(kw)

    with patch("app.core.ci_fixer._call_claude_code",
               new=AsyncMock(return_value={"ok": True, "diff": "stub"})), \
         patch("app.core.ci_fixer.post_lesson_summary_to_memory_api", new=fake_post):
        asyncio.run(attempt_fix(
            run_id=100, project="bilge-arena", test_name="t_x",
            raw_error="AssertionError", attempt_num=1, cwd=str(tmp_path),
        ))

    assert len(posted) == 1
    assert posted[0]["type"] == "lesson_learned"
    assert "bilge-arena" in posted[0]["name"]


def test_attempt_fix_skips_memory_post_on_failure(ci_test_db, tmp_path):
    ci_test_db.execute("INSERT INTO ci_runs(id, started_at) VALUES (101, CURRENT_TIMESTAMP)")
    ci_test_db.commit()

    posted = []
    async def fake_post(**kw): posted.append(kw)

    with patch("app.core.ci_fixer._call_claude_code",
               new=AsyncMock(return_value={"ok": False, "diff": None})), \
         patch("app.core.ci_fixer.post_lesson_summary_to_memory_api", new=fake_post):
        asyncio.run(attempt_fix(
            run_id=101, project="p", test_name="t",
            raw_error="AssertionError", attempt_num=1, cwd=str(tmp_path),
        ))

    assert posted == []
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_fixer.py -k "memory_summary or skips_memory" -v`
Expected: FAIL (no `post_lesson_summary_to_memory_api`).

**Step 3: Write minimal implementation**

Add helper to `app/core/ci_fixer.py`:

```python
import httpx
from app.core.config import settings  # existing config pattern

async def post_lesson_summary_to_memory_api(*, type: str, name: str,
                                            description: str, content: str) -> None:
    """Best-effort: POST a lesson summary. Silent on failure."""
    try:
        base = settings.memory_api_base  # e.g. http://100.113.153.62:8420/api/v1/memory
        key = settings.memory_api_key
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{base}/memories",
                headers={"X-Memory-Key": key, "Content-Type": "application/json"},
                json={"type": type, "name": name,
                      "description": description, "content": content},
            )
    except Exception as e:
        logger.warning("memory api post failed: %s", e)
```

Inside `attempt_fix`, after `record_lesson(...)`:

```python
if outcome == "passed":
    await post_lesson_summary_to_memory_api(
        type="lesson_learned",
        name=f"CI fix: {project}/{test_name}",
        description=f"Attempt {attempt_num}, fix-direct — fixed",
        content=(
            f"Run #{run_id}: {test_name} in {project} fixed on attempt {attempt_num}. "
            f"Signature: {signature}. "
            f"Diff length: {len(result.get('diff') or '')} chars."
        ),
    )
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_fixer.py -k "memory_summary or skips_memory" -v`
Expected: both PASS.

**Step 5: Commit**

```bash
git add app/core/ci_fixer.py tests/test_ci_fixer.py
git commit -m "feat(ci): POST lesson summary to memory API on successful AI fix"
```

---

## 🔖 Phase 1 checkpoint

Deploy to staging (Klipper dev branch or feature VM). Let it run 1-2 days of real CI traffic.

Observation script to run daily:

```bash
sqlite3 /opt/linux-ai-server/data/ci_tests.db \
  "SELECT project, signature, COUNT(*) as n
   FROM ci_lesson_learned
   GROUP BY project, signature
   HAVING n >= 2
   ORDER BY n DESC
   LIMIT 20;"
```

Review the top-20 recurring signatures. For each, check whether the normalizer is producing the right hash (same bug → same signature). If you see >20% "same bug, multiple signatures", add new noise patterns to `NOISE_PATTERNS` and commit; if <20%, proceed to Phase 2.

Target: after 1-2 days, ≥30 lessons, <20% signature-drift noise.

---

## Phase 2 — Active Dedup + Context Enrichment

### Task 9: Env-gated dedup check replaces hard-coded strategy

**Files:**
- Modify: `app/core/ci_fixer.py`
- Modify: `tests/test_ci_fixer.py`

**Step 1: Write the failing test**

```python
def test_strategy_switches_to_context_enriched_after_2_failures(ci_test_db, tmp_path):
    # Seed 2 past failures with same signature
    r1, r2 = _seed_runs(ci_test_db, 2)
    sig = "p::t::h"
    _seed_lesson(ci_test_db, r1, sig, "failed")
    _seed_lesson(ci_test_db, r2, sig, "failed")

    # Normalizer happens to produce 'h' for this error — simulate by monkeypatching
    with patch("app.core.ci_signal_dedup.compute_signature",
               return_value=("h", sig)), \
         patch("app.core.ci_fixer._call_claude_code",
               new=AsyncMock(return_value={"ok": True, "diff": "d"})):
        asyncio.run(attempt_fix(
            run_id=_seed_runs(ci_test_db, 1)[0], project="p", test_name="t",
            raw_error="e", attempt_num=1, cwd=str(tmp_path),
        ))

    row = ci_test_db.execute(
        "SELECT strategy FROM ci_lesson_learned ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "context-enriched"


def test_dedup_disabled_by_env_flag(ci_test_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CI_SIGNAL_DEDUP_ENABLED", "0")
    # Seed 2 past failures (should be ignored)
    r1, r2 = _seed_runs(ci_test_db, 2)
    sig = "p::t::h"
    _seed_lesson(ci_test_db, r1, sig, "failed")
    _seed_lesson(ci_test_db, r2, sig, "failed")

    with patch("app.core.ci_signal_dedup.compute_signature",
               return_value=("h", sig)), \
         patch("app.core.ci_fixer._call_claude_code",
               new=AsyncMock(return_value={"ok": True, "diff": "d"})):
        asyncio.run(attempt_fix(
            run_id=_seed_runs(ci_test_db, 1)[0], project="p", test_name="t",
            raw_error="e", attempt_num=1, cwd=str(tmp_path),
        ))

    row = ci_test_db.execute(
        "SELECT strategy FROM ci_lesson_learned ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "fix-direct"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_fixer.py -k "strategy_switches or dedup_disabled" -v`
Expected: FAIL (strategy hard-coded to "fix-direct").

**Step 3: Write minimal implementation**

Replace the hard-coded `strategy="fix-direct"` in `attempt_fix` with:

```python
import os

def _dedup_enabled() -> bool:
    return os.environ.get("CI_SIGNAL_DEDUP_ENABLED", "1") != "0"

# Inside attempt_fix, BEFORE calling _call_claude_code:
error_hash, signature = dedup.compute_signature(project, test_name, raw_error)
strategy = "fix-direct"
context_lessons_rows = None

if _dedup_enabled():
    try:
        recent = dedup.get_recent_occurrences(get_ci_tests_db(), signature, window=3)
        if recent >= 2:
            strategy = "context-enriched"
            context_lessons_rows = dedup.fetch_lesson_context(
                get_ci_tests_db(), project, signature, limit=5,
            )
    except Exception as e:
        logger.warning("dedup check failed, falling back to fix-direct: %s", e)
```

And in the `record_lesson(...)` call, pass:

```python
context_lessons=(
    json.dumps([r["id"] for r in context_lessons_rows])
    if context_lessons_rows else None
),
strategy=strategy,
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_fixer.py -k "strategy_switches or dedup_disabled" -v`
Expected: both PASS. All previous tests also PASS.

**Step 5: Commit**

```bash
git add app/core/ci_fixer.py tests/test_ci_fixer.py
git commit -m "feat(ci): env-gated dedup check selects context-enriched strategy on recurrence"
```

---

### Task 10: `build_fix_prompt()` appends past-lessons block

**Files:**
- Modify: `app/core/ci_fixer.py::build_fix_prompt` (line 66+)
- Modify: `tests/test_ci_fixer.py`

**Step 1: Write the failing test**

```python
from app.core.ci_fixer import build_fix_prompt

def test_prompt_contains_past_lessons_when_provided():
    lessons = [
        {"attempt_num": 1, "strategy": "fix-direct", "outcome": "failed",
         "fix_diff": "diff 1", "raw_error": "err", "created_at": "2026-04-17 10:00:00"},
        {"attempt_num": 2, "strategy": "fix-direct", "outcome": "failed",
         "fix_diff": "diff 2", "raw_error": "err", "created_at": "2026-04-18 09:00:00"},
    ]
    prompt = build_fix_prompt(
        project="p", test_name="t", raw_error="e",
        context_lessons=lessons,
    )
    assert "Past lessons for this signature" in prompt
    assert "diff 1" in prompt
    assert "diff 2" in prompt


def test_prompt_has_no_lessons_section_when_empty():
    prompt = build_fix_prompt(
        project="p", test_name="t", raw_error="e",
        context_lessons=None,
    )
    assert "Past lessons" not in prompt
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_fixer.py -k "prompt_contains or no_lessons_section" -v`
Expected: FAIL (build_fix_prompt has no `context_lessons` parameter yet).

**Step 3: Write minimal implementation**

Modify `build_fix_prompt` signature and body:

```python
def build_fix_prompt(
    *, project: str, test_name: str, raw_error: str,
    context_lessons: list[dict] | None = None,  # NEW
) -> str:
    parts = [
        f"You are fixing a failing test in project {project}.",
        f"Test: {test_name}",
        f"Error:\n{raw_error}",
        # ... existing sections ...
    ]
    if context_lessons:
        block = ["", "## Past lessons for this signature (newest first)"]
        for L in context_lessons:
            block.append(
                f"- attempt {L['attempt_num']} ({L['strategy']}) → {L['outcome']} "
                f"at {L['created_at']}"
            )
            if L.get("fix_diff"):
                block.append(f"  diff:\n  ```\n  {L['fix_diff']}\n  ```")
        parts.append("\n".join(block))
    return "\n\n".join(parts)
```

Then update the call in `attempt_fix` to pass `context_lessons=context_lessons_rows`.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_fixer.py -k "prompt_contains or no_lessons_section" -v`
Expected: both PASS. All previous tests also PASS.

**Step 5: Commit**

```bash
git add app/core/ci_fixer.py tests/test_ci_fixer.py
git commit -m "feat(ci): inject past-lessons block into AI fix prompt when enriched"
```

---

### Task 11: Integration test — full flow end-to-end

**Files:**
- Create: `tests/test_ci_lesson_flow.py`

**Step 1: Write the failing test**

```python
"""End-to-end flow: 3 consecutive runs, same signature fails twice, enriched on 3rd."""
import asyncio, sqlite3
from unittest.mock import AsyncMock, patch
from app.core.ci_fixer import attempt_fix

async def _run_attempt(ci_test_db, run_id, result):
    ci_test_db.execute("INSERT INTO ci_runs(id, started_at) VALUES (?, CURRENT_TIMESTAMP)", (run_id,))
    ci_test_db.commit()
    with patch("app.core.ci_fixer._call_claude_code",
               new=AsyncMock(return_value=result)), \
         patch("app.core.ci_fixer.post_lesson_summary_to_memory_api",
               new=AsyncMock()):
        await attempt_fix(
            run_id=run_id, project="bilge-arena", test_name="t_stuck",
            raw_error="AssertionError: 11 != 12", attempt_num=1, cwd="/tmp",
        )

def test_enrichment_kicks_in_after_two_failures(ci_test_db):
    asyncio.run(_run_attempt(ci_test_db, 1, {"ok": False, "diff": "try 1"}))
    asyncio.run(_run_attempt(ci_test_db, 2, {"ok": False, "diff": "try 2"}))
    # 3rd run: should switch to context-enriched
    asyncio.run(_run_attempt(ci_test_db, 3, {"ok": True,  "diff": "try 3 with context"}))

    strategies = [r[0] for r in ci_test_db.execute(
        "SELECT strategy FROM ci_lesson_learned ORDER BY id"
    ).fetchall()]
    assert strategies == ["fix-direct", "fix-direct", "context-enriched"]

    outcomes = [r[0] for r in ci_test_db.execute(
        "SELECT outcome FROM ci_lesson_learned ORDER BY id"
    ).fetchall()]
    assert outcomes == ["failed", "failed", "passed"]
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_lesson_flow.py -v`
Expected: PASS (everything from previous tasks should already make this pass). If it fails, it exposes a gap in earlier tasks — fix there, not here.

**Step 3: (No new implementation if Step 2 passed.)**

If Step 2 failed, trace back to the first broken assertion and fix the corresponding earlier task.

**Step 4: Run full suite**

Run: `pytest tests/ -q -x`
Expected: all PASS.

**Step 5: Commit**

```bash
git add tests/test_ci_lesson_flow.py
git commit -m "test(ci): end-to-end lesson flow covers 2-fail → enrichment transition"
```

---

## 🔖 Phase 2 checkpoint

1. `pytest tests/ -q` — green
2. `bash scripts/run-all-tests.sh` — green (11/11 projects, 2178+ tests + new ones)
3. Push branch, open PR against `master`:
   - Title: `feat(ci): lesson-learned store + signal de-duplication for AI auto-fix`
   - Body: link the design doc and this plan, enumerate the 11 committed changes, include the rollback recipe (`CI_SIGNAL_DEDUP_ENABLED=0`).
4. After merge, enable on Klipper by setting `CI_SIGNAL_DEDUP_ENABLED=1` (default) and watch first context-enriched Telegram message.

---

## Success metrics (measure at +2 weeks)

Query the lesson DB:

```sql
-- Repeat-fix success rate for enriched vs direct
SELECT strategy,
       SUM(CASE WHEN outcome='passed' THEN 1 ELSE 0 END)*1.0/COUNT(*) AS success_rate,
       COUNT(*) AS n
FROM ci_lesson_learned
WHERE signature IN (
    SELECT signature FROM ci_lesson_learned GROUP BY signature HAVING COUNT(*) >= 2
)
GROUP BY strategy;
```

Target: `context-enriched` success_rate > `fix-direct` success_rate by at least 15pp.

---

## What's deferred (explicitly YAGNI for this plan)

- Model escalation (Sonnet → Opus) — not in scope.
- Strategy presets (balanced/harden/repair-only env var) — Phase 3, later plan.
- Protected Source Files allow-list — Phase 3, later plan.
- Cross-project lesson sharing — Phase 3, later plan.
- Automatic pattern discovery — remains manual.
