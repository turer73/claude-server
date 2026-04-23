# CI Lesson-Learned & Signal De-duplication Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to dispatch one implementer + two reviewers per task.

**Goal:** Add an EvolutionEvent-style lesson store and normalized-signature dedup guard to the Self-Healing CI/CD pipeline so AI auto-fix learns across runs and stops repeating the same first-pass strategy on stuck signals.

**Architecture:** Extend the existing `server.db` `SCHEMA_V1` (in `app/db/database.py`) with a new table `ci_lesson_learned`. Introduce a new pure/async module `app/core/ci_signal_dedup.py`. Integrate two call sites into `app/core/ci_fixer.py::attempt_fix()` (per-attempt lesson record, plus a dedup check before `_call_claude_code`). POST a narrative summary to the existing `/api/v1/memory/memories` on successful fix. Env-gated, non-blocking, additive.

**Tech Stack:** Python 3.12, aiosqlite (via existing `app.db.database.Database`), pytest + pytest-asyncio, httpx (AsyncClient), existing Claude Code CLI integration untouched.

**Source design doc:** [docs/plans/2026-04-18-ci-lesson-learned-signal-dedup-design.md](./2026-04-18-ci-lesson-learned-signal-dedup-design.md) — note: the design doc's original "separate `ci_tests.db`" and FK-to-`ci_runs` framing is replaced here; we reuse `server.db` and group attempts by `run_uuid` (a uuid4 generated at `attempt_fix` entry). Everything else in the design still holds.

---

## Conventions for every task

- TDD: write the failing test first, run it, see it fail, then implement, run again, see it pass, commit.
- One logical change per commit. Use conventional commit prefix: `feat`, `test`, `refactor`, `chore`.
- Keep `app/core/ci_signal_dedup.py` functions **pure** — no module-level DB, no `datetime.utcnow()` without injection. The two "pure sync" functions (`normalize_error`, `compute_signature`) take only strings. The three "async DB" functions take a `Database` instance (from `app.db.database`) as the first argument.
- Full fast suite after every commit: `pytest tests/ -q -x`. End-of-phase also: `bash scripts/run-all-tests.sh`.
- Branch: `feat/ci-lesson-learned-signal-dedup` (already created, design committed at `cd662fc`).
- Work directory: `F:/projelerim/claude-server`.

---

## Phase 0 — Schema + Module (TDD)

### Task 1: Extend `SCHEMA_V1` with `ci_lesson_learned` + add async `ci_db` fixture

**Files:**
- Modify: `app/db/database.py` (append table + two indexes to `SCHEMA_V1`)
- Modify: `tests/conftest.py` (add `ci_db` async fixture)
- Create: `tests/test_ci_schema.py` (new file — schema assertions; distinct from the existing Pydantic-only `tests/test_ci_schemas.py`)

**Step 1: Write the failing test**

Create `tests/test_ci_schema.py`:

```python
"""Tests for CI lesson_learned DB schema (distinct from Pydantic schemas)."""
import pytest


@pytest.mark.asyncio
async def test_ci_lesson_learned_table_exists(ci_db):
    cursor = await ci_db.conn.execute("PRAGMA table_info(ci_lesson_learned)")
    rows = await cursor.fetchall()
    cols = {row["name"]: row["type"] for row in rows}
    assert cols == {
        "id": "INTEGER",
        "run_uuid": "TEXT",
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
        "created_at": "TEXT",
    }


@pytest.mark.asyncio
async def test_ci_lesson_learned_indexes_exist(ci_db):
    cursor = await ci_db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ci_lesson_learned'"
    )
    rows = await cursor.fetchall()
    names = {r["name"] for r in rows}
    assert "idx_lesson_signature" in names
    assert "idx_lesson_project" in names
```

Append to `tests/conftest.py`:

```python
from app.db.database import Database


@pytest.fixture
async def ci_db(tmp_path):
    """Fresh aiosqlite Database for CI lesson tests, schema applied."""
    db = Database(str(tmp_path / "ci.db"))
    await db.initialize()
    yield db
    await db.close()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_schema.py -v`
Expected: FAIL with "no such table: ci_lesson_learned" (PRAGMA returns empty).

**Step 3: Write minimal implementation**

In `app/db/database.py`, append to the end of the `SCHEMA_V1` string (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS ci_lesson_learned (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid TEXT NOT NULL,
    project TEXT NOT NULL,
    test_name TEXT NOT NULL,
    error_hash TEXT NOT NULL,
    signature TEXT NOT NULL,
    raw_error TEXT,
    attempt_num INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    context_lessons TEXT,
    fix_diff TEXT,
    outcome TEXT NOT NULL,
    duration_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_lesson_signature ON ci_lesson_learned(signature, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_lesson_project ON ci_lesson_learned(project, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_lesson_run_uuid ON ci_lesson_learned(run_uuid);
```

**Note on types:** SQLite is dynamically typed; `created_at` uses `TEXT` + `datetime('now')` to match the file's existing pattern (see `api_keys.created_at`). Do NOT use `DATETIME DEFAULT CURRENT_TIMESTAMP` — that's not the house style.

**Note on the third index:** `idx_lesson_run_uuid` is included because it's cheap and makes the "group by run_uuid" query in `get_recent_occurrences` fast. If the spec reviewer objects "not in design doc", keep it — the design predates the UUID grouping decision.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_schema.py -v`
Expected: both new tests PASS. Also run existing tests to make sure we didn't break anything: `pytest tests/ -q -x`. All must PASS.

**Step 5: Commit**

```bash
git add app/db/database.py tests/conftest.py tests/test_ci_schema.py
git commit -m "feat(ci): add ci_lesson_learned table + indexes to SCHEMA_V1"
```

---

### Task 2: `normalize_error()` with default noise patterns

**Files:**
- Create: `app/core/ci_signal_dedup.py`
- Create: `tests/test_ci_signal_dedup.py`

**Step 1: Write the failing test**

Create `tests/test_ci_signal_dedup.py`:

```python
"""Tests for signal normalization and signature computation."""
from app.core.ci_signal_dedup import normalize_error


def test_normalize_strips_iso_timestamp_z():
    raw = "Connection failed at 2026-04-18T01:23:45.123Z on port 5432"
    assert normalize_error(raw) == "Connection failed at <TS> on port :<PORT>"


def test_normalize_strips_iso_timestamp_space():
    raw = "logged at 2026-04-18 01:23:45 UTC"
    assert normalize_error(raw) == "logged at <TS> UTC"


def test_normalize_strips_uuid():
    raw = "job id deadbeef-1234-5678-9abc-def012345678 aborted"
    assert normalize_error(raw) == "job id <UUID> aborted"


def test_normalize_strips_hex_address():
    raw = "segfault at 0xdeadbeef"
    assert normalize_error(raw) == "segfault at <HEX>"


def test_normalize_strips_tmp_path():
    raw = "cannot write /tmp/pytest-abc/test.txt"
    assert normalize_error(raw) == "cannot write <TMPPATH>"


def test_normalize_strips_linux_home_path():
    raw = "open /home/klipperos/foo failed"
    assert normalize_error(raw) == "open <USERPATH> failed"


def test_normalize_strips_windows_user_path():
    raw = r"open C:\Users\sevdi\test.py failed"
    assert normalize_error(raw) == "open <USERPATH> failed"


def test_normalize_strips_bigint():
    raw = "epoch 1745000000000 exceeded"
    assert normalize_error(raw) == "epoch <BIGINT> exceeded"


def test_normalize_idempotent():
    raw = "timestamp 2026-04-18T01:23:45Z and id abc12345-6789-4abc-8def-123456789012"
    once = normalize_error(raw)
    twice = normalize_error(once)
    assert once == twice
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.ci_signal_dedup'`.

**Step 3: Write minimal implementation**

Create `app/core/ci_signal_dedup.py`:

```python
"""Signal-based de-duplication and lesson-learned storage for CI auto-fix.

Pure/async functions. Sync functions (normalize_error, compute_signature) take
plain strings. Async DB functions take a Database instance from
app.db.database so they can be tested against a tmp_path DB.

See: docs/plans/2026-04-18-ci-lesson-learned-signal-dedup-design.md
"""
from __future__ import annotations

import hashlib
import re

NOISE_PATTERNS: list[tuple[str, str]] = [
    (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[\.\d]*Z?", "<TS>"),
    (r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "<TS>"),
    (r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<UUID>"),
    (r"0x[0-9a-f]+", "<HEX>"),
    (r"/tmp/[^\s)'\"]+", "<TMPPATH>"),
    (r"(?:/home/|/Users/|C:\\Users\\)[^\s)'\"]+", "<USERPATH>"),
    (r":\d{4,5}\b", ":<PORT>"),
    (r"\b\d{10,}\b", "<BIGINT>"),
]

_COMPILED = [(re.compile(pat), sub) for pat, sub in NOISE_PATTERNS]


def normalize_error(raw: str) -> str:
    """Replace noisy substrings with stable placeholders.

    Idempotent: running it twice yields the same string as once.
    """
    out = raw
    for rx, repl in _COMPILED:
        out = rx.sub(repl, out)
    return out
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: all 9 tests PASS.

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

Append to `tests/test_ci_signal_dedup.py`:

```python
from app.core.ci_signal_dedup import compute_signature


def test_signature_is_project_testname_hash_triple():
    h, sig = compute_signature("bilge-arena", "test_login", "AssertionError: 5 != 3")
    assert len(h) == 12
    assert sig == f"bilge-arena::test_login::{h}"


def test_signature_stable_across_timestamps():
    _, sig1 = compute_signature("p", "t", "failed at 2026-04-18T01:23:45Z")
    _, sig2 = compute_signature("p", "t", "failed at 2026-04-18T09:59:59Z")
    assert sig1 == sig2


def test_signature_differs_for_different_errors():
    _, sig1 = compute_signature("p", "t", "AssertionError: 5 != 3")
    _, sig2 = compute_signature("p", "t", "KeyError: missing")
    assert sig1 != sig2


def test_signature_hash_is_hex_12_chars():
    h, _ = compute_signature("p", "t", "anything")
    assert len(h) == 12
    assert all(c in "0123456789abcdef" for c in h)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: FAIL with `ImportError: cannot import name 'compute_signature'`.

**Step 3: Write minimal implementation**

Append to `app/core/ci_signal_dedup.py`:

```python
def compute_signature(project: str, test_name: str, raw_error: str) -> tuple[str, str]:
    """Return (error_hash, full_signature).

    error_hash = first 12 hex chars of sha1(normalize_error(raw_error)).
    full_signature = f"{project}::{test_name}::{error_hash}".
    """
    normalized = normalize_error(raw_error)
    error_hash = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return error_hash, f"{project}::{test_name}::{error_hash}"
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: all 13 tests PASS.

**Step 5: Commit**

```bash
git add app/core/ci_signal_dedup.py tests/test_ci_signal_dedup.py
git commit -m "feat(ci): compute_signature() returns (hash, project::test::hash)"
```

---

### Task 4: `record_lesson()` (async, takes `Database`)

**Files:**
- Modify: `app/core/ci_signal_dedup.py`
- Modify: `tests/test_ci_signal_dedup.py`

**Step 1: Write the failing test**

Append:

```python
import pytest
from app.core.ci_signal_dedup import record_lesson


@pytest.mark.asyncio
async def test_record_lesson_inserts_and_returns_id(ci_db):
    row_id = await record_lesson(
        ci_db,
        run_uuid="u1",
        project="p",
        test_name="t",
        error_hash="abc123abc123",
        signature="p::t::abc123abc123",
        raw_error="AssertionError",
        attempt_num=1,
        strategy="fix-direct",
        context_lessons=None,
        fix_diff="diff --git ...",
        outcome="passed",
        duration_ms=420,
    )
    assert row_id > 0
    row = await ci_db.fetch_one(
        "SELECT project, outcome, strategy FROM ci_lesson_learned WHERE id = ?",
        (row_id,),
    )
    assert row == {"project": "p", "outcome": "passed", "strategy": "fix-direct"}


@pytest.mark.asyncio
async def test_record_lesson_truncates_fix_diff(ci_db):
    big = "x" * 10000
    row_id = await record_lesson(
        ci_db,
        run_uuid="u2",
        project="p",
        test_name="t",
        error_hash="h",
        signature="p::t::h",
        raw_error="e",
        attempt_num=1,
        strategy="fix-direct",
        context_lessons=None,
        fix_diff=big,
        outcome="failed",
        duration_ms=0,
    )
    stored = await ci_db.fetch_one(
        "SELECT fix_diff FROM ci_lesson_learned WHERE id = ?", (row_id,)
    )
    assert len(stored["fix_diff"]) <= 4096


@pytest.mark.asyncio
async def test_record_lesson_accepts_none_optional_fields(ci_db):
    row_id = await record_lesson(
        ci_db,
        run_uuid="u3",
        project="p",
        test_name="t",
        error_hash="h",
        signature="p::t::h",
        raw_error=None,
        attempt_num=1,
        strategy="fix-direct",
        context_lessons=None,
        fix_diff=None,
        outcome="error",
        duration_ms=None,
    )
    assert row_id > 0
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: FAIL with `ImportError: cannot import name 'record_lesson'`.

**Step 3: Write minimal implementation**

Append to `app/core/ci_signal_dedup.py`:

```python
from app.db.database import Database

FIX_DIFF_CAP = 4096


async def record_lesson(
    db: Database,
    *,
    run_uuid: str,
    project: str,
    test_name: str,
    error_hash: str,
    signature: str,
    raw_error: str | None,
    attempt_num: int,
    strategy: str,
    context_lessons: str | None,
    fix_diff: str | None,
    outcome: str,
    duration_ms: int | None,
) -> int:
    """Insert one lesson row, returning its id. fix_diff is truncated to FIX_DIFF_CAP."""
    if fix_diff is not None and len(fix_diff) > FIX_DIFF_CAP:
        fix_diff = fix_diff[:FIX_DIFF_CAP]
    cursor = await db.execute(
        """
        INSERT INTO ci_lesson_learned
            (run_uuid, project, test_name, error_hash, signature, raw_error,
             attempt_num, strategy, context_lessons, fix_diff, outcome, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_uuid, project, test_name, error_hash, signature, raw_error,
         attempt_num, strategy, context_lessons, fix_diff, outcome, duration_ms),
    )
    return cursor.lastrowid
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: all PASS.

**Step 5: Commit**

```bash
git add app/core/ci_signal_dedup.py tests/test_ci_signal_dedup.py
git commit -m "feat(ci): record_lesson() async insert with fix_diff cap"
```

---

### Task 5: `get_recent_occurrences()` (async)

**Files:**
- Modify: `app/core/ci_signal_dedup.py`
- Modify: `tests/test_ci_signal_dedup.py`

**Step 1: Write the failing test**

Append:

```python
from app.core.ci_signal_dedup import get_recent_occurrences


async def _seed(db, run_uuid, sig, outcome):
    await db.execute(
        """
        INSERT INTO ci_lesson_learned
            (run_uuid, project, test_name, error_hash, signature, raw_error,
             attempt_num, strategy, context_lessons, fix_diff, outcome, duration_ms)
        VALUES (?, 'p', 't', 'h', ?, 'e', 1, 'fix-direct', NULL, NULL, ?, 0)
        """,
        (run_uuid, sig, outcome),
    )


@pytest.mark.asyncio
async def test_get_recent_occurrences_counts_failed_only(ci_db):
    sig = "p::t::abc"
    await _seed(ci_db, "u1", sig, "failed")
    await _seed(ci_db, "u2", sig, "passed")  # must not count
    await _seed(ci_db, "u3", sig, "failed")
    assert await get_recent_occurrences(ci_db, sig, window=3) == 2


@pytest.mark.asyncio
async def test_get_recent_occurrences_counts_per_run_uuid(ci_db):
    # window=3 means "last 3 distinct run_uuids". If one run_uuid has 3 failed
    # attempts, that still counts as 1 occurrence.
    sig = "p::t::abc"
    await _seed(ci_db, "u1", sig, "failed")  # attempt 1
    await _seed(ci_db, "u1", sig, "failed")  # attempt 2 — same run
    await _seed(ci_db, "u1", sig, "passed")  # attempt 3 — fixed within run
    await _seed(ci_db, "u2", sig, "failed")
    # u1 should count as 0 (final outcome passed) — but we're counting rows not runs
    # Simplified: count rows with outcome='failed' across last `window` runs.
    # Result: 3 (two u1 failures + one u2 failure)
    assert await get_recent_occurrences(ci_db, sig, window=3) == 3


@pytest.mark.asyncio
async def test_get_recent_occurrences_respects_window(ci_db):
    sig = "p::t::abc"
    for i in range(4):
        await _seed(ci_db, f"u{i}", sig, "failed")
    # Only last 3 runs (u1, u2, u3 by insertion order) — count all their failures
    assert await get_recent_occurrences(ci_db, sig, window=3) == 3


@pytest.mark.asyncio
async def test_get_recent_occurrences_zero_when_empty(ci_db):
    assert await get_recent_occurrences(ci_db, "nope", window=3) == 0
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: FAIL with ImportError.

**Step 3: Write minimal implementation**

Append:

```python
async def get_recent_occurrences(db: Database, signature: str, window: int = 3) -> int:
    """Count 'failed' attempts with this signature across the `window` most recent runs.

    A "run" is a distinct run_uuid. We look at the last `window` run_uuids that
    touched this signature (any outcome), then count how many rows inside those
    runs have outcome='failed'.
    """
    row = await db.fetch_one(
        """
        WITH recent_runs AS (
            SELECT DISTINCT run_uuid, MAX(created_at) AS last_ts
            FROM ci_lesson_learned
            WHERE signature = ?
            GROUP BY run_uuid
            ORDER BY last_ts DESC
            LIMIT ?
        )
        SELECT COUNT(*) AS n
        FROM ci_lesson_learned
        WHERE signature = ?
          AND outcome = 'failed'
          AND run_uuid IN (SELECT run_uuid FROM recent_runs)
        """,
        (signature, window, signature),
    )
    return int((row or {}).get("n") or 0)
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: all PASS.

**Step 5: Commit**

```bash
git add app/core/ci_signal_dedup.py tests/test_ci_signal_dedup.py
git commit -m "feat(ci): get_recent_occurrences() counts failures across recent run_uuids"
```

---

### Task 6: `fetch_lesson_context()` (async)

**Files:**
- Modify: `app/core/ci_signal_dedup.py`
- Modify: `tests/test_ci_signal_dedup.py`

**Step 1: Write the failing test**

Append:

```python
from app.core.ci_signal_dedup import fetch_lesson_context


@pytest.mark.asyncio
async def test_fetch_lesson_context_returns_newest_first(ci_db):
    sig = "p::t::abc"
    # Seed in ascending order; newest (by insertion id) should come back first
    await _seed(ci_db, "u1", sig, "failed")
    await _seed(ci_db, "u2", sig, "failed")
    await _seed(ci_db, "u3", sig, "passed")
    rows = await fetch_lesson_context(ci_db, "p", sig, limit=5)
    assert len(rows) == 3
    assert rows[0]["id"] > rows[1]["id"] > rows[2]["id"]


@pytest.mark.asyncio
async def test_fetch_lesson_context_respects_limit(ci_db):
    sig = "p::t::abc"
    for i in range(7):
        await _seed(ci_db, f"u{i}", sig, "failed")
    rows = await fetch_lesson_context(ci_db, "p", sig, limit=3)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_fetch_lesson_context_scoped_to_project(ci_db):
    await _seed(ci_db, "u1", "p::t::abc", "failed")
    rows = await fetch_lesson_context(ci_db, "other-project", "p::t::abc", limit=5)
    assert rows == []
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: FAIL with ImportError.

**Step 3: Write minimal implementation**

Append:

```python
async def fetch_lesson_context(
    db: Database, project: str, signature: str, limit: int = 5,
) -> list[dict]:
    """Return past lessons matching (project, signature), newest first."""
    rows = await db.fetch_all(
        """
        SELECT id, run_uuid, attempt_num, strategy, outcome, fix_diff,
               raw_error, created_at
        FROM ci_lesson_learned
        WHERE project = ? AND signature = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (project, signature, limit),
    )
    return rows
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_signal_dedup.py -v`
Expected: all PASS.

**Step 5: Commit**

```bash
git add app/core/ci_signal_dedup.py tests/test_ci_signal_dedup.py
git commit -m "feat(ci): fetch_lesson_context() returns scoped past lessons newest-first"
```

---

## 🔖 Phase 0 checkpoint

1. `pytest tests/ -q -x` — everything green.
2. `pytest tests/test_ci_signal_dedup.py tests/test_ci_schema.py -v` — all new tests green.
3. If green, continue to Phase 1. Otherwise, stop and diagnose — do NOT start Phase 1 with a red bar.

---

## Phase 1 — Passive integration (write-only)

### Task 7: `attempt_fix()` generates `run_uuid` and records every attempt

**Files:**
- Modify: `app/core/ci_fixer.py` (add module-level `_open_ci_db`, wire recording into the retry loop; actual signature of `attempt_fix` at line 188 is `(project, test_file, test_name, error, source_file=None, max_attempts=MAX_ATTEMPTS)` — do NOT change that)
- Modify: `tests/test_ci_fixer.py`

**Step 1: Write the failing test**

> **Note on test DB lifecycle:** All `attempt_fix` test snippets in this plan return `_NoCloseDB(ci_db)` from `fake_open_ci_db` rather than the raw `ci_db` fixture. `attempt_fix` has a `finally: await db.close()` block that runs after the retry loop — if the fixture is handed back directly, that close call would destroy the DB mid-test and break post-call `fetch_all` assertions. `_NoCloseDB` (defined at `tests/test_ci_fixer.py:17-34`) is a transparent proxy that forwards every attribute to the wrapped `ci_db` but makes `close()` a no-op. The fixture's own pytest teardown still closes the DB correctly after the test function returns.

Append to `tests/test_ci_fixer.py`:

```python
@pytest.mark.asyncio
async def test_attempt_fix_records_a_lesson_per_attempt(ci_db, monkeypatch):
    """Every iteration inside attempt_fix must insert a row into ci_lesson_learned."""
    from unittest.mock import AsyncMock, patch

    async def fake_open_ci_db():
        return _NoCloseDB(ci_db)

    monkeypatch.setattr("app.core.ci_fixer._open_ci_db", fake_open_ci_db)

    mock_claude = AsyncMock(return_value={
        "answer": "Fixed it",
        "session_id": "sess-1",
        "error": None,
    })
    mock_tests = AsyncMock(return_value={
        "project": "klipper",
        "total": 10, "passed": 10, "failed": 0,
        "duration_s": 2.0, "failures": [],
    })

    with patch("app.core.ci_fixer._call_claude_code", mock_claude), \
         patch("app.core.ci_fixer.run_project_tests", mock_tests):
        result = await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError: 1 != 2",
        )

    assert result["fixed"] is True
    rows = await ci_db.fetch_all(
        "SELECT project, test_name, outcome, strategy, attempt_num, run_uuid "
        "FROM ci_lesson_learned ORDER BY id"
    )
    assert len(rows) == 1
    assert rows[0]["project"] == "klipper"
    assert rows[0]["test_name"] == "test_bar"
    assert rows[0]["outcome"] == "passed"
    assert rows[0]["strategy"] == "fix-direct"
    assert rows[0]["attempt_num"] == 1


@pytest.mark.asyncio
async def test_attempt_fix_all_attempts_share_one_run_uuid(ci_db, monkeypatch):
    """When attempt_fix retries 3 times, all rows must share the same run_uuid."""
    from unittest.mock import AsyncMock, patch

    async def fake_open_ci_db():
        return _NoCloseDB(ci_db)

    monkeypatch.setattr("app.core.ci_fixer._open_ci_db", fake_open_ci_db)

    mock_claude = AsyncMock(return_value={"answer": "", "session_id": None, "error": None})
    mock_tests = AsyncMock(return_value={
        "project": "klipper",
        "total": 10, "passed": 9, "failed": 1,
        "duration_s": 2.0,
        "failures": [{"test_file": "tests/test_foo.py",
                      "test_name": "test_bar",
                      "error": "still broken"}],
    })

    with patch("app.core.ci_fixer._call_claude_code", mock_claude), \
         patch("app.core.ci_fixer.run_project_tests", mock_tests):
        await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError",
        )

    rows = await ci_db.fetch_all(
        "SELECT run_uuid, attempt_num, outcome FROM ci_lesson_learned ORDER BY id"
    )
    assert len(rows) == 3
    assert {r["run_uuid"] for r in rows} == {rows[0]["run_uuid"]}  # all identical
    assert [r["attempt_num"] for r in rows] == [1, 2, 3]
    assert all(r["outcome"] == "failed" for r in rows)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_fixer.py -k "records_a_lesson or share_one_run_uuid" -v`
Expected: FAIL — either ImportError on `_open_ci_db`, or the `ci_lesson_learned` table has no rows.

**Step 3: Write minimal implementation**

In `app/core/ci_fixer.py`:

Add imports at the top:

```python
import uuid

from app.core.config import get_settings
from app.core.ci_signal_dedup import compute_signature, record_lesson
from app.db.database import Database
```

Add helper just below imports (before the existing `CLAUDE_BIN =` line):

```python
async def _open_ci_db() -> Database:
    """Open a fresh Database connection for CI lesson recording.

    Tests monkeypatch this to return a tmp_path-scoped Database.
    """
    db = Database(get_settings().db_path)
    await db.initialize()
    return db
```

Modify `attempt_fix` (around line 188) to:

1. Generate `run_uuid = uuid.uuid4().hex` at the top.
2. Open DB once: `db = await _open_ci_db()` (wrap in try/except so DB failure is non-fatal).
3. Inside the existing `for attempt in range(1, max_attempts + 1)` loop, after `test_result = await run_project_tests(project)`:
   - Determine the current error text (already done by the code that populates `prev_errors`).
   - Compute signature + record the lesson:
     ```python
     current_error_text = error if attempt == 1 else prev_errors[-1]
     error_hash, signature = compute_signature(project, test_name, current_error_text)
     outcome = "passed" if test_result.get("failed", 0) == 0 else "failed"
     try:
         await record_lesson(
             db,
             run_uuid=run_uuid,
             project=project, test_name=test_name,
             error_hash=error_hash, signature=signature,
             raw_error=current_error_text,
             attempt_num=attempt,
             strategy="fix-direct",  # Phase 2 replaces this
             context_lessons=None,
             fix_diff=claude_result.get("answer"),
             outcome=outcome,
             duration_ms=None,
         )
     except Exception as exc:
         logger.warning("lesson record failed: %s", exc)
     ```
4. In a `finally` at the end of `attempt_fix`, call `await db.close()` (also wrap in try/except).

Place the lesson-record block AFTER `test_result = await run_project_tests(project)` and BEFORE the `if test_result.get("failed", 0) == 0: return {...}` shortcut — the passed case still needs to be recorded first. Make sure the lesson is recorded exactly once per loop iteration (not both in passed and failed branches).

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_fixer.py -v`
Expected: all PASS (new tests + existing 5 tests in `TestAttemptFix`).

**Step 5: Commit**

```bash
git add app/core/ci_fixer.py tests/test_ci_fixer.py
git commit -m "feat(ci): attempt_fix records every retry as a lesson (run_uuid grouped)"
```

---

### Task 8: Memory-API summary on successful fix (+ `memory_api_base` setting)

**Files:**
- Modify: `app/core/config.py` (add `memory_api_base` field)
- Modify: `app/core/ci_fixer.py` (add `post_lesson_summary_to_memory_api` + call on success)
- Modify: `tests/test_ci_fixer.py`

**Step 1: Write the failing test**

Append to `tests/test_ci_fixer.py`:

```python
@pytest.mark.asyncio
async def test_attempt_fix_posts_memory_summary_on_success(ci_db, monkeypatch):
    from unittest.mock import AsyncMock, patch

    async def fake_open_ci_db():
        return _NoCloseDB(ci_db)
    monkeypatch.setattr("app.core.ci_fixer._open_ci_db", fake_open_ci_db)

    posted = []

    async def fake_post(**kw):
        posted.append(kw)

    mock_claude = AsyncMock(return_value={"answer": "done", "session_id": None, "error": None})
    mock_tests = AsyncMock(return_value={
        "project": "klipper",
        "total": 1, "passed": 1, "failed": 0,
        "duration_s": 0.1, "failures": [],
    })

    with patch("app.core.ci_fixer._call_claude_code", mock_claude), \
         patch("app.core.ci_fixer.run_project_tests", mock_tests), \
         patch("app.core.ci_fixer.post_lesson_summary_to_memory_api", fake_post):
        result = await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError",
        )

    assert result["fixed"] is True
    assert len(posted) == 1
    assert posted[0]["type"] == "lesson_learned"
    assert "klipper" in posted[0]["name"]


@pytest.mark.asyncio
async def test_attempt_fix_skips_memory_post_on_failure(ci_db, monkeypatch):
    from unittest.mock import AsyncMock, patch

    async def fake_open_ci_db():
        return _NoCloseDB(ci_db)
    monkeypatch.setattr("app.core.ci_fixer._open_ci_db", fake_open_ci_db)

    posted = []

    async def fake_post(**kw):
        posted.append(kw)

    mock_claude = AsyncMock(return_value={"answer": "", "session_id": None, "error": None})
    mock_tests = AsyncMock(return_value={
        "project": "klipper",
        "total": 1, "passed": 0, "failed": 1,
        "duration_s": 0.1,
        "failures": [{"test_file": "tests/test_foo.py",
                      "test_name": "test_bar",
                      "error": "boom"}],
    })

    with patch("app.core.ci_fixer._call_claude_code", mock_claude), \
         patch("app.core.ci_fixer.run_project_tests", mock_tests), \
         patch("app.core.ci_fixer.post_lesson_summary_to_memory_api", fake_post):
        await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError",
        )

    assert posted == []
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_fixer.py -k "memory_summary or skips_memory" -v`
Expected: FAIL with `AttributeError: module 'app.core.ci_fixer' has no attribute 'post_lesson_summary_to_memory_api'`.

**Step 3: Write minimal implementation**

In `app/core/config.py`, add to `Settings`:

```python
# Memory API (cross-device lesson store)
memory_api_base: str = "http://100.113.153.62:8420/api/v1/memory"
```

In `app/core/ci_fixer.py`, add:

```python
import httpx


async def post_lesson_summary_to_memory_api(
    *, type: str, name: str, description: str, content: str
) -> None:
    """Best-effort POST a lesson summary to the memory API.

    Silent on failure. The memory API rejects payloads with backslash/newline
    characters in JSON (it uses a strict parser), so the caller is responsible
    for keeping `content` on a single line.
    """
    try:
        settings = get_settings()
        base = settings.memory_api_base
        key = settings.memory_api_key
        if not base or not key:
            return
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{base}/memories",
                headers={"X-Memory-Key": key, "Content-Type": "application/json"},
                json={"type": type, "name": name,
                      "description": description, "content": content},
            )
    except Exception as exc:
        logger.warning("memory api post failed: %s", exc)
```

Inside `attempt_fix`, after the existing `return {... "fixed": True ...}` on successful loop — move the return below the memory-post so both happen on success. Example shape:

```python
if test_result.get("failed", 0) == 0:
    logger.info("Test duzeltildi! deneme=%d", attempt)
    # NEW: post summary (single-line content — memory API rejects \n in JSON)
    await post_lesson_summary_to_memory_api(
        type="lesson_learned",
        name=f"CI fix: {project}/{test_name}",
        description=f"Attempt {attempt}, fix-direct — fixed",
        content=(
            f"Run {run_uuid[:8]}: {test_name} in {project} fixed on attempt {attempt}. "
            f"Signature: {signature}. "
            f"Diff length: {len(claude_result.get('answer') or '')} chars."
        ),
    )
    return {
        "fixed": True, "attempt": attempt,
        "project": project, "test_file": test_file, "test_name": test_name,
        "claude_responses": claude_responses, "error": None,
    }
```

Do NOT add memory-post to the failure branch. The test `test_attempt_fix_skips_memory_post_on_failure` guards this.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_fixer.py -v`
Expected: all PASS (new tests + existing ones).

**Step 5: Commit**

```bash
git add app/core/config.py app/core/ci_fixer.py tests/test_ci_fixer.py
git commit -m "feat(ci): POST lesson summary to memory API on successful fix"
```

---

## 🔖 Phase 1 checkpoint

1. `pytest tests/ -q -x` — green.
2. `bash scripts/run-all-tests.sh` — green (across all 11 projects).
3. Merge this phase to `master` OR deploy the branch to a staging Klipper and let it run 1-2 days of real CI traffic.

Daily observation query (run against whichever DB the staging server uses — `server.db` by default, pass `DB_PATH=...` to override):

```bash
sqlite3 /var/lib/linux-ai-server/server.db \
  "SELECT project, signature, COUNT(*) AS n
   FROM ci_lesson_learned
   GROUP BY project, signature
   HAVING n >= 2
   ORDER BY n DESC
   LIMIT 20;"
```

Review the top-20 recurring signatures. For each, check whether the normalizer is producing the right hash (same bug → same signature). If you see >20% "same bug, multiple signatures", add new noise patterns to `NOISE_PATTERNS` and commit; if <20%, proceed to Phase 2.

Target: after 1-2 days, ≥30 lessons, <20% signature-drift noise.

---

## Phase 2 — Active dedup + context enrichment

### Task 9: Env-gated dedup check selects strategy

**Files:**
- Modify: `app/core/ci_fixer.py`
- Modify: `tests/test_ci_fixer.py`

**Step 1: Write the failing test**

Append:

```python
@pytest.mark.asyncio
async def test_strategy_switches_to_context_enriched_after_2_failures(ci_db, monkeypatch):
    """If 2 past runs failed with the same signature, 3rd run uses context-enriched."""
    from unittest.mock import AsyncMock, patch

    async def fake_open_ci_db():
        return _NoCloseDB(ci_db)
    monkeypatch.setattr("app.core.ci_fixer._open_ci_db", fake_open_ci_db)

    # Seed 2 past failures with a known signature
    sig = "klipper::test_bar::h"
    for u in ("u-old-1", "u-old-2"):
        await ci_db.execute(
            """INSERT INTO ci_lesson_learned
               (run_uuid, project, test_name, error_hash, signature, raw_error,
                attempt_num, strategy, context_lessons, fix_diff, outcome, duration_ms)
               VALUES (?, 'klipper', 'test_bar', 'h', ?, 'e', 1,
                       'fix-direct', NULL, NULL, 'failed', 0)""",
            (u, sig),
        )

    mock_claude = AsyncMock(return_value={"answer": "fix", "session_id": None, "error": None})
    mock_tests = AsyncMock(return_value={
        "project": "klipper",
        "total": 1, "passed": 1, "failed": 0,
        "duration_s": 0.1, "failures": [],
    })

    with patch("app.core.ci_fixer._call_claude_code", mock_claude), \
         patch("app.core.ci_fixer.run_project_tests", mock_tests), \
         patch("app.core.ci_fixer.compute_signature", return_value=("h", sig)), \
         patch("app.core.ci_fixer.post_lesson_summary_to_memory_api", AsyncMock()):
        await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError",
        )

    latest = await ci_db.fetch_one(
        "SELECT strategy FROM ci_lesson_learned ORDER BY id DESC LIMIT 1"
    )
    assert latest["strategy"] == "context-enriched"


@pytest.mark.asyncio
async def test_dedup_disabled_by_env_flag(ci_db, monkeypatch):
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("CI_SIGNAL_DEDUP_ENABLED", "0")

    async def fake_open_ci_db():
        return _NoCloseDB(ci_db)
    monkeypatch.setattr("app.core.ci_fixer._open_ci_db", fake_open_ci_db)

    sig = "klipper::test_bar::h"
    for u in ("u-old-1", "u-old-2"):
        await ci_db.execute(
            """INSERT INTO ci_lesson_learned
               (run_uuid, project, test_name, error_hash, signature, raw_error,
                attempt_num, strategy, context_lessons, fix_diff, outcome, duration_ms)
               VALUES (?, 'klipper', 'test_bar', 'h', ?, 'e', 1,
                       'fix-direct', NULL, NULL, 'failed', 0)""",
            (u, sig),
        )

    mock_claude = AsyncMock(return_value={"answer": "fix", "session_id": None, "error": None})
    mock_tests = AsyncMock(return_value={
        "project": "klipper",
        "total": 1, "passed": 1, "failed": 0,
        "duration_s": 0.1, "failures": [],
    })

    with patch("app.core.ci_fixer._call_claude_code", mock_claude), \
         patch("app.core.ci_fixer.run_project_tests", mock_tests), \
         patch("app.core.ci_fixer.compute_signature", return_value=("h", sig)), \
         patch("app.core.ci_fixer.post_lesson_summary_to_memory_api", AsyncMock()):
        await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError",
        )

    latest = await ci_db.fetch_one(
        "SELECT strategy FROM ci_lesson_learned ORDER BY id DESC LIMIT 1"
    )
    assert latest["strategy"] == "fix-direct"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_fixer.py -k "strategy_switches or dedup_disabled" -v`
Expected: FAIL — strategy is hard-coded to `"fix-direct"`.

**Step 3: Write minimal implementation**

In `app/core/ci_fixer.py`:

```python
from app.core.ci_signal_dedup import get_recent_occurrences, fetch_lesson_context


def _dedup_enabled() -> bool:
    return os.environ.get("CI_SIGNAL_DEDUP_ENABLED", "1") != "0"
```

Inside `attempt_fix`, BEFORE calling `_call_claude_code`, compute strategy + context rows:

```python
error_hash, signature = compute_signature(project, test_name, current_error_text)
strategy = "fix-direct"
context_rows: list[dict] | None = None
if _dedup_enabled():
    try:
        recent = await get_recent_occurrences(db, signature, window=3)
        if recent >= 2:
            strategy = "context-enriched"
            context_rows = await fetch_lesson_context(db, project, signature, limit=5)
    except Exception as exc:
        logger.warning("dedup check failed, falling back to fix-direct: %s", exc)
```

Then in the existing `record_lesson(...)` call, pass:

```python
strategy=strategy,
context_lessons=(
    json.dumps([r["id"] for r in context_rows]) if context_rows else None
),
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_fixer.py -v`
Expected: all PASS.

**Step 5: Commit**

```bash
git add app/core/ci_fixer.py tests/test_ci_fixer.py
git commit -m "feat(ci): env-gated dedup check switches to context-enriched on recurrence"
```

---

### Task 10: `build_fix_prompt()` appends past-lessons block (Turkish)

**Files:**
- Modify: `app/core/ci_fixer.py::build_fix_prompt` (line 66)
- Modify: `tests/test_ci_fixer.py`

**Step 1: Write the failing test**

Append:

```python
def test_prompt_contains_past_lessons_when_provided():
    lessons = [
        {"attempt_num": 1, "strategy": "fix-direct", "outcome": "failed",
         "fix_diff": "diff 1", "raw_error": "err", "created_at": "2026-04-17 10:00:00"},
        {"attempt_num": 2, "strategy": "fix-direct", "outcome": "failed",
         "fix_diff": "diff 2", "raw_error": "err", "created_at": "2026-04-18 09:00:00"},
    ]
    prompt = build_fix_prompt(
        project="klipper",
        test_file="tests/test_foo.py",
        test_name="test_bar",
        error="AssertionError",
        context_lessons=lessons,
    )
    assert "Onceki oturumlardaki dersler" in prompt
    assert "diff 1" in prompt
    assert "diff 2" in prompt


def test_prompt_has_no_lessons_section_when_none():
    prompt = build_fix_prompt(
        project="klipper",
        test_file="tests/test_foo.py",
        test_name="test_bar",
        error="AssertionError",
        context_lessons=None,
    )
    assert "Onceki oturumlardaki dersler" not in prompt


def test_prompt_has_no_lessons_section_when_empty_list():
    prompt = build_fix_prompt(
        project="klipper",
        test_file="tests/test_foo.py",
        test_name="test_bar",
        error="AssertionError",
        context_lessons=[],
    )
    assert "Onceki oturumlardaki dersler" not in prompt
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ci_fixer.py -k "past_lessons or lessons_section" -v`
Expected: FAIL — `build_fix_prompt` has no `context_lessons` parameter yet.

**Step 3: Write minimal implementation**

Modify `build_fix_prompt` signature (keep backwards compatibility — existing tests must still pass):

```python
def build_fix_prompt(
    project: str,
    test_file: str,
    test_name: str,
    error: str,
    source_file: str | None = None,
    prev_errors: list[str] | None = None,
    context_lessons: list[dict] | None = None,  # NEW
) -> str:
    # ... existing body ...

    if context_lessons:
        lines.extend(["", "Onceki oturumlardaki dersler (en yenisi ilk):"])
        for lesson in context_lessons:
            lines.append(
                f"  - deneme {lesson['attempt_num']} ({lesson['strategy']}) "
                f"=> {lesson['outcome']} ({lesson['created_at']})"
            )
            if lesson.get("fix_diff"):
                lines.append(f"    diff: {lesson['fix_diff'][:500]}")

    # Insert BEFORE the final "Bu testi duzelt..." instruction lines.
```

Then update the call in `attempt_fix` to pass `context_lessons=context_rows`.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ci_fixer.py -v`
Expected: all PASS (3 new + all previous).

**Step 5: Commit**

```bash
git add app/core/ci_fixer.py tests/test_ci_fixer.py
git commit -m "feat(ci): inject past-lessons block into AI fix prompt when enriched"
```

---

### Task 11: End-to-end integration test

**Files:**
- Create: `tests/test_ci_lesson_flow.py`

**Step 1: Write the failing test**

```python
"""End-to-end: 3 consecutive attempt_fix calls, same signature, enriched on 3rd."""
from unittest.mock import AsyncMock, patch

import pytest

from app.core.ci_fixer import attempt_fix


async def _one_call(ci_db, monkeypatch, *, claude_result, test_result):
    async def fake_open_ci_db():
        return _NoCloseDB(ci_db)
    monkeypatch.setattr("app.core.ci_fixer._open_ci_db", fake_open_ci_db)

    mock_claude = AsyncMock(return_value=claude_result)
    mock_tests = AsyncMock(return_value=test_result)

    with patch("app.core.ci_fixer._call_claude_code", mock_claude), \
         patch("app.core.ci_fixer.run_project_tests", mock_tests), \
         patch("app.core.ci_fixer.compute_signature",
               return_value=("h", "klipper::test_bar::h")), \
         patch("app.core.ci_fixer.post_lesson_summary_to_memory_api", AsyncMock()):
        return await attempt_fix(
            project="klipper",
            test_file="tests/test_foo.py",
            test_name="test_bar",
            error="AssertionError",
        )


@pytest.mark.asyncio
async def test_enrichment_kicks_in_after_two_failing_calls(ci_db, monkeypatch):
    failed_result = {
        "project": "klipper",
        "total": 1, "passed": 0, "failed": 1,
        "duration_s": 0.1,
        "failures": [{"test_file": "tests/test_foo.py",
                      "test_name": "test_bar",
                      "error": "still broken"}],
    }
    passed_result = {
        "project": "klipper",
        "total": 1, "passed": 1, "failed": 0,
        "duration_s": 0.1, "failures": [],
    }
    claude = {"answer": "try", "session_id": None, "error": None}

    # Call 1: 3 retries, all fail → 3 rows, strategy fix-direct
    await _one_call(ci_db, monkeypatch, claude_result=claude, test_result=failed_result)
    # Call 2: same → another 3 rows, all fix-direct
    await _one_call(ci_db, monkeypatch, claude_result=claude, test_result=failed_result)
    # Call 3: now there are ≥2 failed runs in history → strategy should flip
    await _one_call(ci_db, monkeypatch, claude_result=claude, test_result=passed_result)

    rows = await ci_db.fetch_all(
        "SELECT strategy, outcome, attempt_num FROM ci_lesson_learned ORDER BY id"
    )
    strategies = [r["strategy"] for r in rows]
    outcomes = [r["outcome"] for r in rows]

    # First 6 rows (calls 1 + 2) are fix-direct, all failed
    assert strategies[:6] == ["fix-direct"] * 6
    assert outcomes[:6] == ["failed"] * 6
    # Call 3's rows are context-enriched
    assert all(s == "context-enriched" for s in strategies[6:])
    # Last row of call 3 is the successful one
    assert outcomes[-1] == "passed"
```

**Step 2: Run test to verify it fails OR passes**

Run: `pytest tests/test_ci_lesson_flow.py -v`
Expected: PASS if T1–T10 are all correct. If it FAILS, trace the first broken assertion back to the task that introduced it and fix there — do NOT patch this integration test to make it green.

**Step 3: (No new implementation if Step 2 passed.)**

If Step 2 failed, identify which task's behavior is off, fix it on the corresponding commit level, then rerun.

**Step 4: Run full suite**

Run: `pytest tests/ -q -x`
Expected: all PASS.

**Step 5: Commit**

```bash
git add tests/test_ci_lesson_flow.py
git commit -m "test(ci): end-to-end lesson flow covers 2-fail to enrichment transition"
```

---

## 🔖 Phase 2 checkpoint

1. `pytest tests/ -q` — green.
2. `bash scripts/run-all-tests.sh` — green (11/11 projects, all prior tests + new).
3. Push branch, open PR against `master`:
   - Title: `feat(ci): lesson-learned store + signal de-dup for AI auto-fix`
   - Body: link the design doc and this plan, enumerate the 11 committed changes, include the rollback recipe (`CI_SIGNAL_DEDUP_ENABLED=0`).
4. After merge, the default (`CI_SIGNAL_DEDUP_ENABLED=1`) is already enabled. Watch the lesson DB grow and the first context-enriched Telegram message.

---

## Success metrics (measure at +2 weeks)

Query the lesson DB on the staging/production server:

```sql
-- Repeat-fix success rate for enriched vs direct
SELECT strategy,
       SUM(CASE WHEN outcome='passed' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS success_rate,
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
- A dedicated `ci_runs` parent table — grouping via `run_uuid` is enough for this iteration.
